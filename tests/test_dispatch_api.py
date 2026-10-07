#!/usr/bin/env python3
#
# tests/test_dispatch_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Battery dispatch API integration against the real RCT gateway and fake network."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.admin.store import AdminStore
from app.catalog.registry import RegistryCatalog
from app.config import DeviceEntry
from app.errors import DeviceApiError
from tests.api_helpers import (
    DISPATCH_WRITE_NAMES,
    HOST,
    PORT,
    WRITE_TOKEN,
    _fixed_pat,
    dispatch_capabilities,
    dispatch_fixtures,
    float_payload,
    make_settings,
    running_app,
)

WRITER = {"Authorization": f"Bearer {WRITE_TOKEN}"}
SECRET = "d" * 48
ADMIN_WRITE_TOKEN = _fixed_pat("AdminWriteToken")
ADMIN_WRITER = {"Authorization": f"Bearer {ADMIN_WRITE_TOKEN}"}


def settings(tmp_path: Path, **overrides):
    paths = dispatch_fixtures(tmp_path)
    admin_path = tmp_path / "admin.db"
    store = AdminStore(admin_path, SECRET)
    password = store.initialize()
    assert password is not None
    assert store.change_password(password, "replacement-test-password")
    store.put("write_names", list(DISPATCH_WRITE_NAMES))
    store.close()
    return make_settings(
        enable_write_support=True,
        hmac_secret=SECRET,
        admin_db_path=admin_path,
        # The dispatch gate blocks unverified hardware, so an integration test that drives a device
        # has to seed the verification of exactly that device explicitly.
        dispatch_db_path=dispatch_capabilities(tmp_path, ("main", "slave1"), secret=SECRET),
        dispatch_max_charge_power_w=3000,
        dispatch_max_discharge_power_w=5000,
        write_response_timeout_ms=50,
        **paths,
        **overrides,
    )


def admin_write_token(settings_obj) -> str:
    """Mint a read/write PAT on the REAL admin store (``settings.admin_db_path``), the one
    ``/admin/api/*`` authenticates bearer tokens against via ``_store()``. Distinct from the
    dispatch ``WRITE_TOKEN``, which only exists in ``running_app()``'s own separate token store
    used for ``/api/v1/devices`` auth and is never registered against the real admin store.
    """
    store = AdminStore(settings_obj.admin_db_path, SECRET)
    with patch("app.admin.store.generate_pat", side_effect=[ADMIN_WRITE_TOKEN]):
        store.create_token("admin write test token", "read/write", None)
    store.close()
    return ADMIN_WRITE_TOKEN


def seed_payloads(harness) -> None:
    catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
    values = {
        "battery_soc": float_payload(0.5),
        "grid_power": float_payload(1200),
        "battery_power": float_payload(0),
        "household_load_power": float_payload(1200),
        "power_mng_soc_strategy": b"\x00",
        "power_mng_soc_target_set": float_payload(0.5),
        "power_mng_battery_power_extern": float_payload(0),
        "power_mng_use_grid_power_enable": b"\x00",
    }
    for name, payload in values.items():
        harness.net.payloads[catalog.object_entry(name).object_id] = payload


