#!/usr/bin/env python3
#
# tests/test_auth.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Token and PAT authentication: fail-closed default and opt-out, off-loop SQLite lookups, PAT format and storage."""

import asyncio
import hashlib
import re
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from app.admin.store import AdminStore
from app.config import TokenRole
from app.errors import AuthenticationError
from app.security.dependencies import require_read
from app.security.pat import PAT_PREFIX, generate_pat, pat_well_formed
from app.security.tokens import ANONYMOUS, Principal, TokenStore
from tests.api_helpers import READ_TOKEN, make_settings, running_app


class _SlowTokens:
    def __init__(self, blocked: threading.Event, release: threading.Event) -> None:
        self.thread: int | None = None
        self._blocked = blocked
        self._release = release

    def authenticate(self, authorization: str | None) -> Principal:
        self.thread = threading.get_ident()
        self._blocked.set()  # proves the worker thread is blocked, not the event loop
        self._release.wait(timeout=5)
        return Principal("tok", TokenRole.READ)


async def test_require_read_keeps_the_event_loop_responsive() -> None:
    """Deterministic version of the offloading check: the worker blocks on a threading.Event,
    the test proves the event loop still runs a coroutine while blocked, then releases the worker.
    No timing-based tick-count assumption and no un-awaited cancelled task."""
    blocked = threading.Event()
    release = threading.Event()
    tokens = _SlowTokens(blocked, release)
    limiter = SimpleNamespace(
        check_auth_blocked=lambda a: None, check_request=lambda c: None, record_auth_failure=lambda a: None
    )
    ctx = SimpleNamespace(tokens=tokens, limiter=limiter, client_ip=SimpleNamespace(resolve=lambda peer, headers: "192.0.2.1"))
    request = SimpleNamespace(client=SimpleNamespace(host="192.0.2.1"), headers={}, scope={})
    ticked = asyncio.Event()

    async def ticker() -> None:
        while True:
            await asyncio.sleep(0.01)
            ticked.set()

    task = asyncio.create_task(ticker())
    auth_task = asyncio.create_task(require_read(request, ctx))  # type: ignore[arg-type]
    await asyncio.to_thread(blocked.wait, 5)  # worker is now blocked in authenticate()
    assert blocked.is_set()
    await asyncio.wait_for(ticked.wait(), timeout=5)  # the loop still runs a coroutine meanwhile
    release.set()
    principal = await auth_task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert principal.token_id == "tok"
    assert tokens.thread != threading.get_ident()


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


@pytest.fixture
def store(tmp_path) -> AdminStore:
    s = AdminStore(tmp_path / "admin.db", "s" * 48)
    s.initialize()
    yield s
    s.close()


def test_authenticate_token_matches_by_keyed_digest_not_by_comparing_plaintext(tmp_path, store) -> None:
    # The lookup key is an HMAC, so no byte-wise token comparison can leak timing information.
    record, token = store.create_token("ci", "read", None)
    with sqlite3.connect(tmp_path / "admin.db") as db:
        digests = [row[0] for row in db.execute("SELECT digest FROM pats")]
    assert len(digests) == 1
    assert token not in digests[0]
    assert digests[0] != hashlib.sha256(token.encode()).hexdigest()
    assert store.authenticate_token(token).id == record["id"]


def test_authenticate_token_rejects_unknown_malformed_and_unprefixed_tokens(store) -> None:
    _, token = store.create_token("ci", "read", None)
    assert store.authenticate_token(generate_pat()) is None
    assert store.authenticate_token(token[len(PAT_PREFIX):]) is None
    assert store.authenticate_token(token + "x") is None
    assert store.authenticate_token("pat_" + "a" * 600) is None


def test_generated_pats_have_prefix_base62_body_and_valid_checksum() -> None:
    tokens = {generate_pat() for _ in range(200)}
    assert len(tokens) == 200
    for token in tokens:
        assert re.fullmatch(r"pat_[0-9A-Za-z]{46}", token)
        assert pat_well_formed(token)


def test_mistyped_or_truncated_pats_are_rejected() -> None:
    token = generate_pat()
    flipped = token[:10] + ("A" if token[10] != "A" else "B") + token[11:]
    assert not pat_well_formed(flipped)
    assert not pat_well_formed(token[:-1])
    assert not pat_well_formed(PAT_PREFIX + "x" * 46)


def test_tokens_without_the_pat_prefix_are_rejected() -> None:
    assert not pat_well_formed("legacy-token-from-api-tokens")
    assert not pat_well_formed(generate_pat()[len(PAT_PREFIX):])
