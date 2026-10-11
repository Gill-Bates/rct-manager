#!/usr/bin/env python3
#
# tests/test_api_examples.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Example and edge-case tests of the HTTP layer (tasks 10.10, 10.11, 12.4)."""

import asyncio
import json

import pytest

from app.catalog.registry import RegistryCatalog
from app.gateway.base import DeviceState
from app.scheduling.heartbeat import LivenessSource
from app.scheduling.shutdown import ShutdownPhase, ShutdownPlan
from tests.api_helpers import (
    ACTION_NAME,
    READ_TOKEN,
    TARGET_NAME,
    TARGET_OBJECT_ID,
    WRITE_TOKEN,
    float_payload,
    make_settings,
    running_app,
    write_fixtures,
)

BEARER = {"Authorization": f"Bearer {READ_TOKEN}"}
WRITER = {"Authorization": f"Bearer {WRITE_TOKEN}"}
CATALOG = RegistryCatalog.from_file(make_settings().object_registry_path)
OBJECT = {name: CATALOG.object_entry(name).object_id for name in CATALOG.names()}


def assert_problem(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == code
    assert body["status"] == status
    assert body["correlation_id"] == response.headers["x-request-id"]
    return body


async def test_unknown_path_and_wrong_method() -> None:
    async with running_app() as h:
        assert_problem(await h.client.get("/nowhere"), 404, "not_found")
        response = await h.client.post("/health")
        assert_problem(response, 405, "method_not_allowed")
        assert response.headers["allow"] == "GET"


async def test_browser_favicon_request_serves_the_icon_and_is_public() -> None:
    async with running_app(authorize=False) as h:
        response = await h.client.get("/favicon.ico")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/vnd.microsoft.icon"
        assert response.content.startswith(b"\x00\x00\x01\x00")  # ICO file signature
        assert "x-request-id" in response.headers
        assert "/favicon.ico" not in h.app.openapi()["paths"]


async def test_authentication_failures_carry_www_authenticate() -> None:
    async with running_app(make_settings(), authorize=False) as h:
        for headers, code in (({}, "missing_token"), ({"Authorization": "Bearer wrong"}, "invalid_token")):
            response = await h.client.get("/api/v1/devices", headers=headers)
            assert_problem(response, 401, code)
            assert response.headers["www-authenticate"] == "Bearer"
        assert (await h.client.get("/api/v1/devices", headers=BEARER)).status_code == 200


async def test_no_anonymous_access_and_bad_token_is_rejected() -> None:
    async with running_app(authorize=False) as h:
        for path in ("/api/v1/devices", "/api/v1/devices/main/metrics/battery_soc"):
            assert_problem(await h.client.get(path), 401, "missing_token")
        bad = {"Authorization": "Bearer x"}
        assert_problem(await h.client.get("/api/v1/devices", headers=bad), 401, "invalid_token")
        assert (await h.client.get("/health")).status_code == 200


async def test_vendor_effective_byte_width_falls_back_to_type_default() -> None:
    async with running_app(make_settings(enable_vendor_diagnostics=True), authorize=False) as h:
        rows = (await h.client.get("/api/v1/vendor/rct/objects", headers=WRITER)).json()
    widths = {r["protocol_data_type"]: r["effective_byte_width"] for r in rows}
    assert widths["t_float"] == 4
    unsized = ("t_string", "t_struct")
    assert all(r["effective_byte_width"] for r in rows if r["protocol_data_type"] not in unsized)


async def test_invalid_query_parameter_is_reported_per_field() -> None:
    async with running_app() as h:
        body = assert_problem(
            await h.client.get("/api/v1/devices/main/metrics/battery_soc?fresh=maybe"), 422, "invalid_parameter"
        )
        assert body["errors"][0]["parameter"] == "fresh"


async def test_disabled_write_paths_are_reported_as_write_disabled() -> None:
    async with running_app() as h:
        put = await h.client.put("/api/v1/devices/main/metrics/battery_soc", json={"value": 1})
        assert_problem(put, 404, "write_disabled")
        post = await h.client.post(f"/api/v1/devices/main/actions/{ACTION_NAME}", json={"value": 1})
        assert_problem(post, 404, "write_disabled")
        assert_problem(await h.client.delete("/api/v1/devices/main/metrics/battery_soc"), 405, "method_not_allowed")
        assert (await h.client.get("/api/v1/devices/main/metrics/battery_soc")).status_code == 200


async def test_total_batch_failure_lists_every_metric_error() -> None:
    async with running_app(settle=False, fail_connects=1000) as h:
        response = await h.client.get("/api/v1/devices/main/metrics?names=battery_soc,grid_power")
        body = assert_problem(response, 502, "device_unavailable")
        assert [e["name"] for e in body["errors"]] == ["battery_soc", "grid_power"]
        for error in body["errors"]:
            assert set(error) == {"name", "code", "detail"}
            assert error["code"] == "device_unreachable"


async def test_partial_success_answers_200_with_errors() -> None:
    silent = OBJECT["grid_power"]
    async with running_app(behavior=lambda f: "ignore" if f.object_id == silent else "respond") as h:
        response = await h.client.get("/api/v1/devices/main/metrics?names=battery_soc,grid_power")
        assert response.status_code == 200
        body = response.json()
        assert [m["name"] for m in body["metrics"]] == ["battery_soc"]
        assert [(e["name"], e["code"]) for e in body["errors"]] == [("grid_power", "device_timeout")]


async def test_stale_replacement_within_the_grace_period() -> None:
    state = {"silent": False}
    async with running_app(
        make_settings(cache_ttl_seconds=0, enable_periodic_reads=False),  # periodic values would stay pinned
        behavior=lambda f: "ignore" if state["silent"] else "respond",
    ) as h:
        first = await h.client.get("/api/v1/devices/main/metrics/battery_soc")
        assert first.status_code == 200 and first.json()["source"] == "device"
        await asyncio.sleep(0.01)  # the entry is past its TTL and inside the grace period
        state["silent"] = True
        second = (await h.client.get("/api/v1/devices/main/metrics/battery_soc")).json()
        assert second["stale"] is True and second["source"] == "cache"
        assert second["stale_reason"] == "device_timeout"
        fresh = await h.client.get("/api/v1/devices/main/metrics/battery_soc?fresh=true")
        assert fresh.status_code == 200 and fresh.json()["freshness"] == "cached"


async def test_readiness_is_503_problem_with_devices_before_the_first_heartbeat() -> None:
    async with running_app(settle=False, behavior=lambda f: "ignore") as h:
        body = assert_problem(await h.client.get("/api/v1/readiness"), 503, "not_ready")
        assert {d["device_id"]: d["state"] for d in body["devices"]} == {"main": "starting", "slave1": "starting"}
        assert "locked" not in json.dumps(body)


async def test_readiness_follows_the_failure_threshold() -> None:
    async with running_app() as h:
        ok = await h.client.get("/api/v1/readiness")
        assert ok.status_code == 200 and ok.json()["ready"] is True
        assert ok.json()["devices"][0]["liveness_source"] == LivenessSource.HEARTBEAT
        heartbeat = h.runtime.gateway._devices["main"].heartbeat
        heartbeat.consecutive_failures = 1
        assert (await h.client.get("/api/v1/readiness")).status_code == 200  # degraded still delivers values
        heartbeat.consecutive_failures = h.runtime.settings.heartbeat_failure_threshold
        body = assert_problem(await h.client.get("/api/v1/readiness"), 503, "not_ready")
        assert body["devices"][0]["state"] == DeviceState.UNREACHABLE


@pytest.mark.parametrize("phase", list(ShutdownPhase)[1:])
async def test_shutdown_phases_answer_health_readiness_and_reject_device_work(phase: ShutdownPhase) -> None:
    async with running_app() as h:
        h.runtime.shutdown.plan = ShutdownPlan(h.runtime.clock.now(), 0.0, 0.0, phase)
        assert_problem(await h.client.get("/health"), 503, "not_ready")
        assert_problem(await h.client.get("/api/v1/readiness"), 503, "not_ready")
        assert_problem(await h.client.get("/api/v1/devices/main/metrics/battery_soc"), 503, "not_ready")
        assert (await h.client.get("/api/v1/devices")).status_code == 200  # no device work involved
        assert (await h.client.get("/metrics")).status_code == 200  # a projection keeps answering
        h.runtime.shutdown.plan = None


async def test_documentation_when_enabled_declares_the_bearer_scheme() -> None:
    async with running_app(make_settings(docs_public=True)) as h:
        docs = await h.client.get("/docs")
        assert docs.status_code == 200
        assert '"tryItOutEnabled": true' in docs.text
        assert '"docExpansion": "list"' in docs.text
        assert "nav-side" in docs.text and "theme-toggle" in docs.text
        spec = (await h.client.get("/openapi.json")).json()
        assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
        for path in ("/api/v1/devices", "/api/v1/readiness", "/api/v1/devices/{device_id}/metrics"):
            assert spec["paths"][path]["get"]["security"]
        assert "/health" in spec["paths"] and not spec["paths"]["/health"]["get"].get("security")
        fresh = next(
            p for p in spec["paths"]["/api/v1/devices/{device_id}/metrics"]["get"]["parameters"] if p["name"] == "fresh"
        )
        assert "time-bounded freshness" in fresh["description"]
        assert "application/problem+json" in spec["paths"]["/api/v1/devices"]["get"]["responses"]["401"]["content"]

    async with running_app(make_settings(docs_public=True)) as h:
        spec = (await h.client.get("/openapi.json")).json()
        assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
        assert spec["paths"]["/api/v1/devices"]["get"]["security"]


@pytest.mark.parametrize("bind_address", ["127.0.0.1", "::1", "0.0.0.0"])
async def test_documentation_is_hidden_unless_enabled(bind_address: str) -> None:
    bind = {"bind_address": bind_address, "allow_non_loopback_bind": True, "behind_reverse_proxy": True}
    async with running_app(make_settings(**bind)) as h:
        for path in ("/docs", "/openapi.json"):
            assert_problem(await h.client.get(path), 404, "docs_not_available")
    async with running_app(make_settings(docs_public=True, **bind)) as h:
        for path in ("/docs", "/openapi.json"):
            assert (await h.client.get(path)).status_code == 200


# --- write paths (task 12.4) ------------------------------------------------------------------------


def _write_settings(tmp_path, **extra):
    return make_settings(enable_write_support=True, **write_fixtures(tmp_path), **extra)


async def test_scaled_metric_is_reported_as_percent_not_as_a_raw_ratio(tmp_path) -> None:
    """A registry entry with scale=100 (like the shipped battery_soc_target) must surface a raw
    device float32 ratio of 1.0 as 100.0, not as the unscaled '1' the dashboard previously showed
    (the fix sits at the catalog/decode level, so every REST API consumer gets the scaled value,
    not just the dashboard). Uses an isolated one-off registry so this does not change the shared
    test fixture's periodic-metrics footprint for unrelated tests."""
    scaled_object_id = 0x1234ABCF
    registry = json.loads(make_settings().object_registry_path.read_text(encoding="utf-8"))
    registry["entries"].append(
        {
            "name": "scaled_target",
            "object_id": f"0x{scaled_object_id:08X}",
            "data_type": "t_float",
            "unit": "%",
            "value_type": "number",
            "writable": False,
            "idempotent_write": True,
            "is_action": False,
            "preselected": False,
            "description": "test-only scaled metric",
            "scale": 100,
        }
    )
    registry_path = tmp_path / "objects_scaled.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    async with running_app(make_settings(object_registry_path=registry_path)) as h:
        h.net.payloads[scaled_object_id] = float_payload(1.0)
        response = await h.client.get("/api/v1/devices/main/metrics/scaled_target?fresh=true", headers=BEARER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["value"] == 100.0
    assert body["unit"] == "%"


async def test_write_is_confirmed_by_the_readback(tmp_path) -> None:
    async with running_app(_write_settings(tmp_path)) as h:
        h.net.payloads[TARGET_OBJECT_ID] = float_payload(0.5)
        response = await h.client.put(
            f"/api/v1/devices/main/metrics/{TARGET_NAME}", json={"value": 0.5}, headers=WRITER
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["confirmed"] is True and body["written_value"] == 0.5 and body["readback_value"] == 0.5
        cached = h.runtime.gateway._cache.get(("main", TARGET_NAME))
        assert cached is not None and cached.origin == "transaction"  # only the read-back repopulates the cache


async def test_write_with_differing_readback_is_502_with_the_readback_value(tmp_path) -> None:
    async with running_app(_write_settings(tmp_path)) as h:
        h.net.freeze_writes = True  # the device keeps its old value
        h.net.payloads[TARGET_OBJECT_ID] = float_payload(0.5)
        response = await h.client.put(
            f"/api/v1/devices/main/metrics/{TARGET_NAME}", json={"value": 0.25}, headers=WRITER
        )
        assert assert_problem(response, 502, "write_outcome_unknown")["readback_value"] == 0.5


async def test_write_with_failed_readback_is_502_with_null_and_discards_the_cache(tmp_path) -> None:
    state = {"ignore_reads": False}

    def behavior(frame):
        return (
            "ignore"
            if state["ignore_reads"] and frame.object_id == TARGET_OBJECT_ID and frame.command.name == "READ"
            else "respond"
        )

    async with running_app(_write_settings(tmp_path), behavior=behavior) as h:
        h.net.payloads[TARGET_OBJECT_ID] = float_payload(0.5)
        h.runtime.gateway._cache.put(
            ("main", TARGET_NAME),
            0.1,
            measured_at=h.runtime.clock.now(),
            received_monotonic=h.runtime.clock.monotonic(),
            origin="transaction",
        )
        state["ignore_reads"] = True
        response = await h.client.put(
            f"/api/v1/devices/main/metrics/{TARGET_NAME}", json={"value": 0.5}, headers=WRITER
        )
        body = assert_problem(response, 502, "write_outcome_unknown")
        assert "readback_value" in body and body["readback_value"] is None
        assert h.runtime.gateway._cache.get(("main", TARGET_NAME)) is None


async def test_action_never_reports_confirmation(tmp_path) -> None:
    async with running_app(_write_settings(tmp_path)) as h:
        response = await h.client.post(f"/api/v1/devices/main/actions/{ACTION_NAME}", json={"value": 1}, headers=WRITER)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["action_confirmed"] is False and body["action_note"] and body["requested_value"] == 1


async def test_write_needs_the_write_role_and_a_valid_body(tmp_path) -> None:
    async with running_app(_write_settings(tmp_path)) as h:
        url = f"/api/v1/devices/main/metrics/{TARGET_NAME}"
        assert_problem(await h.client.put(url, json={"value": 0.5}, headers=BEARER), 403, "insufficient_scope")
        bad = await h.client.put(url, content=b"{not json", headers={**WRITER, "Content-Type": "application/json"})
        assert_problem(bad, 422, "invalid_request")
        assert_problem(await h.client.put(url, json={"value": 5}, headers=WRITER), 422, "value_out_of_range")
        assert_problem(await h.client.put(url, json={"value": 0.123}, headers=WRITER), 422, "value_step_mismatch")
        assert_problem(await h.client.put(url, json={"value": "x"}, headers=WRITER), 422, "value_type_mismatch")
        other = await h.client.put("/api/v1/devices/main/metrics/battery_soc", json={"value": 1}, headers=WRITER)
        assert_problem(other, 403, "write_not_allowed")
        action = await h.client.put(f"/api/v1/devices/main/metrics/{ACTION_NAME}", json={"value": 1}, headers=WRITER)
        assert_problem(action, 409, "metric_is_action")


async def test_fresh_without_names_is_rejected_with_invalid_request() -> None:
    async with running_app() as h:
        response = await h.client.get("/api/v1/devices/main/metrics", params={"fresh": "true"})
    body = assert_problem(response, 422, "invalid_request")
    assert "fresh=true requires an explicit names list of at most 8 metrics" in body["detail"]


async def test_cached_value_stays_distinguishable_by_timestamp_age_stale_and_source() -> None:
    state = {"silent": False}
    async with running_app(
        make_settings(cache_ttl_seconds=0, enable_periodic_reads=False),
        behavior=lambda f: "ignore" if state["silent"] else "respond",
    ) as h:
        first = (await h.client.get("/api/v1/devices/main/metrics/battery_soc")).json()
        assert (first["source"], first["stale"]) == ("device", False)
        await asyncio.sleep(0.05)
        state["silent"] = True
        second = (await h.client.get("/api/v1/devices/main/metrics/battery_soc")).json()
        assert (second["source"], second["stale"]) == ("cache", True)
        assert second["timestamp"] == first["timestamp"]  # the device observation time, not the reply time
        assert second["age_seconds"] > first["age_seconds"]
        assert second["stale_reason"] == "device_timeout"
