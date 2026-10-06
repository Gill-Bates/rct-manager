#!/usr/bin/env python3
#
# tests/test_docs_startup_log.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The start-up log names where the API documentation is served, or that it is off."""

import logging

import pytest

from app.api.app_factory import create_app
from tests.api_helpers import make_settings


def _docs_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("API documentation")]


@pytest.mark.parametrize(
    ("address", "url"),
    [("127.0.0.1", "http://127.0.0.1:8123/docs"), ("::1", "http://[::1]:8123/docs")],
)
def test_enabled_docs_log_their_url(caplog: pytest.LogCaptureFixture, address: str, url: str) -> None:
    with caplog.at_level(logging.INFO, logger="app.api.app_factory"):
        create_app(make_settings(docs_public=True, bind_address=address, bind_port=8123))
    assert _docs_lines(caplog) == [f"API documentation: {url} (OpenAPI: /openapi.json)"]


def test_disabled_docs_say_so(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.api.app_factory"):
        create_app(make_settings(docs_public=False))
    assert _docs_lines(caplog) == ["API documentation disabled (DOCS_PUBLIC=false)"]