async def test_grid_charge_then_delete_restores_device(tmp_path: Path) -> None:
    async with running_app(settings(tmp_path)) as harness:
        seed_payloads(harness)
        response = await harness.client.post(
            "/api/v1/devices/main/battery/dispatch",
            headers=WRITER,
            json={
                "mode": "charge_from_grid",
                "target_soc_percent": 80,
                "max_power_w": 4000,
                "valid_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "charging"
        assert response.json()["max_power_w"] == 3000
        assert response.json()["max_power_w_clamped"] is True
        stopped = await harness.client.delete(
            "/api/v1/devices/main/battery/dispatch", headers=WRITER
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["state"] == "idle"
        assert stopped.json()["restore_required"] is False


async def test_an_existing_request_body_produces_the_recorded_response(tmp_path: Path) -> None:
    """AC-16: the body an existing client sends today must still produce the same response, field
    for field. Recorded here so a regression shows up as a diff instead of as a silent change.
    """
    async with running_app(settings(tmp_path)) as harness:
        seed_payloads(harness)
        response = await harness.client.post(
            "/api/v1/devices/main/battery/dispatch",
            headers=WRITER,
            json={
                "mode": "charge_from_grid",
                "target_soc_percent": 80,
                "max_power_w": 4000,
                "valid_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    # Generated per operation, so they are asserted for shape rather than value.
    assert isinstance(body.pop("operation_id"), str)
    assert isinstance(body.pop("valid_until"), str)
    assert body == {
        "device_id": "main",
        "mode": "charge_from_grid",
        "state": "charging",
        "phase": "controlling",
        "control_state": "controlled",
        "restore_required": False,
        "target_soc_percent": 80.0,
        "max_power_w": 3000.0,
        "max_power_w_requested": 4000.0,
        "max_power_w_clamped": True,
        "commanded_direction": "charge",
        "commanded_power_w": 3000.0,
        "stop_reason": None,
        "fault_code": None,
        "replaced": False,
    }


async def test_a_hold_body_is_accepted_and_reports_a_held_battery(tmp_path: Path) -> None:
    """`hold` carries no SoC goal and no power budget; the commanded figures carry the zero."""
    async with running_app(settings(tmp_path)) as harness:
        seed_payloads(harness)
        response = await harness.client.post(
            "/api/v1/devices/main/battery/dispatch",
            headers=WRITER,
            json={
                "mode": "hold",
                "max_power_w": 0,
                "valid_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "holding"
        assert body["phase"] == "controlling"
        assert body["control_state"] == "controlled"
        assert body["target_soc_percent"] is None
        assert body["max_power_w"] == 0.0
        assert body["commanded_direction"] == "none"
        assert body["commanded_power_w"] == 0.0
        stopped = await harness.client.delete("/api/v1/devices/main/battery/dispatch", headers=WRITER)
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["state"] == "idle"


async def test_mode_dependent_bodies_are_rejected_before_the_device_is_touched(tmp_path: Path) -> None:
    valid_until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    bad_bodies = [
        {"mode": "hold", "target_soc_percent": 80, "max_power_w": 0, "valid_until": valid_until},
        {"mode": "hold", "max_power_w": 500, "valid_until": valid_until},
        {"mode": "charge_from_grid", "max_power_w": 2000, "valid_until": valid_until},
        {"mode": "charge_from_grid", "target_soc_percent": 80, "max_power_w": 0, "valid_until": valid_until},
    ]
    async with running_app(settings(tmp_path)) as harness:
        seed_payloads(harness)
        for body in bad_bodies:
            response = await harness.client.post(
                "/api/v1/devices/main/battery/dispatch", headers=WRITER, json=body
            )
            assert response.status_code == 422, (body, response.text)
            assert response.json()["code"] == "invalid_request"
        status = await harness.client.get("/api/v1/devices/main/battery/dispatch", headers=WRITER)
        assert status.json()["state"] == "idle"


async def test_dispatch_requires_write_role(tmp_path: Path) -> None:
    async with running_app(settings(tmp_path)) as harness:
        response = await harness.client.get("/api/v1/devices/main/battery/dispatch")
    assert response.status_code == 403


async def test_dispatch_route_is_hidden_when_writes_are_disabled() -> None:
    async with running_app(make_settings()) as harness:
        response = await harness.client.get(
            "/api/v1/devices/main/battery/dispatch", headers=WRITER
        )
    assert response.status_code == 404
    assert response.json()["code"] == "write_disabled"


def test_dispatch_fixture_contains_exact_write_registers(tmp_path: Path) -> None:
    paths = dispatch_fixtures(tmp_path)
    allowlist = json.loads(paths["write_allowlist_path"].read_text(encoding="utf-8"))
    assert {entry["name"] for entry in allowlist["entries"]} == set(DISPATCH_WRITE_NAMES)


async def test_reconfiguration_is_rejected_when_an_affected_devices_restore_fails(tmp_path: Path) -> None:
    """P0 safety invariant: removing a device with an active dispatch must be refused, not silently
    swapped out, when the synchronous restore through the still-live transport cannot complete. The
    old device graph, its transport and the dispatch's persisted state must stay intact."""
    config = settings(tmp_path, devices=[DeviceEntry(device_id="main", host=HOST, port=PORT)])
    admin_write_token(config)
    async with running_app(config) as harness:
        seed_payloads(harness)
        started = await harness.client.post(
            "/api/v1/devices/main/battery/dispatch",
            headers=WRITER,
            json={
                "mode": "charge_from_grid",
                "target_soc_percent": 80,
                "max_power_w": 4000,
                "valid_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        assert started.status_code == 200, started.text
        assert started.json()["state"] == "charging"

        with patch(
            "app.gateway.rct_dispatch.RctDispatchGateway.restore",
            side_effect=DeviceApiError("device_unreachable"),
        ):
            response = await harness.client.put(
                "/admin/api/settings", headers=ADMIN_WRITER, json={"devices": []}
            )
        assert response.status_code == 409, response.text

        # The old graph is untouched: "main" is still a live device and still reports its dispatch.
        assert "main" in harness.runtime.devices
        status = await harness.client.get(
            "/api/v1/devices/main/battery/dispatch", headers=WRITER
        )
        assert status.status_code == 200, status.text
        assert status.json()["state"] == "fault_restore_pending"
        assert status.json()["restore_required"] is True

        # The admin's own view of the device list must still show "main" too (no false "saved").
        after = (
            await harness.client.get("/admin/api/settings", headers=ADMIN_WRITER)
        ).json()["settings"]
        assert any(d["device_id"] == "main" for d in after["devices"])


async def test_reconfiguration_succeeds_and_rebuilds_limits_after_a_clean_restore(tmp_path: Path) -> None:
    """The success path of the same transaction: the active dispatch is cleanly restored, the
    device is removed from the live graph, and a newly added device picks up fresh limits (no
    leftover ``dispatch_limits_missing`` from a stale per-device dict)."""
    config = settings(tmp_path, devices=[DeviceEntry(device_id="main", host=HOST, port=PORT)])
    admin_write_token(config)
    async with running_app(config) as harness:
        seed_payloads(harness)
        started = await harness.client.post(
            "/api/v1/devices/main/battery/dispatch",
            headers=WRITER,
            json={
                "mode": "charge_from_grid",
                "target_soc_percent": 80,
                "max_power_w": 4000,
                "valid_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        assert started.status_code == 200, started.text

        # No device_id in the body: _normalize_devices assigns the new device the id "main" again
        # (not reused by anything else), which makes this the *readdressing* case (same device_id,
        # different host/port) rather than a removal — the predicate reconfigure_devices() uses
        # must treat both the same way.
        new_host, new_port = "192.0.2.90", 48999
        response = await harness.client.put(
            "/admin/api/settings", headers=ADMIN_WRITER,
            json={"devices": [{"host": new_host, "port": new_port}]},
        )
        assert response.status_code == 200, response.text
        devices = response.json()["settings"]["devices"]
        assert devices == [{"device_id": "main", "host": new_host, "port": new_port, "display_name": None, "network_id": None}]
        assert harness.runtime.devices["main"].host == new_host
        new_id = "main"

        # DispatchController.set_limits() was called with the new device set: the newly added
        # device must be dispatchable, not rejected for a limits dict that still only knew "main".
        dispatch_response = await harness.client.post(
            f"/api/v1/devices/{new_id}/battery/dispatch",
            headers=WRITER,
            json={
                "mode": "charge_from_grid",
                "target_soc_percent": 80,
                "max_power_w": 4000,
                "valid_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        # A re-addressed device is a different physical device: its VERIFIED capabilities were reset,
        # so it is refused as unverified (not for missing limits).
        assert dispatch_response.status_code == 409, dispatch_response.text
        assert dispatch_response.json()["code"] == "dispatch_unverified"
