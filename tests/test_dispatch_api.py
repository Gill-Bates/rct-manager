#!/usr/bin/env python3
#
# tests/test_dispatch_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Battery dispatch API integration against the real RCT gateway and fake network."""

import ast
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.admin.store import AdminStore
from app.catalog.registry import RegistryCatalog
from app.config import DeviceEntry
from app.dispatch.battery_facts import resolve_battery_facts
from app.dispatch.models import DeviceLimits
from app.dispatch.soc_policy import SocTargetMode, SocTargetPolicy
from app.energy.models import EnergyMode, ModeRecord
from app.errors import DeviceApiError
from tests.api_helpers import (
    DISPATCH_WRITE_NAMES,
    HOST,
    PORT,
    WRITE_TOKEN,
    _fixed_pat,
    admin_session_headers,
    dispatch_capabilities,
    dispatch_fixtures,
    float_payload,
    make_settings,
    running_app,
    wait_startup_traffic_done,
)

WRITER = {"Authorization": f"Bearer {WRITE_TOKEN}"}
SECRET = "d" * 48
ADMIN_WRITE_TOKEN = _fixed_pat("AdminWriteToken")
ADMIN_WRITER = {"Authorization": f"Bearer {ADMIN_WRITE_TOKEN}"}


def _activate_energy_manager(harness, *device_ids: str) -> None:
    """Planning is at rest in the factory Off state; these tests need the dry-run to compute."""
    for device_id in device_ids:
        harness.runtime.energy._mode_states[device_id] = ModeRecord(
            device_id, mode=EnergyMode.MANUAL
        )
    harness.runtime.energy.activity.notify()


def settings(tmp_path: Path, **overrides):
    paths = dispatch_fixtures(tmp_path)
    admin_path = tmp_path / "admin.db"
    store = AdminStore(admin_path, SECRET)
    password = store.initialize()
    assert password is not None
    assert store.change_password(password, "replacement-test-password")
    store.put("write_names", list(DISPATCH_WRITE_NAMES))
    store.close()
    base = {
        "enable_write_support": True,
        "hmac_secret": SECRET,
        "admin_db_path": admin_path,
        # The dispatch gate blocks unverified hardware, so an integration test that drives a device
        # has to seed the verification of exactly that device explicitly.
        "dispatch_db_path": dispatch_capabilities(
            tmp_path, ("main", "slave1"), secret=SECRET
        ),
        "dispatch_max_charge_power_w": 3000,
        "dispatch_max_discharge_power_w": 5000,
        "write_response_timeout_ms": 50,
        **paths,
    }
    return make_settings(**{**base, **overrides})


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


