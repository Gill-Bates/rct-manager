#!/usr/bin/env python3
#
# tests/test_auth_opt_out.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""auth_required: fail-closed by default, explicit opt-out accepts anonymous callers (Requirement 12.12)."""

import pytest

from app.config import TokenRole
from app.errors import AuthenticationError
from app.security.tokens import ANONYMOUS, TokenStore
from tests.api_helpers import READ_TOKEN, make_settings, running_app


def test_default_store_rejects_missing_token() -> None:
    with pytest.raises(AuthenticationError) as info:
        TokenStore().authenticate(None)
    assert info.value.code == "missing_token"


def test_opt_out_store_admits_anonymous_but_still_checks_sent_tokens() -> None:
    store = TokenStore(auth_required=False)
    assert store.authenticate(None) is ANONYMOUS and ANONYMOUS.role is TokenRole.READ_WRITE
    with pytest.raises(AuthenticationError):
        store.authenticate("Bearer " + "x" * 40)


def test_authenticate_required_ignores_the_opt_out() -> None:
    """P2-4: a caller whose own setting demands a token must not inherit the global opt-out."""
    store = TokenStore(auth_required=False)
    for header in (None, "", "   "):
        with pytest.raises(AuthenticationError) as info:
            store.authenticate_required(header)
        assert info.value.code == "missing_token"
    with pytest.raises(AuthenticationError) as invalid:
        store.authenticate_required("Bearer " + "x" * 40)
    assert invalid.value.code == "invalid_token"


async def test_default_app_rejects_anonymous_requests() -> None:
    async with running_app(authorize=False) as h:
        response = await h.client.get("/api/v1/devices")
    assert response.status_code == 401


async def test_opt_out_app_serves_anonymous_requests() -> None:
    async with running_app(make_settings(auth_required=False), authorize=False) as h:
        assert (await h.client.get("/api/v1/devices")).status_code == 200
        wrong = await h.client.get("/api/v1/devices", headers={"Authorization": f"Bearer {READ_TOKEN}x"})
    assert wrong.status_code == 401
