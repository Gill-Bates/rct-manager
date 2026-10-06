#!/usr/bin/env python3
#
# tests/test_body_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Request body limit: rejected before authentication and before the body is read (Requirement 25.26)."""

import json
from types import SimpleNamespace

from app.api.body_limit import MAX_BODY_BYTES, BodyLimitMiddleware
from tests.api_helpers import make_settings, running_app, write_fixtures

URL = "/api/v1/devices/main/metrics/battery_soc"


def _settings(tmp_path):
    # Without write support the PUT route does not exist and the body would never be read.
    return make_settings(enable_write_support=True, **write_fixtures(tmp_path))


async def test_oversized_content_length_is_422_before_auth_and_without_reading() -> None:
    reads = 0

    async def inner(scope, receive, send):  # pragma: no cover - must never run
        raise AssertionError("the application must not be reached")

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "PUT",
        "path": URL,
        "headers": [(b"content-length", str(MAX_BODY_BYTES + 1).encode())],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "app": SimpleNamespace(state=SimpleNamespace()),
    }
    await BodyLimitMiddleware(inner)(scope, receive, send)
    assert reads == 0 and sent[0]["status"] == 422


async def test_oversized_declared_body_without_token_is_422_not_401(tmp_path) -> None:
    async with running_app(_settings(tmp_path), authorize=False) as h:
        response = await h.client.put(URL, content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "invalid_request"
    assert body["errors"][0]["parameter"] == "body"
    assert response.headers["x-request-id"] and body["correlation_id"] == response.headers["x-request-id"]


async def test_oversized_chunked_body_is_422(tmp_path) -> None:
    async def chunks():
        for _ in range(MAX_BODY_BYTES // 65536 + 2):
            yield b"x" * 65536

    async with running_app(_settings(tmp_path), authorize=False) as h:
        response = await h.client.put(URL, content=chunks())
    assert response.status_code == 422 and response.json()["code"] == "invalid_request"


async def test_large_legal_body_passes_the_limit(tmp_path) -> None:
    body = json.dumps({"value": "a" * (MAX_BODY_BYTES - 64)}).encode()
    assert len(body) < MAX_BODY_BYTES
    async with running_app(_settings(tmp_path), authorize=False) as h:
        response = await h.client.put(URL, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 401  # reached the authentication layer
