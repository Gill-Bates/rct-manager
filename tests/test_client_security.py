#!/usr/bin/env python3
#
# tests/test_client_security.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Client-facing security boundaries: source address resolution, request body limit, secrets kept out of the read API."""

import json
from ipaddress import ip_network
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from app.allowlist import Allowlist
from app.api.body_limit import MAX_BODY_BYTES, BodyLimitMiddleware
from app.catalog.registry import RegistryCatalog
from app.errors import ConfigError
from app.security.client_ip import ClientIpResolver
from tests.api_helpers import make_settings, running_app, write_fixtures

ROOT = Path(__file__).resolve().parents[1]


FIXTURES = Path(__file__).resolve().parent / "fixtures"


SECRET = {
    "name": "wifi_password",
    "object_id": "0x14C0E627",
    "data_type": "t_string",
    "unit": "",
    "value_type": "string",
    "idempotent_write": False,
    "is_action": False,
    "preselected": False,
    "description": "wifi.password",
    "writable": True,
}


def test_shipped_files_do_not_contain_the_wifi_password() -> None:
    catalog = RegistryCatalog.from_file(ROOT / "app/catalog/objects.json")
    allowed = Allowlist.load(ROOT / "app/catalog/default_write_allowlist.json", catalog)
    assert "wifi_password" not in {e.name for e in catalog.entries()}
    assert allowed.entry("wifi_password") is None
    assert "wifi_authentication_method" in {e.name for e in catalog.entries()}  # an algorithm name, no secret


async def test_wifi_password_is_unknown_over_the_read_api() -> None:
    settings = make_settings(
        object_registry_path=ROOT / "app/catalog/objects.json", write_allowlist_path=ROOT / "app/catalog/default_write_allowlist.json"
    )
    async with running_app(settings, settle=False) as h:
        single = await h.client.get("/api/v1/devices/main/metrics/wifi_password")
        batch = await h.client.get("/api/v1/devices/main/metrics", params={"names": "wifi_password"})
    assert single.status_code == 404 and single.json()["code"] == "unknown_metric"
    assert batch.status_code == 422


@pytest.mark.parametrize("name", ["wifi_password", "renamed_secret"])
def test_registry_with_the_wifi_password_aborts_the_start(tmp_path: Path, name: str) -> None:
    raw = json.loads((FIXTURES / "objects.json").read_text(encoding="utf-8"))
    raw["entries" if "entries" in raw else "objects"].append({**SECRET, "name": name})
    path = tmp_path / "objects_read.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        RegistryCatalog.from_file(path)
    assert "secret" in exc.value.context["detail"]


def test_allowlist_with_the_wifi_password_aborts_the_start(tmp_path: Path) -> None:
    catalog = RegistryCatalog.from_file(FIXTURES / "objects.json")
    path = tmp_path / "objects_write_allowed.json"
    path.write_text(
        json.dumps({"version": 1, "entries": [{"name": "wifi_password", "data_type": "t_string"}]}), encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        Allowlist.load(path, catalog)


URL = "/api/v1/devices/main/metrics/battery_soc"


def _settings(tmp_path):
    # Without write support the PUT route does not exist and the body would never be read.
    return make_settings(enable_write_support=True, **write_fixtures(tmp_path))


async def test_oversized_content_length_is_413_before_auth_and_without_reading() -> None:
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
    assert reads == 0 and sent[0]["status"] == 413


async def test_oversized_declared_body_without_token_is_413_not_401(tmp_path) -> None:
    async with running_app(_settings(tmp_path), authorize=False) as h:
        response = await h.client.put(URL, content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "invalid_request"
    assert body["errors"][0]["parameter"] == "body"
    assert response.headers["x-request-id"] and body["correlation_id"] == response.headers["x-request-id"]


async def test_oversized_chunked_body_is_413(tmp_path) -> None:
    async def chunks():
        for _ in range(MAX_BODY_BYTES // 65536 + 2):
            yield b"x" * 65536

    async with running_app(_settings(tmp_path), authorize=False) as h:
        response = await h.client.put(URL, content=chunks())
    assert response.status_code == 413 and response.json()["code"] == "invalid_request"


async def test_large_legal_body_passes_the_limit(tmp_path) -> None:
    body = json.dumps({"value": "a" * (MAX_BODY_BYTES - 64)}).encode()
    assert len(body) < MAX_BODY_BYTES
    async with running_app(_settings(tmp_path), authorize=False) as h:
        response = await h.client.put(URL, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 401  # reached the authentication layer


def test_two_forwarding_header_lines_are_joined_in_order() -> None:
    resolver = ClientIpResolver([ip_network("10.0.0.0/8")], "X-Forwarded-For")
    headers = Headers(raw=[(b"x-forwarded-for", b"203.0.113.9"), (b"x-forwarded-for", b"10.1.1.1")])
    assert resolver.resolve("10.0.0.1", headers) == "203.0.113.9"  # the first line is not lost


def test_fully_trusted_forwarding_chain_yields_the_leftmost_hop() -> None:
    """P3: a chain entirely inside trusted_proxies must not collapse to the immediate peer."""
    resolver = ClientIpResolver([ip_network("10.0.0.0/24")], "X-Forwarded-For")
    headers = Headers(raw=[(b"x-forwarded-for", b"10.0.0.10, 10.0.0.2")])
    assert resolver.resolve("10.0.0.3", headers) == "10.0.0.10"


def test_partially_trusted_forwarding_chain_yields_the_first_untrusted_hop() -> None:
    """Existing behavior must survive the fully-trusted fix: the untrusted hop closest to the origin wins."""
    resolver = ClientIpResolver([ip_network("10.0.0.0/24")], "X-Forwarded-For")
    headers = Headers(raw=[(b"x-forwarded-for", b"203.0.113.9, 10.0.0.2")])
    assert resolver.resolve("10.0.0.3", headers) == "203.0.113.9"