async def test_a_generic_write_to_a_dispatch_register_is_refused_during_an_active_dispatch(
    tmp_path: Path,
) -> None:
    """P1 ownership: while a dispatch controls the inverter, the four dispatch-control registers may
    not be written through the generic PAT endpoint — the controller keeps no consistent state of a
    parallel raw write. Fail-closed: 409 dispatch_register_locked, no device transaction.
    """
    async with running_app(settings(tmp_path)) as harness:
        seed_payloads(harness)
        armed = await harness.client.post(
            "/api/v1/devices/main/battery/dispatch",
            headers=WRITER,
            json={
                "mode": "charge_from_grid",
                "target_soc_percent": 80,
                "max_power_w": 4000,
                "valid_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        assert armed.status_code == 200, armed.text
        blocked = await harness.client.put(
            "/api/v1/devices/main/metrics/power_mng_battery_power_extern",
            headers=WRITER,
            json={"value": 1500.0},
        )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "dispatch_register_locked"


async def test_a_generic_write_to_a_dispatch_register_is_allowed_when_idle(
    tmp_path: Path,
) -> None:
    """The lock is scoped to an active dispatch: with no dispatch and the Energy Manager off, the
    same register is writable through the generic endpoint (the escape hatch still exists).
    """
    async with running_app(settings(tmp_path)) as harness:
        seed_payloads(harness)
        response = await harness.client.put(
            "/api/v1/devices/main/metrics/power_mng_battery_power_extern",
            headers=WRITER,
            json={"value": 0.0},
        )
    assert response.status_code == 200, response.text


async def test_raw_control_write_rechecks_energy_mode_after_waiting_for_mode_lock(
    tmp_path: Path,
) -> None:
    async with running_app(settings(tmp_path)) as harness:
        seed_payloads(harness)
        manager = harness.runtime.energy
        assert manager is not None
        async with manager.guard_raw_write("main"):
            pending = asyncio.create_task(
                harness.client.put(
                    "/api/v1/devices/main/metrics/power_mng_battery_power_extern",
                    headers=WRITER,
                    json={"value": 0.0},
                )
            )
            await asyncio.sleep(0)
            assert not pending.done()
            manager._mode_states["main"] = ModeRecord("main", mode=EnergyMode.MANUAL)
        blocked = await asyncio.wait_for(pending, timeout=2.0)
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "dispatch_register_locked"


def _charge_body():
    return {
        "mode": "charge_from_grid",
        "target_soc_percent": 80,
        "max_power_w": 4000,
        "valid_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }


async def test_low_level_dispatch_submit_is_refused_while_energy_manager_owns_the_device(
    tmp_path: Path,
) -> None:
    """F1 ownership (P2): the low-level POST ``/battery/dispatch`` bypasses ``EnergyManager.command()``.
    While the Energy Manager owns the device (mode != OFF) only the owning surface may command it,
    so a write-scoped PAT hitting the raw dispatch route must be refused with the same device-wide
    ownership error the raw metric-write path uses — in MANUAL (admin-only) and in EXTERNAL alike.
    """
    for mode in (EnergyMode.MANUAL, EnergyMode.EXTERNAL):
        case_dir = tmp_path / mode.value
        case_dir.mkdir()
        async with running_app(settings(case_dir)) as harness:
            seed_payloads(harness)
            manager = harness.runtime.energy
            assert manager is not None
            manager._mode_states["main"] = ModeRecord("main", mode=mode)
            response = await harness.client.post(
                "/api/v1/devices/main/battery/dispatch",
                headers=WRITER,
                json=_charge_body(),
            )
        assert response.status_code == 409, (mode, response.text)
        assert response.json()["code"] == "dispatch_register_locked", mode
        # Fail-closed: the device must be left untouched (no dispatch started).
        assert manager.mode("main") is mode


async def test_low_level_dispatch_cancel_is_refused_while_energy_manager_owns_the_device(
    tmp_path: Path,
) -> None:
    """F1 ownership (P2): the same device-wide rule applies to DELETE. A PAT must not cancel/restore
    a dispatch the admin GUI (MANUAL) or an external app (EXTERNAL) owns through the Energy Manager.
    """
    for mode in (EnergyMode.MANUAL, EnergyMode.EXTERNAL):
        case_dir = tmp_path / mode.value
        case_dir.mkdir()
        async with running_app(settings(case_dir)) as harness:
            seed_payloads(harness)
            manager = harness.runtime.energy
            assert manager is not None
            manager._mode_states["main"] = ModeRecord("main", mode=mode)
            response = await harness.client.delete(
                "/api/v1/devices/main/battery/dispatch", headers=WRITER
            )
        assert response.status_code == 409, (mode, response.text)
        assert response.json()["code"] == "dispatch_register_locked", mode


async def test_low_level_dispatch_is_allowed_when_energy_manager_mode_is_off(
    tmp_path: Path,
) -> None:
    """When the Energy Manager mode is OFF nobody owns the device through it, so the low-level
    dispatch API — the EXTERNAL/off path it legitimately serves — still works end to end.
    """
    async with running_app(settings(tmp_path)) as harness:
        seed_payloads(harness)
        manager = harness.runtime.energy
        assert manager is not None
        manager._mode_states["main"] = ModeRecord("main", mode=EnergyMode.OFF)
        started = await harness.client.post(
            "/api/v1/devices/main/battery/dispatch", headers=WRITER, json=_charge_body()
        )
        assert started.status_code == 200, started.text
        assert started.json()["state"] == "charging"
        stopped = await harness.client.delete(
            "/api/v1/devices/main/battery/dispatch", headers=WRITER
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["state"] == "idle"


async def test_low_level_dispatch_submit_runs_under_the_energy_ownership_guard(
    tmp_path: Path,
) -> None:
    """F1 atomicity: the mode check and the submit are one critical section under the Energy
    Manager mode lock. Holding ``guard_raw_write`` must block a concurrent raw dispatch submit, and
    a mode change applied before releasing the lock must then be observed by that submit — so the
    check cannot be a stale pre-check outside the lock (the set_mode race).
    """
    async with running_app(settings(tmp_path)) as harness:
        seed_payloads(harness)
        manager = harness.runtime.energy
        assert manager is not None
        async with manager.guard_raw_write("main"):
            pending = asyncio.create_task(
                harness.client.post(
                    "/api/v1/devices/main/battery/dispatch",
                    headers=WRITER,
                    json=_charge_body(),
                )
            )
            await asyncio.sleep(0)
            assert not pending.done()  # blocked on the mode lock the guard holds
            manager._mode_states["main"] = ModeRecord("main", mode=EnergyMode.MANUAL)
        blocked = await asyncio.wait_for(pending, timeout=2.0)
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "dispatch_register_locked"


async def test_an_existing_request_body_produces_the_recorded_response(
    tmp_path: Path,
) -> None:
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


async def test_a_hold_body_is_accepted_and_reports_a_held_battery(
    tmp_path: Path,
) -> None:
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
        stopped = await harness.client.delete(
            "/api/v1/devices/main/battery/dispatch", headers=WRITER
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["state"] == "idle"


async def test_mode_dependent_bodies_are_rejected_before_the_device_is_touched(
    tmp_path: Path,
) -> None:
    valid_until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    bad_bodies = [
        {
            "mode": "hold",
            "target_soc_percent": 80,
            "max_power_w": 0,
            "valid_until": valid_until,
        },
        {"mode": "hold", "max_power_w": 500, "valid_until": valid_until},
        {"mode": "charge_from_grid", "max_power_w": 2000, "valid_until": valid_until},
        {
            "mode": "charge_from_grid",
            "target_soc_percent": 80,
            "max_power_w": 0,
            "valid_until": valid_until,
        },
    ]
    async with running_app(settings(tmp_path)) as harness:
        seed_payloads(harness)
        for body in bad_bodies:
            response = await harness.client.post(
                "/api/v1/devices/main/battery/dispatch", headers=WRITER, json=body
            )
            assert response.status_code == 422, (body, response.text)
            assert response.json()["code"] == "invalid_request"
        status = await harness.client.get(
            "/api/v1/devices/main/battery/dispatch", headers=WRITER
        )
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


async def test_readiness_stays_red_while_a_dispatch_record_is_unreadable(
    tmp_path: Path,
) -> None:
    """C2 regression: an unreadable dispatch record means battery settings were NOT automatically
    restored (controller.recover()'s own contract), so a green readiness must not precede that —
    even once every device otherwise answers heartbeats fine.
    """
    device_settings = settings(tmp_path)
    from app.dispatch.store import DispatchStore

    store = DispatchStore(device_settings.dispatch_db_path, SECRET)
    store.initialize()
    from app.dispatch.models import DispatchRecord, DispatchState

    store.put(DispatchRecord("main", state=DispatchState.PRECHECK))
    with store.connect() as db:
        db.execute(
            "UPDATE dispatch_operations SET encrypted=? WHERE device_id='main'",
            (b"garbage",),
        )
    store.close()

    async with running_app(device_settings) as harness:
        response = await harness.client.get("/api/v1/readiness", headers=WRITER)
        assert response.status_code == 503
        assert harness.runtime.dispatch.recovery_ready() is False
        assert harness.runtime.dispatch.unreadable_devices == ("main",)


async def test_readiness_is_green_once_dispatch_recovery_completes_cleanly(
    tmp_path: Path,
) -> None:
    async with running_app(settings(tmp_path)) as harness:
        response = await harness.client.get("/api/v1/readiness", headers=WRITER)
        assert response.status_code == 200
        assert harness.runtime.dispatch.recovery_ready() is True
        assert harness.runtime.dispatch_recovery_failed is False


async def test_a_recovery_sweep_that_raises_leaves_the_app_not_ready_and_starts_no_dispatch(
    tmp_path: Path, caplog
) -> None:
    """P2-2: if the whole recovery sweep raises (not a per-device restore the controller already
    tracks), the exception would be swallowed by the shutdown gather. It must instead surface on
    readiness, be logged, and start no dispatch run() loop, rather than leaving the loops down while
    readiness looks healthy.
    """
    from app.dispatch.controller import DispatchController

    run_calls: list = []
    real_run = DispatchController.run

    async def spy_run(self, device_id: str) -> None:
        run_calls.append(("run", device_id))
        await real_run(self, device_id)

    async def exploding_recover(self) -> None:
        raise RuntimeError("recovery sweep failed")

    with (
        patch.object(DispatchController, "recover", exploding_recover),
        patch.object(DispatchController, "run", spy_run),
        caplog.at_level(logging.ERROR, logger="app.api.app_factory"),
    ):
        async with running_app(settings(tmp_path)) as harness:
            response = await harness.client.get("/api/v1/readiness", headers=WRITER)
            assert response.status_code == 503
            assert harness.runtime.dispatch_recovery_failed is True

    assert run_calls == []  # no dispatch loop started after the failed sweep
    assert any(
        "Battery dispatch recovery failed" in r.getMessage() for r in caplog.records
    )


def test_routers_import_max_power_w_from_limits_not_the_manager() -> None:
    """F2 port boundary (P3): the dispatch and energy routers, and the admin dispatch/energy API
    modules, must take ``MAX_POWER_W`` from the owning ``app.limits`` module, not re-import it via
    the concrete ``app.energy.manager`` service, so none keeps a dependency on the service
    implementation purely for a constant.
    """
    import app.admin.dispatch_api as admin_dispatch
    import app.admin.energy_api as admin_energy
    import app.api.routers.dispatch as dispatch_router
    import app.api.routers.energy as energy_router
    from app.limits import MAX_POWER_W as limits_value

    modules = (dispatch_router, energy_router, admin_dispatch, admin_energy)
    for module in modules:
        assert module.MAX_POWER_W is limits_value
        src = Path(module.__file__).read_text(encoding="utf-8")
        imported_from_limits = {
            alias.name
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.ImportFrom) and node.module == "app.limits"
            for alias in node.names
        }
        assert "MAX_POWER_W" in imported_from_limits
        assert "from app.energy.manager import MAX_POWER_W" not in src


def test_dispatch_fixture_contains_exact_write_registers(tmp_path: Path) -> None:
    paths = dispatch_fixtures(tmp_path)
    allowlist = json.loads(paths["write_allowlist_path"].read_text(encoding="utf-8"))
    assert {entry["name"] for entry in allowlist["entries"]} == set(
        DISPATCH_WRITE_NAMES
    )


async def test_reconfiguration_is_rejected_when_an_affected_devices_restore_fails(
    tmp_path: Path,
) -> None:
    """P0 safety invariant: removing a device with an active dispatch must be refused, not silently
    swapped out, when the synchronous restore through the still-live transport cannot complete. The
    old device graph, its transport and the dispatch's persisted state must stay intact.
    """
    config = settings(
        tmp_path, devices=[DeviceEntry(device_id="main", host=HOST, port=PORT)]
    )
    admin_write_token(config)
    async with running_app(config) as harness:
        ADMIN_WRITER = await admin_session_headers(
            harness.client, "replacement-test-password"
        )
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


async def test_reconfiguration_succeeds_and_rebuilds_limits_after_a_clean_restore(
    tmp_path: Path,
) -> None:
    """The success path of the same transaction: the active dispatch is cleanly restored, the
    device is removed from the live graph, and a newly added device picks up fresh limits (no
    leftover ``dispatch_limits_missing`` from a stale per-device dict)."""
    config = settings(
        tmp_path, devices=[DeviceEntry(device_id="main", host=HOST, port=PORT)]
    )
    admin_write_token(config)
    async with running_app(config) as harness:
        ADMIN_WRITER = await admin_session_headers(
            harness.client, "replacement-test-password"
        )
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
            "/admin/api/settings",
            headers=ADMIN_WRITER,
            json={"devices": [{"host": new_host, "port": new_port}]},
        )
        assert response.status_code == 200, response.text
        devices = response.json()["settings"]["devices"]
        assert devices == [
            {
                "device_id": "main",
                "host": new_host,
                "port": new_port,
                "display_name": None,
                "network_id": None,
            }
        ]
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


async def test_readdressing_clears_unverified_evidence_and_engineering_mode_too(
    tmp_path: Path,
) -> None:
    """A revoke keeps a capability's evidence while it is UNVERIFIED (by design); a readdressing
    must not let that leftover evidence, or an engineering-mode switch, follow onto whatever
    physical device now answers under the same device_id — not only the capabilities that still
    happened to be VERIFIED when the device list was saved."""
    config = settings(
        tmp_path, devices=[DeviceEntry(device_id="main", host=HOST, port=PORT)]
    )
    token = admin_write_token(config)
    headers = {"Authorization": f"Bearer {token}"}
    async with running_app(config) as harness:
        ADMIN_WRITER = await admin_session_headers(
            harness.client, "replacement-test-password"
        )
        headers = (
            ADMIN_WRITER  # PAT calls would be refused once the cookie session exists
        )
        # UNVERIFIED but carrying evidence a revoke would leave behind (strategy code, note).
        put_cap = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/write_path_convention",
            headers=headers,
            json={
                "status": "unverified",
                "soc_strategy_external_code": 2,
                "enum_byte_width": 1,
                "bool_byte_width": 1,
                "write_frame_layout_verified": True,
                "apply_sequence_verified": True,
                "note": "evidence from the previous physical device",
            },
        )
        assert put_cap.status_code == 200, put_cap.text
        assert put_cap.json()["soc_strategy_external_code"] == 2

        put_limits = await harness.client.put(
            "/admin/api/dispatch/devices/main",
            headers=headers,
            json={
                "max_charge_power_w": 3000,
                "max_discharge_power_w": 5000,
                "engineering_mode": True,
            },
        )
        assert put_limits.status_code == 200, put_limits.text
        assert harness.runtime.dispatch.device_limits("main").engineering_mode is True

        # A non-default SoC-target policy is identity-bound in the same way the capability evidence
        # is (per-device/firmware), so a readdressing must reset it to the shipped default too.
        await harness.runtime.dispatch.set_soc_target_policy(
            "main",
            SocTargetPolicy(
                device_id="main",
                mode=SocTargetMode.BELOW_CURRENT_SOC,
                note="old device policy",
            ),
        )
        assert (
            harness.runtime.dispatch.soc_target_policy("main").mode
            is SocTargetMode.BELOW_CURRENT_SOC
        )

        response = await harness.client.put(
            "/admin/api/settings",
            headers=ADMIN_WRITER,
            json={"devices": [{"host": "192.0.2.91", "port": 48998}]},
        )
        assert response.status_code == 200, response.text

        after = await harness.client.get(
            "/admin/api/dispatch/devices/main/capabilities", headers=headers
        )
        write_path = next(
            r for r in after.json() if r["name"] == "write_path_convention"
        )
        assert write_path["status"] == "unverified"
        assert write_path["soc_strategy_external_code"] is None
        assert write_path["note"] is None
        assert harness.runtime.dispatch.device_limits("main").engineering_mode is False
        reset_policy = harness.runtime.dispatch.soc_target_policy("main")
        assert reset_policy.mode is SocTargetMode.BUSINESS_TARGET
        assert reset_policy.note is None


async def test_failed_graph_build_leaves_identity_bound_state_untouched(
    tmp_path: Path,
) -> None:
    """A readdressing whose new graph does not build must roll back completely: the old device
    keeps running, so its capability evidence and engineering mode must not have been reset.
    """
    config = settings(
        tmp_path, devices=[DeviceEntry(device_id="main", host=HOST, port=PORT)]
    )
    token = admin_write_token(config)
    headers = {"Authorization": f"Bearer {token}"}
    async with running_app(config) as harness:
        ADMIN_WRITER = await admin_session_headers(
            harness.client, "replacement-test-password"
        )
        headers = (
            ADMIN_WRITER  # PAT calls would be refused once the cookie session exists
        )
        put_cap = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/write_path_convention",
            headers=headers,
            json={
                "status": "unverified",
                "soc_strategy_external_code": 2,
                "note": "keep me",
            },
        )
        assert put_cap.status_code == 200, put_cap.text
        put_limits = await harness.client.put(
            "/admin/api/dispatch/devices/main",
            headers=headers,
            json={
                "max_charge_power_w": 3000,
                "max_discharge_power_w": 5000,
                "engineering_mode": True,
            },
        )
        assert put_limits.status_code == 200, put_limits.text

        with patch(
            "app.api.app_factory._build_device_graph", side_effect=RuntimeError("boom")
        ):
            response = await harness.client.put(
                "/admin/api/settings",
                headers=ADMIN_WRITER,
                json={"devices": [{"host": "192.0.2.91", "port": 48998}]},
            )
        assert response.status_code >= 400, response.text

        after = await harness.client.get(
            "/admin/api/dispatch/devices/main/capabilities", headers=headers
        )
        write_path = next(
            r for r in after.json() if r["name"] == "write_path_convention"
        )
        assert write_path["soc_strategy_external_code"] == 2
        assert write_path["note"] == "keep me"
        assert harness.runtime.dispatch.device_limits("main").engineering_mode is True
        assert harness.runtime.devices["main"].host == HOST


async def test_a_device_added_after_a_removal_does_not_inherit_the_removed_ones_capabilities(
    tmp_path: Path,
) -> None:
    """remove -> save -> add without a device_id -> save hands the freed id "main" to new hardware;
    the removed device's verified capabilities and engineering mode must not follow that id.
    """
    config = settings(
        tmp_path, devices=[DeviceEntry(device_id="main", host=HOST, port=PORT)]
    )
    admin_write_token(config)
    async with running_app(config) as harness:
        headers = await admin_session_headers(
            harness.client, "replacement-test-password"
        )
        # The fixture verified "main"; engineering mode is switched on for the removed device.
        assert any(
            r.status.value == "verified"
            for r in harness.runtime.dispatch.capabilities("main")
        )
        put_limits = await harness.client.put(
            "/admin/api/dispatch/devices/main",
            headers=headers,
            json={
                "max_charge_power_w": 3000,
                "max_discharge_power_w": 5000,
                "engineering_mode": True,
            },
        )
        assert put_limits.status_code == 200, put_limits.text

        removed = await harness.client.put(
            "/admin/api/settings",
            headers=headers,
            json={
                "devices": [
                    {"device_id": "slave1", "host": "192.0.2.50", "port": 48990}
                ]
            },
        )
        assert removed.status_code == 200, removed.text
        assert "main" not in harness.runtime.devices

        added = await harness.client.put(
            "/admin/api/settings",
            headers=headers,
            json={
                "devices": [
                    {"device_id": "slave1", "host": "192.0.2.50", "port": 48990},
                    {"host": "192.0.2.51", "port": 48991},
                ]
            },
        )
        assert added.status_code == 200, added.text
        assert "main" in harness.runtime.devices
        assert all(
            r.status.value == "unverified"
            for r in harness.runtime.dispatch.capabilities("main")
        )
        limits = harness.runtime.dispatch.device_limits("main")
        assert limits is None or limits.engineering_mode is False


async def test_p2_8_dynamic_plan_resolves_capacity_per_device_from_stored_limits(
    tmp_path: Path,
) -> None:
    """P2-8 (finding 5): the real ``get_dynamic_plan`` path must resolve each device's usable
    capacity from its OWN stored ``DeviceLimits`` and thread it into the ``BatteryState`` the planner
    converts SoC<->Wh with. Two devices with different stored capacities must yield two different
    resolved battery states through the endpoint — not merely a provider that accepts whatever a
    caller passes. We wrap (not replace) the real ``EnergyReadingsBatteryStateProvider.current`` so
    it runs the production resolution and we record the real ``BatteryState`` it returns. Whether the
    plan then fails closed for want of spot/solar data is irrelevant: capacity is resolved first.
    """
    from app.optimizer.adapters import EnergyReadingsBatteryStateProvider

    config = settings(tmp_path)
    async with running_app(config) as harness:
        _activate_energy_manager(harness, "main", "slave1")
        seed_payloads(harness)
        # seed_payloads only stages device payloads; an explicit read warms the cache the readings
        # port serves the (fresh, non-stale) 50 % SoC from. The endpoint itself performs no read.
        for device_id in ("main", "slave1"):
            await harness.runtime.gateway.read_metric(
                device_id, "battery_soc", fresh=False
            )
        headers = await admin_session_headers(
            harness.client, "replacement-test-password"
        )

        # Two devices, two deliberately different operator-set usable capacities, stored through the
        # real admin PUT that writes DeviceLimits.usable_capacity_wh.
        for device_id, capacity in (("main", 8_000.0), ("slave1", 20_000.0)):
            put = await harness.client.put(
                f"/admin/api/dispatch/devices/{device_id}",
                headers=headers,
                json={
                    "max_charge_power_w": 3000,
                    "max_discharge_power_w": 5000,
                    "usable_capacity_wh": capacity,
                },
            )
            assert put.status_code == 200, put.text
            assert (
                harness.runtime.dispatch.device_limits(device_id).usable_capacity_wh
                == capacity
            )

        resolved: dict[str, float] = {}
        original_current = EnergyReadingsBatteryStateProvider.current

        def recording_current(self):
            state = original_current(
                self
            )  # the real production resolution runs unchanged
            resolved[self._device_id] = state.usable_capacity_wh
            return state

        bodies: dict[str, dict] = {}
        with patch.object(
            EnergyReadingsBatteryStateProvider, "current", recording_current
        ):
            for device_id in ("main", "slave1"):
                resp = await harness.client.get(
                    f"/admin/api/energy/devices/{device_id}/dynamic-plan",
                    headers=headers,
                )
                assert resp.status_code == 200, resp.text
                bodies[device_id] = resp.json()

        # The response carries the very snapshot the planner used: usable capacity x SoC / 100.
        for device_id, capacity in (("main", 8_000.0), ("slave1", 20_000.0)):
            battery = bodies[device_id]["battery"]
            assert battery["usable_capacity_wh"] == capacity
            assert battery["soc_percent"] == 50.0
            assert battery["remaining_energy_wh"] == capacity * 50.0 / 100.0

        # The capacity each device resolved to came from its own stored DeviceLimits, so the two
        # battery states convert the SAME 50 % SoC into DIFFERENT Wh (4000 vs 10000).
        assert resolved["main"] == 8_000.0
        assert resolved["slave1"] == 20_000.0
        assert resolved["main"] != resolved["slave1"]
        soc_fraction = 0.5  # seed_payloads seeds a 50 % battery SoC
        assert resolved["main"] * soc_fraction == 4_000.0
        assert resolved["slave1"] * soc_fraction == 10_000.0


async def test_dynamic_plan_validates_policy_inputs_and_lists_replan_triggers(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    async with running_app(config) as harness:
        headers = await admin_session_headers(
            harness.client, "replacement-test-password"
        )
        url = "/admin/api/energy/devices/main/dynamic-plan"
        for query in (
            "target_soc_percent=80",
            "target_time=2026-10-15T06:00:00%2B00:00",
            "target_soc_percent=80&target_time=2026-10-15T06:00:00",
            "target_soc_percent=99&target_time=2026-10-15T06:00:00%2B00:00",
            "policy=bogus",
        ):
            assert (
                await harness.client.get(f"{url}?{query}", headers=headers)
            ).status_code == 422, query
        ok = await harness.client.get(
            f"{url}?policy=meet_deadline&target_soc_percent=80&target_time=2026-10-15T06:00:00%2B00:00",
            headers=headers,
        )
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert (
            body["policy"] is None and body["reserve_satisfied"] is None
        )  # no plan without inputs
        assert {t["code"] for t in body["replan"]["triggers"]} >= {
            "soc_deviation",
            "missed_charge_slot",
        }
        # The inert live-limit producer yields no limit: absent, never unlimited.
        assert harness.runtime.dispatch.verified_live_charge_limit_w("main") is None


def test_plan_view_exposes_flags_power_energy_assumptions_and_reasons() -> None:
    from datetime import UTC, datetime, timedelta

    from app.admin.energy_api import DryRunPlanView, _plan_flag_fields
    from app.optimizer.models import (
        BatteryState,
        ConsumptionForecast,
        PlanningInput,
        SolarForecast,
    )
    from app.optimizer.planner import DynamicChargeOptimizer, PlanParameters
    from app.spotmarket.models import SpotMarketProvider, SpotPriceSnapshot

    now = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)
    prices = SpotPriceSnapshot(
        provider=SpotMarketProvider.ENERGY_CHARTS,
        bidding_zone="DE-LU",
        currency="EUR",
        fetched_at=now,
        intervals=(),
    )
    battery = BatteryState(50.0, 10_000.0, 3000.0, 3000.0, now)
    plan = DynamicChargeOptimizer(
        PlanParameters(target_soc_percent=90.0, target_time=now + timedelta(hours=2))
    ).plan(
        PlanningInput(
            now,
            battery,
            prices,
            SolarForecast(now, ()),
            ConsumptionForecast(now, ()),
            timedelta(hours=3),
        )
    )
    view = DryRunPlanView(
        device_id="main", status="ok", horizon_hours=3.0, **_plan_flag_fields(plan)
    )
    assert view.policy == "gentle" and view.reserve_satisfied is True
    assert view.target_reachable is False and view.unserved_target_energy_wh > 0
    assert (
        view.power.live_limit_available is False
        and view.power.live_charge_limit_w is None
    )
    assert (
        view.assumptions.charge_efficiency_source == "assumed"
        and not view.assumptions.power_reference_verified
    )
    assert {r.code for r in view.limiting_reasons} >= {"live_limit_unavailable"}


async def test_limits_put_keeps_omitted_capacity_clears_explicit_null_and_status_exposes_it(
    tmp_path: Path,
) -> None:
    """The admin UI saves power limits and the engineering toggle without a capacity field: an
    omitted field must keep the stored value, only an explicit null clears it."""
    config = settings(tmp_path)
    async with running_app(config) as harness:
        headers = await admin_session_headers(
            harness.client, "replacement-test-password"
        )
        url = "/admin/api/dispatch/devices/main"
        base = {"max_charge_power_w": 3000, "max_discharge_power_w": 5000}

        put = await harness.client.put(
            url, headers=headers, json={**base, "usable_capacity_wh": 9_000.0}
        )
        assert put.status_code == 200, put.text

        omitted = await harness.client.put(
            url, headers=headers, json={**base, "engineering_mode": True}
        )
        assert omitted.status_code == 200, omitted.text
        assert omitted.json()["usable_capacity_wh"] == 9_000.0
        assert (
            harness.runtime.dispatch.device_limits("main").usable_capacity_wh == 9_000.0
        )

        status = await harness.client.get("/admin/api/energy/devices")
        entry = next(item for item in status.json() if item["device_id"] == "main")
        assert entry["limits"]["usable_capacity_wh"] == 9_000.0

        cleared = await harness.client.put(
            url, headers=headers, json={**base, "usable_capacity_wh": None}
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["usable_capacity_wh"] is None
        assert harness.runtime.dispatch.device_limits("main").usable_capacity_wh is None


def test_battery_facts_preserve_legacy_capacity_as_expert_override() -> None:
    limits = DeviceLimits(3000, 4000, usable_capacity_wh=9000)
    assert resolve_battery_facts(limits).capacity_source == "expert_override"
    assert (
        resolve_battery_facts(
            limits, verified_usable_capacity_wh=8000
        ).usable_capacity_wh
        == 9000
    )
    assert (
        resolve_battery_facts(DeviceLimits(3000, 4000)).capacity_source == "unavailable"
    )
    assert (
        resolve_battery_facts(None, verified_usable_capacity_wh=8000).capacity_source
        == "verified"
    )


class _FixedCapacity:
    def __init__(self, wh):
        self.wh = wh

    def verified_usable_capacity_wh(self, device_id):
        return self.wh


def test_resolve_device_battery_facts_priority_and_invalid_source() -> None:
    from app.dispatch.battery_facts import (
        NoVerifiedCapacity,
        resolve_device_battery_facts,
    )

    limits = DeviceLimits(3000, 4000)
    assert (
        resolve_device_battery_facts(
            limits, NoVerifiedCapacity(), "main"
        ).capacity_source
        == "unavailable"
    )
    assert (
        resolve_device_battery_facts(
            limits, _FixedCapacity(8000), "main"
        ).capacity_source
        == "verified"
    )
    for bad in (float("nan"), float("inf"), 0, -5, True, "8000"):
        assert (
            resolve_device_battery_facts(
                limits, _FixedCapacity(bad), "main"
            ).capacity_source
            == "unavailable"
        )
    override = DeviceLimits(3000, 4000, usable_capacity_wh=9000)
    assert (
        resolve_device_battery_facts(
            override, _FixedCapacity(8000), "main"
        ).usable_capacity_wh
        == 9000
    )


async def test_verified_capacity_source_reaches_device_list_and_plan_without_device_access(
    tmp_path: Path,
) -> None:
    from app.optimizer.adapters import EnergyReadingsBatteryStateProvider

    config = settings(
        tmp_path,
        enable_write_support=False,
        dispatch_max_charge_power_w=None,
        dispatch_max_discharge_power_w=None,
    )
    async with running_app(config) as harness:
        _activate_energy_manager(harness, "main")
        seed_payloads(harness)
        await harness.runtime.gateway.read_metric("main", "battery_soc", fresh=False)
        headers = await admin_session_headers(
            harness.client, "replacement-test-password"
        )
        url = "/admin/api/energy/devices/main/planning-limits"
        body = {"max_charge_power_w": 3000, "max_discharge_power_w": 4000}
        assert (
            await harness.client.put(url, headers=headers, json=body)
        ).status_code == 200

        async def source() -> str:
            listing = await harness.client.get(
                "/admin/api/energy/planning-devices", headers=headers
            )
            return next(i for i in listing.json() if i["device_id"] == "main")[
                "battery_capacity_source"
            ]

        listing = await harness.client.get(
            "/admin/api/energy/planning-devices", headers=headers
        )
        assert next(i for i in listing.json() if i["device_id"] == "main")["mode"] in {
            "off",
            "manual",
            "external",
            None,
        }

        resolved = {}
        original = EnergyReadingsBatteryStateProvider.current

        def recording(self):
            state = original(self)
            resolved["wh"] = state.usable_capacity_wh
            return state

        plan_url = "/admin/api/energy/devices/main/dynamic-plan"
        assert await source() == "unavailable"  # default source never reports a value

        harness.runtime.battery_capacity = _FixedCapacity(8_000.0)
        await wait_startup_traffic_done(harness.app)
        sent_before = len(harness.net.frames)
        with patch.object(EnergyReadingsBatteryStateProvider, "current", recording):
            assert (
                await harness.client.get(plan_url, headers=headers)
            ).status_code == 200
        assert await source() == "verified"
        assert resolved["wh"] == 8_000.0
        assert len(harness.net.frames) == sent_before  # the plan endpoint sent nothing

        saved = await harness.client.put(
            url, headers=headers, json={**body, "usable_capacity_wh": 9_000}
        )
        assert saved.status_code == 200, saved.text
        assert await source() == "expert_override"
        with patch.object(EnergyReadingsBatteryStateProvider, "current", recording):
            assert (
                await harness.client.get(plan_url, headers=headers)
            ).status_code == 200
        assert resolved["wh"] == 9_000.0


async def test_dry_run_planning_data_works_with_write_support_off(
    tmp_path: Path,
) -> None:
    """The preview's device list and battery data do not depend on the dispatch controller, and
    saving them never switches battery control on."""
    from app.optimizer.adapters import EnergyReadingsBatteryStateProvider

    config = settings(
        tmp_path,
        enable_write_support=False,
        dispatch_max_charge_power_w=None,
        dispatch_max_discharge_power_w=None,
    )
    async with running_app(config) as harness:
        _activate_energy_manager(harness, "main")
        assert harness.runtime.dispatch is None
        seed_payloads(harness)
        await harness.runtime.gateway.read_metric("main", "battery_soc", fresh=False)
        headers = await admin_session_headers(
            harness.client, "replacement-test-password"
        )
        listing = await harness.client.get(
            "/admin/api/energy/planning-devices", headers=headers
        )
        assert listing.status_code == 200, listing.text
        assert {item["device_id"] for item in listing.json()} == set(
            harness.runtime.devices
        )
        assert all(item["limits"] is None for item in listing.json())
        assert all(
            item["battery_capacity_source"] == "unavailable" for item in listing.json()
        )

        url = "/admin/api/energy/devices/main/planning-limits"
        body = {"max_charge_power_w": 3000, "max_discharge_power_w": 4000}
        saved = await harness.client.put(
            url, headers=headers, json={**body, "usable_capacity_wh": 9_000}
        )
        assert saved.status_code == 200, saved.text
        assert harness.runtime.dispatch is None  # no executor was activated

        kept = await harness.client.put(
            url, headers=headers, json=body
        )  # omitted capacity is kept
        assert kept.json()["usable_capacity_wh"] == 9_000
        listing = await harness.client.get(
            "/admin/api/energy/planning-devices", headers=headers
        )
        main = next(item for item in listing.json() if item["device_id"] == "main")
        assert main["limits"] == {
            "max_charge_power_w": 3000,
            "max_discharge_power_w": 4000,
            "engineering_mode": False,
            "usable_capacity_wh": 9_000,
        }
        assert main["battery_capacity_source"] == "expert_override"

        resolved = {}
        original = EnergyReadingsBatteryStateProvider.current

        def recording(self):
            state = original(self)
            resolved[self._device_id] = (
                state.usable_capacity_wh,
                state.max_charge_power_w,
                state.max_discharge_power_w,
            )
            return state

        with patch.object(EnergyReadingsBatteryStateProvider, "current", recording):
            plan = await harness.client.get(
                "/admin/api/energy/devices/main/dynamic-plan", headers=headers
            )
        assert plan.status_code == 200, plan.text
        assert resolved["main"] == (9_000, 3000, 4000)

        cleared = await harness.client.put(
            url, headers=headers, json={**body, "usable_capacity_wh": None}
        )
        assert cleared.json()["usable_capacity_wh"] is None
        # Battery execution stays gated by write support.
        assert (
            await harness.client.put(
                "/admin/api/dispatch/devices/main", headers=headers, json=body
            )
        ).status_code == 503


async def test_a_site_timezone_change_reconciles_the_inverter_day_history_at_once(
    tmp_path: Path,
) -> None:
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    from app.gateway.history import DailyConsumptionReading

    class _ZoneAwareDayHistory:
        """The inverter's day totals, dated in whatever site zone the service passes at query time."""

        def __init__(self, gateway, timezone) -> None:
            self._timezone = timezone

        async def daily_consumption(self, device_id, *, before, limit):
            cutoff = before.astimezone(ZoneInfo(self._timezone())).date()
            return [
                DailyConsumptionReading(cutoff - timedelta(days=age), 9000.0 + age)
                for age in range(1, limit + 1)
            ]

    class _SiteZones:
        def resolve(self, latitude, longitude):
            return "America/New_York" if longitude < -30 else "Europe/Berlin"

    async def eventually(condition) -> None:
        # Bounded far below the 900 s readiness check: only a wake-up can make this pass.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not condition():
            assert loop.time() < deadline, "the day history was not reconciled"
            await asyncio.sleep(0.02)

    def stored_zones() -> list[str]:
        rows = store.get_consumption_daily("main", "2000-01-01", "2100-01-01")
        return [row["timezone"] for row in rows]

    weather = {
        "enabled": True,
        "provider": "open_meteo",
        "location_name": "Remscheid, North Rhine-Westphalia, Germany",
        "location_region": "North Rhine-Westphalia, Germany",
        "latitude": 51.1798,
        "longitude": 7.1925,
        "pv_peak_power_wp": 10000.0,
        "tilt_deg": 35.0,
        "azimuth_deg": 0.0,
        "performance_ratio": 0.85,
    }
    with (
        patch(
            "app.gateway.rct_history.RctDailyConsumptionHistory", _ZoneAwareDayHistory
        ),
        patch("app.admin.consumption_backfill.STARTUP_DELAY_SECONDS", 0.0),
    ):
        async with running_app(
            settings(tmp_path, enable_write_support=False)
        ) as harness:
            harness.app.state.solar_timezone_resolver = _SiteZones()
            store = harness.app.state.admin_store
            headers = await admin_session_headers(
                harness.client, "replacement-test-password"
            )
            # Fresh install: no Weather yet, so the first import is reconciled in UTC.
            await eventually(lambda: stored_zones() == ["UTC"] * 60)

            for site_zone, longitude in (
                ("Europe/Berlin", 7.1925),
                ("America/New_York", -74.0),
            ):
                response = await harness.client.put(
                    "/admin/api/energy/weather/config",
                    headers=headers,
                    json={**weather, "longitude": longitude},
                )
                assert response.status_code == 200, response.text
                # Re-read from the inverter under the new zone; the old rows are not just relabelled.
                await eventually(lambda zone=site_zone: stored_zones() == [zone] * 60)
                history = (
                    await harness.client.get(
                        "/admin/api/energy/devices/main/consumption-history",
                        headers=headers,
                    )
                ).json()
                assert history["timezone"] == site_zone
                assert [day["source"] for day in history["days"]] == [
                    "inverter_history"
                ] * 60


async def test_consumption_history_lists_the_last_60_completed_days_from_the_store(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path, enable_write_support=False)
    async with running_app(config) as harness:
        headers = await admin_session_headers(
            harness.client, "replacement-test-password"
        )
        store = harness.app.state.admin_store
        now = harness.runtime.clock.now()
        yesterday = (now.astimezone(UTC) - timedelta(days=1)).date()
        store.upsert_consumption_daily(
            "main",
            [
                {
                    "local_date": yesterday.isoformat(),
                    "energy_wh": 12345.0,
                    "source": "inverter_history",
                    "timezone": "UTC",
                    "fetched_at": now.isoformat(),
                },
            ],
        )
        response = await harness.client.get(
            "/admin/api/energy/devices/main/consumption-history", headers=headers
        )
        assert response.status_code == 200
        days = response.json()["days"]
        assert (
            len(days) == 60 and days[-1]["date"] == yesterday.isoformat()
        )  # oldest first, running day excluded
        assert days[-1] == {
            "date": yesterday.isoformat(),
            "energy_wh": 12345.0,
            "source": "inverter_history",
        }
        assert all(
            day["source"] == "unavailable" and day["energy_wh"] is None
            for day in days[:-1]
        )
        body = response.json()
        assert body["recording_since"] is None and "inverter_import_enabled" not in body
        store.upsert_consumption_intervals(
            "main",
            [
                {
                    "interval_start": "2026-10-14T20:15:00+00:00",
                    "interval_end": "2026-10-14T20:30:00+00:00",
                    "energy_wh": 100.0,
                    "sample_count": 3,
                    "coverage_seconds": 900.0,
                },
            ],
        )
        again = await harness.client.get(
            "/admin/api/energy/devices/main/consumption-history", headers=headers
        )
        assert again.json()["recording_since"] == "2026-10-14T20:15:00+00:00"
        assert (
            await harness.client.get(
                "/admin/api/energy/devices/nope/consumption-history", headers=headers
            )
        ).status_code == 404


async def test_dynamic_plan_computes_nothing_while_off_and_keeps_the_plan_when_active(
    tmp_path: Path,
) -> None:
    from app.admin import energy_api
    from app.optimizer.adapters import EnergyReadingsBatteryStateProvider

    real_build = energy_api.build_dry_run_plan
    calls: list[int] = []

    def spy(**kwargs):
        calls.append(1)
        return real_build(**kwargs)

    config = settings(tmp_path, enable_write_support=False)
    async with running_app(config) as harness:
        headers = await admin_session_headers(
            harness.client, "replacement-test-password"
        )
        url = "/admin/api/energy/devices/main/dynamic-plan"
        with (
            patch.object(energy_api, "build_dry_run_plan", spy),
            patch.object(
                EnergyReadingsBatteryStateProvider,
                "current",
                side_effect=AssertionError("Off must not read the planning inputs"),
            ),
        ):
            off = await harness.client.get(
                url, headers=headers
            )  # no stored mode == Off
        assert off.status_code == 200, off.text
        assert off.json()["status"] == "energy_manager_off"
        assert off.json()["slots"] == [] and calls == []

        for mode in (EnergyMode.MANUAL, EnergyMode.EXTERNAL):
            _activate_energy_manager(harness, "main")
            harness.runtime.energy._mode_states["main"] = ModeRecord("main", mode=mode)
            with patch.object(energy_api, "build_dry_run_plan", spy):
                active = await harness.client.get(url, headers=headers)
            assert active.status_code == 200, active.text
            assert active.json()["status"] != "energy_manager_off"
        assert len(calls) == 2
