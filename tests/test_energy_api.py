#!/usr/bin/env python3
#
# tests/test_energy_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The Energy Manager HTTP surfaces, end to end against the fake inverter network.

Two properties are pinned here that no unit test can prove: the public ``/api/v1`` contract carries
business semantics only — no register name, no raw strategy code, no byte width, no sign flag — and
the one GET serves the whole card, readings included, without costing a device transaction per poll.
"""

import asyncio
import json
import math
import re
import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.requests import Request

from app.admin.energy_verification import BalanceProof, BalanceSample, prove_signs_from_balance
from app.admin.store import AdminStore
from app.api.app_factory import (
    _ENERGY_METRIC_NAMES,
    _effective_periodic_names,
    create_app,
)
from app.api.problems import _is_write_path
from app.catalog.base import is_numeric
from app.catalog.registry import RegistryCatalog
from app.energy.models import EnergyMode
from app.gateway.battery_inventory import CapacityCalibrationSample, MeasuredBatteryCapacity
from app.protocol.types import WRITE_COMMANDS
from app.protocol.values import encode_value
from app.scheduling.shutdown import ShutdownPlan
from tests.api_helpers import (
    DISPATCH_WRITE_NAMES,
    HOST,
    PORT,
    READ_TOKEN,
    WRITE_TOKEN,
    dispatch_capabilities,
    dispatch_fixtures,
    float_payload,
    make_settings,
    running_app,
)

WRITER = {"Authorization": f"Bearer {WRITE_TOKEN}"}
READER = {"Authorization": f"Bearer {READ_TOKEN}"}
SECRET = "d" * 48
PASSWORD = "replacement-test-password"
ROOT = Path(__file__).resolve().parents[1]

# Seeded as the operator's existing selection: one of the four, deliberately not the first required
# name, so the add-only union's order is observable in the admin store afterwards.
PRE_APPROVED = ("power_mng_use_grid_power_enable",)
# A narrowed Prometheus selection: without the Energy Manager pinning its own metric names the
# readings would go dark here.
EXPOSED = ["battery_power"]


def energy_settings(tmp_path: Path, *, unpinned: bool = True, **overrides):
    """A dispatch-capable app whose admin store already holds an operator selection.

    ``unpinned`` removes the ``preselected`` flag from the Energy Manager's own metrics in the
    registry fixture, so the periodic list contains them only if ``_with_energy_metric_names()``
    actually pinned them.
    """
    paths = dispatch_fixtures(tmp_path)
    if unpinned:
        registry = json.loads(Path(paths["object_registry_path"]).read_text(encoding="utf-8"))
        for entry in registry["entries"]:
            if entry["name"] in _ENERGY_METRIC_NAMES:
                entry["preselected"] = False
        Path(paths["object_registry_path"]).write_text(json.dumps(registry), encoding="utf-8")
    admin_path = tmp_path / "admin.db"
    store = AdminStore(admin_path, SECRET)
    password = store.initialize()
    assert password is not None
    assert store.change_password(password, PASSWORD)
    store.accept_disclaimer()
    store.put_many({"write_names": list(PRE_APPROVED), "exposed_names": EXPOSED})
    store.close()
    base = {
        "enable_write_support": True,
        "hmac_secret": SECRET,
        "admin_db_path": admin_path,
        # The dispatch gate blocks unverified hardware, so an integration test that drives a device
        # has to seed the verification of exactly that device explicitly.
        "dispatch_db_path": dispatch_capabilities(tmp_path, ("main", "slave1"), secret=SECRET),
        "dispatch_max_charge_power_w": 3000,
        "dispatch_max_discharge_power_w": 5000,
        "write_response_timeout_ms": 50,
    }
    return make_settings(**{**base, **paths, **overrides})


def characterization_settings(tmp_path: Path, **overrides):
    settings = energy_settings(tmp_path, **overrides)
    path = Path(settings.object_registry_path)
    registry = json.loads(path.read_text(encoding="utf-8"))
    production = json.loads((ROOT / "app/catalog/objects.json").read_text(encoding="utf-8"))
    names = {"power_mng_battery_type", "battery_ah_capacity", "battery_voltage", "battery_placeholder_0_ah_capacity",
             "battery_placeholder_0_maximum_charge_current"}
    present = {entry["name"] for entry in registry["entries"]}
    registry["entries"].extend(
        entry for entry in production["entries"] if entry["name"] in names - present
    )
    path.write_text(json.dumps(registry), encoding="utf-8")
    return settings


def seed_payloads(harness) -> None:
    catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
    values = {
        "battery_soc": float_payload(0.5),
        "grid_power": float_payload(1200),
        "battery_power": float_payload(0),
        "household_load_power": float_payload(800),
        "solar_a_power": float_payload(1500),
        "solar_b_power": float_payload(900),
        "power_mng_soc_strategy": b"\x00",
        "power_mng_soc_target_set": float_payload(0.5),
        "power_mng_battery_power_extern": float_payload(0),
        "power_mng_use_grid_power_enable": b"\x00",
    }
    for name, payload in values.items():
        harness.net.payloads[catalog.object_entry(name).object_id] = payload


async def arm(harness, device_id: str = "main", mode: str = "external") -> dict:
    """Select an operating mode through the admin surface: the public API cannot switch it.

    The default is External, the mode the PAT-driven tests below need.
    """
    headers = await admin_session(harness)
    response = await harness.client.put(
        f"/admin/api/energy/devices/{device_id}/mode", headers=headers, json={"mode": mode}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def command(harness, body: dict, device_id: str = "main"):
    return await harness.client.post(
        f"/api/v1/devices/{device_id}/energy/command", headers=WRITER, json=body
    )


async def admin_session(harness) -> dict[str, str]:
    """Log in and change nothing: the admin endpoints need a session cookie plus the CSRF header."""
    csrf = (await harness.client.get("/admin/api/session")).json()["csrf_token"]
    login = await harness.client.post(
        "/admin/api/login",
        headers={"X-CSRF-Token": csrf},
        json={"username": "admin", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    return {"X-CSRF-Token": login.json()["csrf_token"]}


def periodic_object_ids(harness, device_id: str = "main") -> tuple[int, ...]:
    periodic = harness.runtime.gateway._device(device_id).periodic
    return () if periodic is None else periodic.object_ids


def energy_object_ids(harness) -> set[int]:
    catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
    return {catalog.object_entry(name).object_id for name in _ENERGY_METRIC_NAMES}




async def test_the_task_body_charges_the_battery_to_the_target(tmp_path: Path) -> None:
    """The literal body from the task: an action and a business target SoC, nothing else."""
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        await arm(harness)
        response = await command(harness, {"action": "charge", "target_soc_percent": 80})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "charging"
        assert body["action"] == "charge"
        assert body["target_soc_percent"] == 80
        assert body["power_limit_w"] == 3000  # the configured device limit, chosen server-side
        assert body["power_limit_clamped"] is False
        assert body["commanded_direction"] == "charge"
        assert body["until"] is not None
        assert body["armed"] is True


async def test_an_explicit_power_above_the_device_limit_is_clamped(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        await arm(harness)
        response = await command(
            harness, {"action": "charge", "target_soc_percent": 80, "max_power_w": 4500}
        )
        assert response.status_code == 200, response.text
        assert response.json()["power_limit_w"] == 3000
        assert response.json()["power_limit_clamped"] is True


async def test_discharge_hold_and_auto_each_reach_their_business_state(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        await arm(harness)

        discharging = await command(harness, {"action": "discharge", "target_soc_percent": 30})
        assert discharging.status_code == 200, discharging.text
        assert discharging.json()["state"] == "discharging"
        assert discharging.json()["power_limit_w"] == 5000

        holding = await command(harness, {"action": "hold"})
        assert holding.status_code == 200, holding.text
        assert holding.json()["state"] == "holding"
        assert holding.json()["action"] == "hold"
        # A hold has no power budget; the commanded pair carries the zero instead.
        assert holding.json()["power_limit_w"] is None
        assert holding.json()["commanded_power_w"] == 0.0
        assert holding.json()["commanded_direction"] == "none"

        automatic = await command(harness, {"action": "auto"})
        assert automatic.status_code == 200, automatic.text
        assert automatic.json()["state"] == "automatic"
        assert automatic.json()["action"] is None
        assert automatic.json()["stop_reason"] == "stopped_by_operator"


async def test_auto_on_an_untouched_device_is_not_an_error(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        await arm(harness)
        response = await command(harness, {"action": "auto"})
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "automatic"


@pytest.mark.parametrize(
    "body",
    [
        {"action": "charge"},  # target required
        {"action": "hold", "target_soc_percent": 80},  # target forbidden
        {"action": "auto", "max_power_w": 1000},  # power forbidden
        {"action": "charge", "target_soc_percent": 3},  # below the hard bound
        {"action": "charge", "target_soc_percent": 99},  # above the hard bound
        {"action": "charge", "target_soc_percent": 80, "max_power_w": 0},
        {"action": "charge", "target_soc_percent": 80, "max_power_w": 50_001},
        {"action": "charge", "target_soc_percent": 80, "unexpected": 1},  # extra="forbid"
        {"action": "shutdown_everything", "target_soc_percent": 80},
    ],
)
async def test_an_invalid_command_body_is_rejected_by_the_request_model(
    tmp_path: Path, body: dict
) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        await arm(harness)
        response = await command(harness, body)
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "invalid_request"


@pytest.mark.parametrize("body", [{"mode": "true"}, {"mode": "auto"}, {"armed": True}, {}])
async def test_an_unknown_mode_is_a_422(tmp_path: Path, body: dict) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        response = await harness.client.put("/admin/api/energy/devices/main/mode", headers=headers, json=body)
        assert response.status_code == 422, response.text


async def test_the_public_api_has_no_mode_endpoint(tmp_path: Path) -> None:
    """The mode is a GUI decision: a token, even a write token, cannot switch the manager on."""
    async with running_app(energy_settings(tmp_path)) as harness:
        for headers in (WRITER, READER):
            response = await harness.client.put(
                "/api/v1/devices/main/energy/mode", headers=headers, json={"mode": "external"}
            )
            assert response.status_code == 404, response.text  # 403 would mean the route still exists
        status = await harness.client.get("/api/v1/devices/main/energy", headers=WRITER)
        assert status.json()["armed"] is False




async def test_a_device_in_mode_off_refuses_a_command(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        response = await command(harness, {"action": "charge", "target_soc_percent": 80})
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "energy_manager_off"


async def test_an_unknown_device_is_a_404(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        for response in (
            await harness.client.get("/api/v1/devices/nope/energy", headers=WRITER),
            await command(harness, {"action": "auto"}, device_id="nope"),
        ):
            assert response.status_code == 404, response.text
            assert response.json()["code"] == "unknown_device"


async def test_a_read_only_token_cannot_reach_the_energy_manager(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        for response in (
            await harness.client.get("/api/v1/devices/main/energy", headers=READER),
            await harness.client.post(
                "/api/v1/devices/main/energy/command", headers=READER, json={"action": "auto"}
            ),
        ):
            assert response.status_code == 403, response.text
            assert response.json()["code"] == "insufficient_scope"


async def test_without_write_support_the_public_router_is_absent_and_the_admin_surface_refuses(
    tmp_path: Path,
) -> None:
    """AC-10: the public endpoints answer write_disabled, while the admin surface explains why."""
    settings = energy_settings(tmp_path, enable_write_support=False)
    async with running_app(settings) as harness:
        for response in (
            await harness.client.get("/api/v1/devices/main/energy", headers=WRITER),
            await command(harness, {"action": "auto"}),
        ):
            assert response.status_code == 404, response.text
            assert response.json()["code"] == "write_disabled"

        headers = await admin_session(harness)
        arming = await harness.client.put(
            "/admin/api/energy/devices/main/mode", headers=headers, json={"mode": "external"}
        )
        assert arming.status_code == 409, arming.text
        assert arming.json()["code"] == "energy_write_support_required"
        commanding = await harness.client.post(
            "/admin/api/energy/devices/main/command",
            headers=headers,
            json={"action": "charge", "target_soc_percent": 80},
        )
        assert commanding.status_code == 503, commanding.text
        assert commanding.json()["code"] == "dispatch_store_unavailable"


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [(b"\x01", 1), (b"\x7f", 127), (None, None)],
)
async def test_hardware_soc_strategy_is_cache_only_and_available_without_dispatch(
    tmp_path: Path, payload: bytes | None, expected_code: int | None
) -> None:
    async with running_app(energy_settings(tmp_path, enable_write_support=False)) as harness:
        seed_payloads(harness)
        headers = await admin_session(harness)
        gateway = harness.runtime.gateway
        if payload is not None:
            strategy_id = RegistryCatalog.from_file(
                harness.runtime.settings.object_registry_path
            ).object_entry("power_mng_soc_strategy").object_id
            harness.net.payloads[strategy_id] = payload
            # An explicit read warms the cache. The UI endpoint itself must not perform this read.
            await gateway.read_metric("main", "power_mng_soc_strategy", fresh=False)
        before = gateway.device_status("main").transactions

        response = await harness.client.get("/admin/api/energy/hardware-strategies", headers=headers)

        assert response.status_code == 200, response.text
        main = next(item for item in response.json() if item["device_id"] == "main")
        assert main["code"] == expected_code
        assert main["label"] is None
        assert gateway.device_status("main").transactions == before
        assert harness.runtime.dispatch is None
        assert harness.runtime.energy.mode("main") is EnergyMode.OFF


async def test_hardware_soc_strategy_stale_cache_is_na_without_device_read(tmp_path: Path) -> None:
    async with running_app(
        energy_settings(tmp_path, enable_write_support=False, cache_ttl_seconds=0.0)
    ) as harness:
        seed_payloads(harness)
        headers = await admin_session(harness)
        gateway = harness.runtime.gateway
        await gateway.read_metric("main", "power_mng_soc_strategy", fresh=False)
        await asyncio.sleep(0.01)  # ttl=0: cached value is stale/grace, not fresh
        before = gateway.device_status("main").transactions

        response = await harness.client.get("/admin/api/energy/hardware-strategies", headers=headers)

        assert response.status_code == 200, response.text
        main = next(item for item in response.json() if item["device_id"] == "main")
        assert main["code"] is None
        assert main["label"] is None
        assert gateway.device_status("main").transactions == before


async def test_battery_characterization_is_authorized_cache_only_and_never_derives_wh(tmp_path: Path) -> None:
    async with running_app(characterization_settings(tmp_path, enable_write_support=False, enable_periodic_reads=False)) as harness:
        path = "/admin/api/energy/devices/main/battery-characterization"
        denied = await harness.client.get(path)
        assert denied.status_code == 401
        headers = await admin_session(harness)
        before = harness.runtime.gateway.device_status("main").transactions
        frames = len(harness.net.frames)

        response = await harness.client.get(path, headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["device_id"] == "main"
        assert "usable_capacity_wh" not in body
        metrics = {item["name"]: item for item in body["metrics"]}
        assert set(metrics) == {
            "power_mng_n_batteries", "power_mng_battery_type", "power_mng_amp_hours_measured",
            "battery_ah_capacity", "battery_placeholder_0_ah_capacity", "battery_soh", "battery_voltage",
            "battery_maximum_charge_current", "battery_maximum_discharge_current",
            "battery_placeholder_0_current", "battery_placeholder_0_maximum_charge_current",
            "battery_placeholder_0_maximum_discharge_current",
        }
        assert metrics["battery_ah_capacity"] == {
            "name": "battery_ah_capacity", "value": None, "unit": "Ah", "age_seconds": None,
            "status": "unavailable",
        }
        assert metrics["battery_placeholder_0_maximum_charge_current"]["unit"] == "A"
        assert [tower["id"] for tower in body["towers"]] == ["battery", "battery_placeholder_0"]
        assert all(tower["module_count_status"] == "pending" for tower in body["towers"])
        assert harness.runtime.gateway.device_status("main").transactions == before
        assert len(harness.net.frames) == frames


def _calibration_sample(serial: str = "SN-A", bms: str | None = "BMS-1", at: datetime | None = None, ah: float = 14.0):
    return CapacityCalibrationSample(
        device_id="main", inverter_serial=serial, battery_bms_sn=bms,
        measured_at=at or datetime(2026, 10, 16, 12, tzinfo=UTC), battery_cycles=128,
        reference_ah=17.0, measured_ah=ah, rated_usable_capacity_wh=10368.0,
        current_usable_capacity_wh=8538.35, remaining_capacity_percent=82.35,
        module_counts=(6,), source_contract="bms_ah_ratio_v1",
    )


async def test_battery_calibration_history_is_authorized_local_only_and_bounded(tmp_path: Path) -> None:
    settings = characterization_settings(tmp_path, enable_write_support=False, enable_periodic_reads=False)
    async with running_app(settings) as harness:
        path = "/admin/api/energy/devices/main/battery-calibration-history"
        assert (await harness.client.get(path)).status_code == 401
        headers = await admin_session(harness)
        store = harness.app.state.admin_store
        store.record_battery_calibration(_calibration_sample())
        before = harness.runtime.gateway.device_status("main").transactions
        frames = len(harness.net.frames)

        # Without a known inverter serial nothing is returned: there is no device_id fallback.
        unknown = (await harness.client.get(path, headers=headers)).json()
        assert unknown["inverter_serial"] is None and unknown["points"] == [] and unknown["baseline"] is None
        assert unknown["baseline_unavailable_reason"] == "inverter_serial_unknown"

        harness.runtime.gateway.set_reported_serial("main", "SN-A")
        response = await harness.client.get(path, headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["device_id"], body["inverter_serial"], body["battery_bms_sn"]) == ("main", "SN-A", "BMS-1")
        assert body["battery_replaced"] is False
        point = body["points"][0]
        assert point["battery_cycles"] == 128
        assert point["current_usable_capacity_wh"] == pytest.approx(8538.35)
        assert point["remaining_capacity_percent"] == pytest.approx(82.35)
        assert point["module_counts"] == [6]
        assert body["baseline"] is None and body["commissioning"] is None
        assert body["baseline_unavailable_reason"] == "no_commissioning_date"
        assert (await harness.client.get(path + "?limit=5001", headers=headers)).status_code == 422
        assert (await harness.client.get("/admin/api/energy/devices/missing/battery-calibration-history", headers=headers)).status_code == 404
        assert harness.runtime.gateway.device_status("main").transactions == before
        assert len(harness.net.frames) == frames


async def test_battery_calibration_baseline_shape_and_manual_override(tmp_path: Path) -> None:
    settings = characterization_settings(tmp_path, enable_write_support=False, enable_periodic_reads=False)
    async with running_app(settings) as harness:
        path = "/admin/api/energy/devices/main/battery-calibration-history"
        commissioning = "/admin/api/energy/devices/main/battery-commissioning"
        headers = await admin_session(harness)
        store = harness.app.state.admin_store
        harness.runtime.gateway.set_reported_serial("main", "SN-A")
        store.record_battery_calibration(_calibration_sample())
        store.set_inverter_commissioning("SN-A", datetime(2021, 5, 1, tzinfo=UTC), "logger_derived")

        body = (await harness.client.get(path, headers=headers)).json()
        assert body["baseline_unavailable_reason"] is None
        assert body["baseline"] == {
            "date": "2021-05-01T00:00:00Z", "battery_cycles": 0, "rated_usable_capacity_wh": 10368.0,
            "current_usable_capacity_wh": 10368.0, "remaining_capacity_percent": 100.0, "source": "logger_derived",
        }
        assert body["commissioning"]["source"] == "logger_derived"
        assert len(body["points"]) == 1  # the baseline is not inserted as a measurement row

        assert (await harness.client.put(commissioning, json={"commissioned_on": "2021-06-15"})).status_code in (401, 403)
        put = await harness.client.put(commissioning, json={"commissioned_on": "2021-06-15"}, headers=headers)
        assert put.status_code == 200, put.text
        assert put.json()["baseline"]["date"].startswith("2021-06-15") and put.json()["baseline"]["source"] == "manual"
        for bad in ("2009-12-31", "2999-01-01"):
            rejected = await harness.client.put(commissioning, json={"commissioned_on": bad}, headers=headers)
            assert rejected.status_code == 422
        deleted = await harness.client.delete(commissioning, headers=headers)
        assert deleted.json()["baseline"]["source"] == "logger_derived"

        # A swapped battery: the derived date belongs to the first battery, so no baseline for the new series.
        store.record_battery_calibration(_calibration_sample(bms="BMS-2", at=datetime(2026, 11, 1, tzinfo=UTC)))
        replaced = (await harness.client.get(path, headers=headers)).json()
        assert replaced["battery_replaced"] is True and replaced["baseline"] is None
        assert replaced["baseline_unavailable_reason"] == "battery_replaced"
        manual = await harness.client.put(commissioning, json={"commissioned_on": "2022-01-01"}, headers=headers)
        assert manual.json()["baseline"]["source"] == "manual"


async def test_commissioning_override_needs_a_known_serial(tmp_path: Path) -> None:
    settings = characterization_settings(tmp_path, enable_write_support=False, enable_periodic_reads=False)
    async with running_app(settings) as harness:
        headers = await admin_session(harness)
        response = await harness.client.put(
            "/admin/api/energy/devices/main/battery-commissioning", json={"commissioned_on": "2021-06-15"}, headers=headers
        )
        assert response.status_code == 409


async def test_calibration_is_recorded_while_the_energy_manager_is_disabled(tmp_path: Path) -> None:
    """The producer path has no dispatch dependency: write support off still records every calibration."""
    from types import SimpleNamespace

    from app.api.app_factory import _inventory_loop
    from app.gateway.base import DeviceState

    settings = characterization_settings(tmp_path, enable_write_support=False, enable_periodic_reads=False)
    async with running_app(settings) as harness:
        runtime = harness.runtime
        assert runtime.dispatch is None  # the Energy Manager is off
        assert isinstance(runtime.battery_capacity, MeasuredBatteryCapacity)
        store = harness.app.state.admin_store
        stub = SimpleNamespace(
            calibration_sample=lambda device_id, at: _calibration_sample(at=at)
        )

        class StopLoop(Exception):
            pass

        class LoopClock:
            elapsed = 0.0

            def monotonic(self):
                return self.elapsed

            def now(self):
                return datetime(2026, 10, 16, tzinfo=UTC)

            async def sleep(self, seconds):
                self.elapsed += seconds
                if self.elapsed > 15:
                    raise StopLoop

        async def read_system(device_id, name):
            return None

        async def inventory(*_args):
            return 0

        gateway = SimpleNamespace(
            read_module_inventory=inventory,
            device_status=lambda _d: SimpleNamespace(state=DeviceState.OK, queue_length=0),
            read_system=read_system,
        )

        async def persist(sample):
            await asyncio.to_thread(store.record_battery_calibration, sample)

        with pytest.raises(StopLoop):
            await _inventory_loop(
                gateway, "main", LoopClock(), capacity_ah_names=("battery_ah_capacity",),
                capacity_source=stub, on_capacity_calibration=persist,
            )
        assert [p["measured_ah"] for p in store.get_battery_calibration_history("SN-A")["points"]] == [14.0]


async def test_battery_characterization_refresh_is_bounded_to_requested_numeric_reads(tmp_path: Path) -> None:
    async with running_app(characterization_settings(tmp_path, enable_write_support=False, enable_periodic_reads=False)) as harness:
        path = "/admin/api/energy/devices/main/battery-characterization"
        catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
        harness.net.payloads[catalog.object_entry("power_mng_battery_type").object_id] = b"\x02"
        harness.net.payloads[catalog.object_entry("battery_ah_capacity").object_id] = float_payload(180.0)
        harness.net.payloads[catalog.object_entry("battery_voltage").object_id] = float_payload(52.0)
        read_pat = harness.app.state.admin_store.create_token("diagnostics", "read", None)[1]
        pat_response = await harness.client.post(
            f"{path}/refresh", headers={"Authorization": f"Bearer {read_pat}"},
            json={"names": ["power_mng_battery_type"]},
        )
        assert pat_response.status_code == 200, pat_response.text
        pat_metrics = {item["name"]: item for item in pat_response.json()["metrics"]}
        assert pat_metrics["power_mng_battery_type"]["value"] == 2
        headers = await admin_session(harness)
        before_denied = harness.runtime.gateway.device_status("main").transactions
        csrf_denied = await harness.client.post(
            f"{path}/refresh", headers={"X-CSRF-Token": "invalid"}, json={"names": ["battery_voltage"]}
        )
        assert csrf_denied.status_code == 403
        assert harness.runtime.gateway.device_status("main").transactions == before_denied
        before = harness.runtime.gateway.device_status("main").transactions
        frames = len(harness.net.frames)

        response = await harness.client.post(
            f"{path}/refresh", headers=headers,
            json={"names": ["battery_ah_capacity", "battery_voltage"]},
        )

        assert response.status_code == 200, response.text
        metrics = {item["name"]: item for item in response.json()["metrics"]}
        assert metrics["power_mng_battery_type"]["value"] == 2
        assert metrics["power_mng_battery_type"]["status"] == "fresh"
        assert metrics["battery_ah_capacity"]["value"] == pytest.approx(180.0)
        assert metrics["battery_ah_capacity"]["status"] == "fresh"
        assert metrics["battery_ah_capacity"]["age_seconds"] is not None
        assert metrics["battery_voltage"]["value"] == pytest.approx(52.0)
        assert harness.runtime.gateway.device_status("main").transactions - before <= 2
        cached = await harness.client.get(path, headers=headers)
        assert cached.status_code == 200, cached.text
        cached_metrics = {item["name"]: item for item in cached.json()["metrics"]}
        assert cached_metrics["power_mng_battery_type"]["value"] == 2
        assert not any(frame.command in WRITE_COMMANDS for _, frame in harness.net.frames[frames:])
        refused = await harness.client.post(f"{path}/refresh", headers=headers, json={"names": ["battery_module_sn_0"]})
        assert refused.status_code == 422
        too_many = await harness.client.post(f"{path}/refresh", headers=headers, json={
            "names": ["battery_voltage", "battery_ah_capacity", "battery_soh",
                      "battery_maximum_charge_current", "battery_maximum_discharge_current"],
        })
        assert too_many.status_code == 422
        assert not any(frame.command in WRITE_COMMANDS for _, frame in harness.net.frames[frames:])


async def test_battery_characterization_reports_stale_age_without_refreshing(tmp_path: Path) -> None:
    async with running_app(characterization_settings(
        tmp_path, enable_write_support=False, enable_periodic_reads=False, cache_ttl_seconds=0.0
    )) as harness:
        path = "/admin/api/energy/devices/main/battery-characterization"
        catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
        harness.net.payloads[catalog.object_entry("battery_voltage").object_id] = float_payload(52.0)
        await harness.runtime.gateway.read_metric("main", "battery_voltage", fresh=False)
        await asyncio.sleep(0.01)
        headers = await admin_session(harness)
        before = harness.runtime.gateway.device_status("main").transactions

        response = await harness.client.get(path, headers=headers)

        assert response.status_code == 200, response.text
        metric = next(item for item in response.json()["metrics"] if item["name"] == "battery_voltage")
        assert metric["value"] == pytest.approx(52.0)
        assert metric["status"] == "stale"
        assert metric["age_seconds"] > 0
        assert harness.runtime.gateway.device_status("main").transactions == before


async def test_battery_characterization_refresh_never_probes_unconfirmed_second_tower(tmp_path: Path) -> None:
    async with running_app(characterization_settings(tmp_path, enable_write_support=False, enable_periodic_reads=False)) as harness:
        path = "/admin/api/energy/devices/main/battery-characterization/refresh"
        headers = await admin_session(harness)
        before = harness.runtime.gateway.device_status("main").transactions
        frames = len(harness.net.frames)

        response = await harness.client.post(
            path, headers=headers, json={"names": ["battery_placeholder_0_ah_capacity"]}
        )

        assert response.status_code == 200, response.text
        metric = next(item for item in response.json()["metrics"] if item["name"] == "battery_placeholder_0_ah_capacity")
        assert metric["status"] == "unavailable"
        assert harness.runtime.gateway.device_status("main").transactions == before
        assert not any(frame.command in WRITE_COMMANDS for _, frame in harness.net.frames[frames:])


async def test_revoking_the_register_approvals_disables_the_actions(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        await arm(harness)
        # What the Inverters page does when an operator clears the selection.
        harness.app.state.admin_store.put("write_names", [])
        status = await harness.client.get("/api/v1/devices/main/energy", headers=WRITER)
        assert status.status_code == 200, status.text
        # The handback needs no write approval, so `auto` stays available.
        by_action = {item["action"]: item for item in status.json()["actions"]}
        assert by_action["auto"]["available"] is True
        assert {item["reason"] for name, item in by_action.items() if name != "auto"} == {
            "write_not_permitted"
        }
        refused = await command(harness, {"action": "charge", "target_soc_percent": 80})
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "energy_action_unavailable"




async def test_the_status_get_serves_the_whole_card_in_one_call(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        # One ordinary read per figure fills the cache the readings are served from.
        names = ",".join(_ENERGY_METRIC_NAMES)
        filled = await harness.client.get(
            f"/api/v1/devices/main/metrics?names={names}", headers=WRITER
        )
        assert filled.status_code == 200, filled.text

        response = await harness.client.get("/api/v1/devices/main/energy", headers=WRITER)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "automatic" and body["action"] is None
        assert body["power_limit_w"] is None and body["commanded_power_w"] == 0.0
        assert body["target_soc_window"] == {"min": 7.0, "max": 95.0}
        readings = body["readings"]
        assert readings["battery_soc_percent"]["value"] == pytest.approx(50.0)
        assert readings["grid_power_w"]["value"] == pytest.approx(1200.0)
        # Measured battery power (not the commanded one) is published next to the other figures.
        assert "battery_power_w" in readings
        assert readings["pv_power_w"]["value"] == pytest.approx(2400.0)
        assert readings["house_load_w"]["value"] == pytest.approx(800.0)
        assert all(not readings[name]["stale"] for name in readings)
        assert [item["action"] for item in body["actions"]] == ["charge", "discharge", "hold", "auto"]


async def test_a_device_without_any_cached_reading_answers_with_five_absent_figures(
    tmp_path: Path,
) -> None:
    """AC-21/AC-24: absent is a published state, not an error — and never a device read."""
    async with running_app(energy_settings(tmp_path, enable_periodic_reads=False)) as harness:
        response = await harness.client.get("/api/v1/devices/slave1/energy", headers=WRITER)
        assert response.status_code == 200, response.text
        readings = response.json()["readings"]
        assert len(readings) == 5
        for reading in readings.values():
            assert reading == {"value": None, "age_seconds": None, "stale": True}




def fake_request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "root_path": "",
        }
    )


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("PUT", "/api/v1/devices/main/metrics/battery_target", True),
        ("POST", "/api/v1/devices/main/actions/com_service", True),
        ("POST", "/api/v1/devices/main/battery/dispatch", True),
        ("GET", "/api/v1/devices/main/battery/dispatch", True),
        ("DELETE", "/api/v1/devices/main/battery/dispatch", True),
        ("GET", "/api/v1/devices/main/energy", True),
        ("POST", "/api/v1/devices/main/energy/command", True),
        ("PUT", "/api/v1/devices/main/energy/mode", False),  # no public mode switch
        # Short paths must return False, not raise: this runs inside the global exception handler,
        # where an IndexError would turn a plain 404 into an unhandled 500.
        ("GET", "/api/v1/devices", False),
        ("GET", "/api/v1/devices/main", False),
        ("GET", "/api/v1", False),
        ("GET", "/", False),
        ("GET", "/api/v1/devices/main/energy/unknown", False),
        ("GET", "/api/v1/devices/main/metrics/battery_target", False),  # wrong method
        ("GET", "/api/v1/devices/main/nothing", False),
    ],
)
def test_is_write_path_matches_method_and_path_exactly(
    method: str, path: str, expected: bool
) -> None:
    assert _is_write_path(fake_request(method, path)) is expected


async def test_a_404_on_a_short_path_stays_a_plain_404_without_write_support() -> None:
    async with running_app(make_settings(enable_write_support=False)) as harness:
        for path in ("/api/v1/devices/main", "/api/v1/devices/main/nothing"):
            response = await harness.client.get(path, headers=WRITER)
            assert response.status_code == 404, response.text
            assert response.json()["code"] == "not_found"




async def test_the_shutdown_drain_refuses_commands_on_both_surfaces_but_still_reports(
    tmp_path: Path,
) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        headers = await admin_session(harness)
        harness.runtime.shutdown.plan = ShutdownPlan(harness.runtime.clock.now(), 0.0, 0.0)
        try:
            refused = [
                await command(harness, {"action": "charge", "target_soc_percent": 80}),
                await harness.client.post(
                    "/admin/api/energy/devices/main/command",
                    headers=headers,
                    json={"action": "charge", "target_soc_percent": 80},
                ),
                await harness.client.put(
                    "/admin/api/energy/devices/main/mode", headers=headers, json={"mode": "external"}
                ),
            ]
            for response in refused:
                assert response.status_code == 503, response.text
                assert response.json()["code"] == "not_ready"
            status = await harness.client.get("/api/v1/devices/main/energy", headers=WRITER)
            assert status.status_code == 200, status.text
        finally:
            harness.runtime.shutdown.plan = None




async def test_the_energy_metrics_are_pinned_at_startup_and_after_a_device_list_change(
    tmp_path: Path,
) -> None:
    """The reconfiguration path runs on every Inverters-page save; extending only create_app() would
    make the card go dark on the next settings change."""
    settings = energy_settings(tmp_path)
    async with running_app(settings) as harness:
        wanted = energy_object_ids(harness)
        assert wanted <= set(periodic_object_ids(harness))

        headers = await admin_session(harness)
        current = (await harness.client.get("/admin/api/settings", headers=headers)).json()
        devices = current["settings"]["devices"]
        saved = await harness.client.put(
            "/admin/api/settings",
            headers=headers,
            json={"devices": [*devices, {"host": HOST, "port": PORT, "network_id": 1234567}]},
        )
        assert saved.status_code == 200, saved.text
        assert wanted <= set(periodic_object_ids(harness))


async def test_without_dispatch_the_flow_metrics_are_still_pinned(tmp_path: Path) -> None:
    # The dashboard flow graphic reads the same cache-only figures, so a narrowed exposed list must
    # not drop them when write support is off.
    settings = energy_settings(tmp_path, enable_write_support=False)
    async with running_app(settings) as harness:
        catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
        pinned = {catalog.object_entry(name).object_id for name in _ENERGY_METRIC_NAMES}
        assert pinned <= set(periodic_object_ids(harness))


def test_the_periodic_cap_prefers_flow_metrics_over_card_and_module_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = energy_settings(tmp_path, enable_write_support=False)
    catalog = RegistryCatalog.from_file(settings.object_registry_path)
    filler = [
        e.name for e in catalog.entries()
        if e.name not in _ENERGY_METRIC_NAMES and is_numeric(e.value_type)
    ]
    # Room for exactly the flow metrics on top of the selection: nothing else may squeeze in.
    cap = len(filler) + len(_ENERGY_METRIC_NAMES)
    monkeypatch.setattr("app.api.app_factory.MAX_PERIODIC_PER_DEVICE", cap)
    names = _effective_periodic_names(settings, catalog, filler)
    assert len(names) == cap
    assert set(_ENERGY_METRIC_NAMES) <= set(names)
    assert not any("module_sn" in n for n in names)


def test_a_fully_used_periodic_budget_still_pins_the_flow_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = energy_settings(tmp_path, enable_write_support=False)
    catalog = RegistryCatalog.from_file(settings.object_registry_path)
    filler = [
        e.name for e in catalog.entries()
        if e.name not in _ENERGY_METRIC_NAMES and is_numeric(e.value_type)
    ]
    # The user selection alone fills the cap exactly, so no room is left for the flow metrics.
    monkeypatch.setattr("app.api.app_factory.MAX_PERIODIC_PER_DEVICE", len(filler))
    names = _effective_periodic_names(settings, catalog, filler)
    assert len(names) == len(filler)
    assert set(_ENERGY_METRIC_NAMES) <= set(names)
    assert names[: len(filler) - len(_ENERGY_METRIC_NAMES)] == filler[: len(filler) - len(_ENERGY_METRIC_NAMES)]


async def test_with_periodic_reads_disabled_the_list_is_empty(tmp_path: Path) -> None:
    settings = energy_settings(tmp_path, enable_periodic_reads=False)
    async with running_app(settings) as harness:
        assert periodic_object_ids(harness) == ()
        status = await harness.client.get("/api/v1/devices/main/energy", headers=WRITER)
        assert status.status_code == 200, status.text
        assert all(item["stale"] for item in status.json()["readings"].values())




async def test_the_admin_surface_requires_a_session_and_the_csrf_header(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        assert (await harness.client.get("/admin/api/energy/devices")).status_code == 401
        headers = await admin_session(harness)
        assert (await harness.client.get("/admin/api/energy/devices")).status_code == 200
        # A mutation without the CSRF header is refused even with a valid session.
        without_csrf = await harness.client.put(
            "/admin/api/energy/devices/main/mode", json={"mode": "external"}
        )
        assert without_csrf.status_code == 403, without_csrf.text
        with_csrf = await harness.client.put(
            "/admin/api/energy/devices/main/mode", headers=headers, json={"mode": "external"}
        )
        assert with_csrf.status_code == 200, with_csrf.text


async def test_admin_arming_adds_the_register_approvals_add_only(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        headers = await admin_session(harness)
        assert harness.app.state.admin_store.get("write_names") == list(PRE_APPROVED)
        armed = await harness.client.put(
            "/admin/api/energy/devices/main/mode", headers=headers, json={"mode": "external"}
        )
        assert armed.status_code == 200, armed.text
        names = harness.app.state.admin_store.get("write_names")
        # Add-only and order-preserving: the operator's own entry stays first.
        assert names[: len(PRE_APPROVED)] == list(PRE_APPROVED)
        assert set(names) == set(DISPATCH_WRITE_NAMES)
        assert armed.json()["mode_changed_by"] == "admin"
        assert armed.json()["mode_changed_at"] is not None
        assert set(armed.json()["added_write_names"]) == set(DISPATCH_WRITE_NAMES) - set(PRE_APPROVED)

        disarmed = await harness.client.put(
            "/admin/api/energy/devices/main/mode", headers=headers, json={"mode": "off"}
        )
        assert disarmed.status_code == 200, disarmed.text
        assert disarmed.json()["armed"] is False
        # Non-destructive: disarming revokes no approval.
        assert harness.app.state.admin_store.get("write_names") == names


async def test_admin_status_carries_state_independent_approved_write_names(tmp_path: Path) -> None:
    """``approved_write_names`` is the live write allowlist, authoritative in every arm state —
    unlike ``added_write_names``, which only records what arming itself contributed. With all four
    required writes approved up front but the device never armed, the Setup checklist's
    ``REQUIRED_WRITES ⊆ approved_write_names`` must hold while ``added_write_names`` stays empty
    (the case the old field got wrong). Design §4.4 / §12 / §16 item 3."""
    settings = energy_settings(tmp_path)
    async with running_app(settings) as harness:
        # Approve all four required writes on the admin store before any arming.
        harness.app.state.admin_store.put("write_names", list(DISPATCH_WRITE_NAMES))
        headers = await admin_session(harness)
        response = await harness.client.get("/admin/api/energy/devices", headers=headers)
        assert response.status_code == 200, response.text
        main = next(item for item in response.json() if item["device_id"] == "main")
        assert "approved_write_names" in main
        # Never armed: added_write_names is empty, but approved_write_names covers the requirements.
        assert main["armed"] is False
        assert main["added_write_names"] == []
        assert set(DISPATCH_WRITE_NAMES) <= set(main["approved_write_names"])


async def test_required_dispatch_registers_cannot_be_revoked_while_a_device_is_armed(tmp_path: Path) -> None:
    """A restore writes those registers through the allowlist, so revoking them under an armed
    device would leave the handback rejected forever."""
    async with running_app(energy_settings(tmp_path)) as harness:
        await arm(harness)
        headers = await admin_session(harness)
        exposed = harness.app.state.admin_store.get("exposed_names")
        refused = await harness.client.put(
            "/admin/api/parameters", headers=headers, json={"exposed_names": exposed, "write_names": []}
        )
        assert refused.status_code == 409, refused.text
        assert set(harness.app.state.admin_store.get("write_names")) == set(DISPATCH_WRITE_NAMES)


async def test_the_admin_status_carries_the_raw_gate_detail_and_the_policy(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        del headers
        devices = await harness.client.get("/admin/api/energy/devices")
        assert devices.status_code == 200, devices.text
        entry = next(item for item in devices.json() if item["device_id"] == "main")
        assert [gate["action"] for gate in entry["gates"]] == ["charge", "discharge", "hold", "auto"]
        auto = next(gate for gate in entry["gates"] if gate["action"] == "auto")
        assert auto == {
            "action": "auto",
            "allowed": True,
            "engineering_mode": False,
            "unverified": [],
            "reject_detail": None,
        }
        assert entry["soc_target_policy"]["mode"] == "business_target"
        assert "readings" in entry  # the GUI needs exactly one poll per cycle
        # Header block of the GUI: a name, the reachable flag and the host come with the same poll.
        assert entry["device_name"]
        assert entry["host"] == HOST
        assert isinstance(entry["connected"], bool)
        assert entry["limits"]["max_charge_power_w"] == 3000
        assert {item["name"] for item in entry["capabilities"]} >= {
            "write_path_convention",
            "battery_power_sign_convention",
            "grid_power_sign_convention",
        }

        public = await harness.client.get("/api/v1/devices/main/energy", headers=WRITER)
        assert public.status_code == 200, public.text
        for admin_only in ("gates", "soc_target_policy", "added_write_names", "mode_changed_by"):
            assert admin_only not in public.json()


VERIFICATION = {
    "verified_device_model": "RCT Power DC 10.0",
    "verified_firmware": "2.3.5687",
    "note": "H-1 probe, strategy code measured",
    "soc_strategy_external_code": 2,
    "enum_byte_width": 1,
    "bool_byte_width": 1,
    "write_frame_layout_verified": True,
    "apply_sequence_verified": True,
    "battery_discharge_positive": False,
    "grid_import_positive": False,
    "soc_target_unit": "ratio",
}
_VERIFICATION_NAMES = ("write_path_convention", "battery_power_sign_convention", "grid_power_sign_convention")
VERIFICATION_URL = "/admin/api/energy/devices/main/hardware-verification"


def _capabilities(entry: dict) -> dict[str, dict]:
    return {item["name"]: item for item in entry["capabilities"]}


async def test_hardware_verification_is_atomic_and_round_trips_its_evidence(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        # The fixture ships verified hardware; start from an unverified one.
        await harness.client.delete(VERIFICATION_URL, headers=headers)
        before = (await harness.client.get("/admin/api/energy/devices")).json()
        assert {c["status"] for n, c in _capabilities(before[0]).items() if n in _VERIFICATION_NAMES} == {"unverified"}
        # A missing attestation refuses the whole request and verifies nothing.
        refused = await harness.client.put(
            VERIFICATION_URL, headers=headers, json={**VERIFICATION, "apply_sequence_verified": False}
        )
        assert refused.status_code == 400, refused.text
        after_refusal = (await harness.client.get("/admin/api/energy/devices")).json()
        assert _capabilities(after_refusal[0]) == _capabilities(before[0])

        saved = await harness.client.put(VERIFICATION_URL, headers=headers, json=VERIFICATION)
        assert saved.status_code == 200, saved.text
        caps = _capabilities(saved.json())
        assert {caps[name]["status"] for name in _VERIFICATION_NAMES} == {"verified"}
        write = caps["write_path_convention"]
        # The flags the form prefills from must come back, or a reload loses them.
        assert write["write_frame_layout_verified"] is True and write["apply_sequence_verified"] is True
        assert write["soc_strategy_external_code"] == 2 and write["note"] == VERIFICATION["note"]
        assert caps["battery_power_sign_convention"]["battery_discharge_positive"] is False
        assert caps["grid_power_sign_convention"]["grid_import_positive"] is False


async def test_revoking_the_verification_changes_only_the_status(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        await harness.client.put(VERIFICATION_URL, headers=headers, json=VERIFICATION)
        revoked = await harness.client.delete(VERIFICATION_URL, headers=headers)
        assert revoked.status_code == 200, revoked.text
        caps = _capabilities(revoked.json())
        assert {caps[name]["status"] for name in _VERIFICATION_NAMES} == {"unverified"}
        write = caps["write_path_convention"]
        assert write["soc_strategy_external_code"] == 2 and write["enum_byte_width"] == 1
        assert write["write_frame_layout_verified"] is True and write["note"] == VERIFICATION["note"]
        assert caps["battery_power_sign_convention"]["battery_discharge_positive"] is False
        assert caps["grid_power_sign_convention"]["grid_import_positive"] is False
        # The evidence survives, so the same form can verify again without retyping.
        again = await harness.client.put(VERIFICATION_URL, headers=headers, json=VERIFICATION)
        assert again.status_code == 200, again.text


async def test_hardware_verification_keeps_a_note_already_stored_on_a_sign_record(tmp_path: Path) -> None:
    """The form has no note field for the sign records, so a verification must not erase theirs."""
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        seeded = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/battery_power_sign_convention",
            headers=headers,
            json={
                "status": "unverified",
                "battery_discharge_positive": True,
                "note": "clamp meter on PV string, 2026-01-04",
            },
        )
        assert seeded.status_code == 200, seeded.text

        saved = await harness.client.put(VERIFICATION_URL, headers=headers, json=VERIFICATION)
        assert saved.status_code == 200, saved.text
        caps = _capabilities(saved.json())
        assert caps["battery_power_sign_convention"]["note"] == "clamp meter on PV string, 2026-01-04"
        # The body note documents the write path; it must not spread onto a sign record.
        assert caps["write_path_convention"]["note"] == VERIFICATION["note"]
        assert caps["grid_power_sign_convention"]["note"] is None


async def test_hardware_verification_drops_a_sign_note_recorded_under_different_hardware(
    tmp_path: Path,
) -> None:
    """A sign-record note observed under one model/firmware must not be re-stamped as if it were
    evidence for a different one the next verification attests (release-blocking finding)."""
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        # "unverified" on purpose (AC-20: no test may write a verified record directly) — the
        # model/firmware mismatch this test exercises does not depend on the record's own status.
        seeded = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/battery_power_sign_convention",
            headers=headers,
            json={
                "status": "unverified",
                "battery_discharge_positive": True,
                "note": "measured on firmware 1.0",
                "verified_device_model": "RCT Power DC 8.0",
                "verified_firmware": "1.0.0",
            },
        )
        assert seeded.status_code == 200, seeded.text

        # Re-verify under a different model/firmware: the old note must not survive the carry-over.
        saved = await harness.client.put(VERIFICATION_URL, headers=headers, json=VERIFICATION)
        assert saved.status_code == 200, saved.text
        caps = _capabilities(saved.json())
        assert caps["battery_power_sign_convention"]["note"] is None
        assert caps["battery_power_sign_convention"]["verified_device_model"] == VERIFICATION["verified_device_model"]
        assert caps["battery_power_sign_convention"]["verified_firmware"] == VERIFICATION["verified_firmware"]


async def test_hardware_verification_needs_a_session_and_validates_the_body(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        # The default client carries a bearer PAT and no session cookie. This privileged route is
        # session-only (SEC-01), so a PAT is refused outright with 403 before the token is even
        # checked, rather than falling through to the generic 401 login prompt.
        assert (await harness.client.put(VERIFICATION_URL, json=VERIFICATION)).status_code == 403
        headers = await admin_session(harness)
        for bad in ({"enum_byte_width": 9}, {"note": ""}, {"battery_discharge_positive": "yes"}):
            response = await harness.client.put(VERIFICATION_URL, headers=headers, json={**VERIFICATION, **bad})
            assert response.status_code == 422, (bad, response.text)
        unknown = await harness.client.put(
            "/admin/api/energy/devices/nope/hardware-verification", headers=headers, json=VERIFICATION
        )
        assert unknown.status_code == 404


async def test_the_soc_target_policy_round_trips_through_the_admin_surface(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        saved = await harness.client.put(
            "/admin/api/energy/devices/main/soc-target-policy",
            headers=headers,
            json={"mode": "below_current_soc", "below_margin_percent": 7.5, "note": "H-1 probe"},
        )
        assert saved.status_code == 200, saved.text
        read_back = await harness.client.get("/admin/api/energy/devices/main/soc-target-policy")
        assert read_back.json() == {
            "mode": "below_current_soc",
            "below_margin_percent": 7.5,
            "note": "H-1 probe",
        }
        refused = await harness.client.put(
            "/admin/api/energy/devices/main/soc-target-policy",
            headers=headers,
            json={"mode": "below_current_soc", "below_margin_percent": 80},
        )
        assert refused.status_code == 422, refused.text




@pytest.mark.parametrize("vendor", [True, False])
def test_the_public_document_names_no_vendor_internal(tmp_path: Path, vendor: bool) -> None:
    """AC-19. ``byte_width`` is asserted within the energy and dispatch schemas only: the vendor
    diagnostics router legitimately publishes ``effective_byte_width``, so a document-wide scan
    would pass only by virtue of that flag's default and fail falsely once a harness turns it on.
    """
    settings = energy_settings(tmp_path, enable_vendor_diagnostics=vendor, docs_public=True)
    document = create_app(settings).openapi()
    whole = json.dumps(document)
    for forbidden in ("power_mng", "soc_strategy", "discharge_positive"):
        assert forbidden not in whole

    schemas = document["components"]["schemas"]
    scoped = {
        name: schema
        for name, schema in schemas.items()
        if name.startswith(("Energy", "Dispatch"))
    }
    assert scoped, "the energy and dispatch schemas must be part of the public document"
    assert "byte_width" not in json.dumps(scoped)
    energy_paths = {
        path: item for path, item in document["paths"].items() if path.endswith(("/energy", "/energy/command", "/energy/mode"))
    }
    assert sorted(energy_paths) == [
        "/api/v1/devices/{device_id}/energy",
        "/api/v1/devices/{device_id}/energy/command",
    ]
    assert "byte_width" not in json.dumps(energy_paths)
    assert "grid_import_positive" not in json.dumps(energy_paths)




def test_only_the_rct_adapter_layer_names_the_soc_target_register() -> None:
    """AC-13: the register name is a vendor detail. The scan excludes app/catalog/*.json (data) and
    tests/ (fixture code), both of which legitimately name it today."""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "app").rglob("*.py")
        if "power_mng_soc_target_set" in path.read_text(encoding="utf-8")
        and not path.as_posix().startswith((ROOT / "app" / "gateway").as_posix())
    ]
    assert offenders == []


# Files that already stated a capability verification before this feature existed; they are the only
# place a released record may come from, and the list must not grow (AC-20).
_VERIFIED_FIXTURES = {
    "tests/api_helpers.py",
    "tests/test_dispatch_capabilities.py",
    "tests/test_dispatch_core.py",
    "tests/test_soc_target_policy.py",  # fixture: a device the operator already verified as percent-attested
    "app/dispatch/capabilities.py",  # the enum member itself
    "app/admin/dispatch_api.py",  # the admin PUT that an operator uses to enter one
    "app/admin/energy_api.py",  # the same operator action as one atomic request
    "app/api/app_factory.py",  # only resets a record when a device is re-addressed
}
_VERIFIED = re.compile(r"CapabilityStatus\.VERIFIED|\"status\"\s*:\s*\"verified\"|status=\"verified\"")


def test_no_new_code_marks_a_capability_as_verified() -> None:
    """AC-20: a verification is a hardware fact an operator enters, never something code asserts."""
    named = {
        path.relative_to(ROOT).as_posix()
        for base in ("app", "tests")
        for path in (ROOT / base).rglob("*.py")
        if _VERIFIED.search(path.read_text(encoding="utf-8"))
    }
    assert named <= _VERIFIED_FIXTURES, f"new file marks a capability verified: {named - _VERIFIED_FIXTURES}"


async def test_a_failing_status_projection_after_an_executed_action_still_reports_success(
    tmp_path: Path,
) -> None:
    from unittest.mock import patch

    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        await arm(harness, mode="manual")
        headers = await admin_session(harness)
        with patch("app.admin.energy_api._admin_status", side_effect=RuntimeError("projection broke")):
            response = await harness.client.post(
                "/admin/api/energy/devices/main/command", headers=headers, json={"action": "auto"}
            )
        assert response.status_code == 202, response.text
        assert response.json() == {"executed": True, "status_available": False, "device_id": "main"}


async def test_the_admin_status_shows_the_restore_retry_state_but_the_public_status_does_not(
    tmp_path: Path,
) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        headers = await admin_session(harness)
        admin = (await harness.client.get("/admin/api/energy/devices", headers=headers)).json()[0]
        assert admin["restore_attempts"] == 0 and admin["next_restore_at"] is None
        public = (await harness.client.get("/api/v1/devices/main/energy", headers=WRITER)).json()
        assert "restore_attempts" not in public and "next_restore_at" not in public


async def test_enabling_write_support_approves_the_required_writes_and_never_removes(tmp_path: Path) -> None:
    """Switching write access on adds REQUIRED_WRITES to the allowlist (add-only, live); switching
    it off leaves the allowlist untouched. The Setup checklist's list is served by the backend."""
    settings = energy_settings(tmp_path)
    async with running_app(settings) as harness:
        store = harness.app.state.admin_store
        store.put_many({"write_names": []})
        headers = await admin_session(harness)
        off = await harness.client.put("/admin/api/settings", headers=headers, json={"enable_write_support": False})
        assert off.status_code == 200, off.text
        assert store.get("write_names") == []  # switching off changes nothing
        on = await harness.client.put("/admin/api/settings", headers=headers, json={"enable_write_support": True})
        assert on.status_code == 200, on.text
        assert set(DISPATCH_WRITE_NAMES) <= set(store.get("write_names"))
        parameters = await harness.client.get("/admin/api/parameters")
        assert set(DISPATCH_WRITE_NAMES) <= set(parameters.json()["write_names"])
        energy = await harness.client.get("/admin/api/energy/devices")
        main = next(item for item in energy.json() if item["device_id"] == "main")
        assert set(main["required_write_names"]) == set(DISPATCH_WRITE_NAMES)
        assert set(DISPATCH_WRITE_NAMES) <= set(main["approved_write_names"])
        exposed = store.get("exposed_names")
        trimmed = [name for name in store.get("write_names") if name != DISPATCH_WRITE_NAMES[0]]
        assert (await harness.client.put(
            "/admin/api/parameters", headers=headers, json={"exposed_names": exposed, "write_names": trimmed}
        )).status_code == 200
        assert (await harness.client.put(
            "/admin/api/settings", headers=headers, json={"enable_write_support": False}
        )).status_code == 200
        assert store.get("write_names") == trimmed  # switching off changes nothing
        assert (await harness.client.put(
            "/admin/api/settings", headers=headers, json={"enable_write_support": True}
        )).status_code == 200
        # Applied once: a register the operator cleared on purpose stays cleared.
        assert store.get("write_names") == trimmed
        assert store.get("write_defaults_applied") is True


async def test_clearing_a_required_register_closes_the_gate_and_the_checklist(tmp_path: Path) -> None:
    """Disarmed: clearing a required register leaves the actions refused (write_not_permitted) and
    the Setup data shows it missing. Armed: the clearing is refused (409), see the test above."""
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        headers = await admin_session(harness)
        store = harness.app.state.admin_store
        store.put_many({"write_names": list(DISPATCH_WRITE_NAMES)})
        exposed = store.get("exposed_names")
        trimmed = [n for n in DISPATCH_WRITE_NAMES if n != DISPATCH_WRITE_NAMES[1]]
        assert (await harness.client.put(
            "/admin/api/parameters", headers=headers, json={"exposed_names": exposed, "write_names": trimmed}
        )).status_code == 200
        main = next(i for i in (await harness.client.get("/admin/api/energy/devices")).json() if i["device_id"] == "main")
        assert DISPATCH_WRITE_NAMES[1] in set(main["required_write_names"]) - set(main["approved_write_names"])
        status = await harness.client.get("/api/v1/devices/main/energy", headers=WRITER)
        reasons = {i["reason"] for i in status.json()["actions"] if i["action"] != "auto"}
        assert reasons <= {"write_not_permitted", "mode_off"}


async def _put_write_support(harness, headers: dict[str, str], value: bool):
    return await harness.client.put(
        "/admin/api/settings", headers=headers, json={"enable_write_support": value}
    )


async def test_write_support_switches_on_and_off_live_without_a_restart(tmp_path: Path) -> None:
    """Boot with writes off, enable through Settings, use dispatch, disable: no restart notice, the
    routers follow the switch, disabling disarms and hands the inverter back to automatic."""
    async with running_app(energy_settings(tmp_path, enable_write_support=False)) as harness:
        seed_payloads(harness)
        headers = await admin_session(harness)
        assert harness.runtime.dispatch is None
        assert (await command(harness, {"action": "auto"})).json()["code"] == "write_disabled"

        on = await _put_write_support(harness, headers, True)
        assert on.status_code == 200, on.text
        assert "restart_required" not in on.json()
        assert "enable_write_support" in on.json()["live"]
        assert harness.runtime.dispatch is not None

        await arm(harness)
        charging = await command(harness, {"action": "charge", "target_soc_percent": 80})
        assert charging.status_code == 200, charging.text
        assert charging.json()["state"] == "charging"
        panel = (await harness.client.get("/admin/api/energy/devices")).json()[0]
        assert panel["armed"] is True and panel["state"] == "charging"

        headers = await admin_session(harness)  # arm() logged in again and rotated the CSRF token
        off = await _put_write_support(harness, headers, False)
        assert off.status_code == 200, off.text
        assert "restart_required" not in off.json()
        assert "write_restore_pending" not in off.json()
        refused = await command(harness, {"action": "auto"})
        assert refused.status_code == 404 and refused.json()["code"] == "write_disabled"
        panel = (await harness.client.get("/admin/api/energy/devices")).json()[0]
        assert panel["armed"] is False and panel["state"] == "automatic"
        rearm = await harness.client.put(
            "/admin/api/energy/devices/main/mode", headers=headers, json={"mode": "external"}
        )
        assert rearm.status_code == 409 and rearm.json()["code"] == "energy_write_support_required"

        # Back on: dispatch is reused, a fresh arming is needed.
        assert (await _put_write_support(harness, headers, True)).status_code == 200
        assert (await command(harness, {"action": "auto"})).json()["code"] == "energy_manager_off"




async def test_a_pat_cannot_switch_the_mode_and_the_session_can(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        for headers in (WRITER, READER):  # before any login: the client carries no session cookie yet
            response = await harness.client.put(
                "/admin/api/energy/devices/main/mode", headers=headers, json={"mode": "external"}
            )
            assert response.status_code == 403, response.text
        public = await harness.client.get("/api/v1/devices/main/energy", headers=WRITER)
        assert public.json()["mode"] == "off"
        assert (await arm(harness, mode="manual"))["mode"] == "manual"


async def test_the_admin_gui_fails_fast_when_the_inverter_is_not_connected(tmp_path: Path) -> None:
    from dataclasses import replace

    from app.gateway.base import DeviceState

    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        headers = await admin_session(harness)
        gateway = harness.runtime.gateway
        real = gateway.device_status
        for state in (DeviceState.UNREACHABLE, DeviceState.DEGRADED, DeviceState.STARTING):
            gateway.device_status = lambda device_id, state=state: replace(real(device_id), state=state)
            try:
                before = real("main").transactions
                listing = await harness.client.get("/admin/api/energy/devices", headers=headers)
                assert listing.json()[0]["connected"] is False, state
                for method, path, body in (
                    ("POST", "command", {"action": "hold"}),
                    ("PUT", "mode", {"mode": "manual"}),
                ):
                    response = await harness.client.request(
                        method, f"/admin/api/energy/devices/main/{path}", headers=headers, json=body
                    )
                    assert response.status_code == 502, (state, response.text)
                    assert response.json()["code"] == "device_unreachable"
                assert real("main").transactions == before  # refused before any device access
            finally:
                gateway.device_status = real
        listing = await harness.client.get("/admin/api/energy/devices", headers=headers)
        assert listing.json()[0]["connected"] is True


async def test_the_mode_decides_which_surface_may_command(tmp_path: Path) -> None:
    body = {"action": "hold"}
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)

        async def gui(headers: dict[str, str]):
            return await harness.client.post(
                "/admin/api/energy/devices/main/command", headers=headers, json=body
            )

        headers = await admin_session(harness)
        # off: both surfaces refuse
        for response in (await gui(headers), await command(harness, body)):
            assert response.status_code == 409 and response.json()["code"] == "energy_manager_off"

        # manual: the GUI commands, a PAT is told to ask the operator for External
        await arm(harness, mode="manual")
        headers = await admin_session(harness)
        assert (await gui(headers)).status_code == 200
        refused = await command(harness, body)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "energy_manager_not_external"
        assert "External" in refused.json()["detail"]
        assert (await harness.client.get("/api/v1/devices/main/energy", headers=WRITER)).status_code == 200

        # external: a PAT commands, the GUI is refused; the running hold was handed back by the switch
        await arm(harness, mode="external")
        headers = await admin_session(harness)
        status = (await harness.client.get("/api/v1/devices/main/energy", headers=WRITER)).json()
        assert status["mode"] == "external" and status["armed"] is True
        assert status["accepts_commands_from"] == "api" and status["state"] == "automatic"
        assert (await command(harness, body)).status_code == 200
        refused = await gui(headers)
        assert refused.status_code == 409 and refused.json()["code"] == "energy_manager_external"
        admin = (await harness.client.get("/admin/api/energy/devices", headers=headers)).json()[0]
        assert admin["mode"] == "external" and admin["mode_changed_by"] == "admin"

        # back to off: every command is refused again
        await arm(harness, mode="off")
        assert (await command(harness, body)).json()["code"] == "energy_manager_off"
        assert (await harness.client.get("/api/v1/devices/main/energy", headers=WRITER)).json()["armed"] is False


async def test_off_confirms_internal_strategy_and_records_initial_choice(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        headers = await admin_session(harness)
        strategy_id = RegistryCatalog.from_file(
            harness.runtime.settings.object_registry_path
        ).object_entry("power_mng_soc_strategy").object_id
        harness.net.payloads[strategy_id] = b"\x01"
        initial = (await harness.client.get("/admin/api/energy/devices", headers=headers)).json()[0]
        assert initial["mode"] == "off" and initial["mode_recorded"] is False

        response = await harness.client.put(
            "/admin/api/energy/devices/main/mode", headers=headers, json={"mode": "off"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["mode_recorded"] is True
        assert harness.net.payloads[strategy_id] == b"\x04"


async def test_off_write_failure_does_not_record_an_initial_choice(tmp_path: Path) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        seed_payloads(harness)
        headers = await admin_session(harness)
        strategy_id = RegistryCatalog.from_file(
            harness.runtime.settings.object_registry_path
        ).object_entry("power_mng_soc_strategy").object_id
        harness.net.payloads[strategy_id] = b"\x01"
        harness.net.freeze_writes = True
        response = await harness.client.put(
            "/admin/api/energy/devices/main/mode", headers=headers, json={"mode": "off"}
        )
        assert response.status_code != 200
        assert harness.net.payloads[strategy_id] == b"\x01"
        status = (await harness.client.get("/admin/api/energy/devices", headers=headers)).json()[0]
        assert status["mode_recorded"] is False
        assert "power_mng_soc_strategy" not in status["approved_write_names"]



ASSIST = "/admin/api/energy/devices/main/verification-assistant"


def seed_identity(
    harness, *, battery_w: float = 400.0, target: float = 0.5, load_w: float | None = None, grid_w: float = 1200.0
) -> None:
    """Identity plus a physically consistent house (solar 2400 W): load = solar + grid + battery."""
    catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
    for name, text in (("android_description", "RCT Power DC 10.0"), ("svnversion", "2.3.5687")):
        entry = catalog.object_entry(name)
        harness.net.payloads[entry.object_id] = encode_value(entry.data_type, text, byte_width=entry.byte_width)
    harness.net.payloads[catalog.object_entry("battery_power").object_id] = float_payload(battery_w)
    harness.net.payloads[catalog.object_entry("grid_power").object_id] = float_payload(grid_w)
    load = 2400.0 + grid_w + battery_w if load_w is None else load_w
    harness.net.payloads[catalog.object_entry("household_load_power").object_id] = float_payload(load)
    harness.net.payloads[catalog.object_entry("power_mng_soc_target_set").object_id] = float_payload(target)


async def follow_the_setpoint(harness, stop: asyncio.Event, *, grid_w: float | None = None) -> None:
    """Stand-in for the inverter's own control: with the external strategy active, battery power
    follows the written setpoint."""
    catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
    strategy = catalog.object_entry("power_mng_soc_strategy").object_id
    extern = catalog.object_entry("power_mng_battery_power_extern").object_id
    power = catalog.object_entry("battery_power").object_id
    grid = catalog.object_entry("grid_power").object_id
    baseline_battery = harness.net.payloads[power]
    while not stop.is_set():
        if harness.net.payloads.get(strategy) == b"\x01":
            harness.net.payloads[power] = harness.net.payloads[extern]
            if grid_w is not None:
                battery_before = struct.unpack(">f", baseline_battery)[0]
                battery_now = struct.unpack(">f", harness.net.payloads[extern])[0]
                harness.net.payloads[grid] = float_payload(grid_w + battery_before - battery_now)
        await asyncio.sleep(0.005)


@pytest.fixture
def fast_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.admin.energy_verification.PROBE_POLL_S", 0.02)
    monkeypatch.setattr("app.admin.energy_verification.PROBE_TIMEOUT_S", 0.5)
    monkeypatch.setattr("app.admin.energy_verification.BALANCE_GAP_S", 0.0)


async def assistant_ready(harness) -> dict[str, str]:
    seed_payloads(harness)
    seed_identity(harness)
    await harness.client.delete(VERIFICATION_URL, headers=await admin_session(harness))  # shipping state
    await arm(harness, mode="manual")
    return await admin_session(harness)  # arm() logged in again, so the earlier CSRF token is stale


async def unverified_command_status(harness, headers: dict[str, str]) -> tuple[int, str | None]:
    refused = await harness.client.post(
        "/admin/api/energy/devices/main/command", headers=headers, json={"action": "hold"}
    )
    return refused.status_code, refused.json().get("code")


async def test_the_guided_verification_stores_only_what_it_proved(tmp_path: Path, fast_probe: None) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await assistant_ready(harness)
        before = await unverified_command_status(harness, headers)
        assert before[0] != 200  # the gate is closed until the assistant has committed

        started = (await harness.client.post(f"{ASSIST}/start", headers=headers)).json()
        assert started["blockers"] == [] and started["started"] is True
        assert (started["model"], started["firmware"]) == ("RCT Power DC 10.0", "2.3.5687")
        assert not started["can_commit"]
        early = await harness.client.post(f"{ASSIST}/commit", headers=headers, json={"confirm": True})
        assert early.status_code == 409  # nothing proven yet, so nothing can be stored

        for kind, answer in (("battery", "discharging"), ("grid", "importing")):
            state = (await harness.client.post(
                f"{ASSIST}/direction", headers=headers, json={"kind": kind, "answer": answer}
            )).json()
        assert [s["status"] for s in state["steps"]] == ["done", "done", "pending"]

        refused = await harness.client.post(f"{ASSIST}/control-test", headers=headers, json={"confirm": False})
        assert refused.status_code == 400  # the active step needs explicit consent

        stop = asyncio.Event()
        follower = asyncio.create_task(follow_the_setpoint(harness, stop))
        try:
            tested = await harness.client.post(f"{ASSIST}/control-test", headers=headers, json={"confirm": True})
        finally:
            stop.set()
            await follower
        assert tested.status_code == 200, tested.text
        assert tested.json()["can_commit"] is True, tested.json()
        catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
        restored = {name: harness.net.payloads[catalog.object_entry(name).object_id] for name in DISPATCH_WRITE_NAMES}
        assert restored["power_mng_soc_strategy"] == b"\x00"  # handed back, not left under external control
        assert restored["power_mng_use_grid_power_enable"] == b"\x00"

        done = await harness.client.post(f"{ASSIST}/commit", headers=headers, json={"confirm": True})
        assert done.status_code == 200, done.text
        caps = _capabilities(done.json())
        assert {caps[name]["status"] for name in _VERIFICATION_NAMES} == {"verified"}
        write = caps["write_path_convention"]
        assert (write["verified_device_model"], write["verified_firmware"]) == ("RCT Power DC 10.0", "2.3.5687")
        assert write["enum_byte_width"] == 1 and write["bool_byte_width"] == 1
        assert write["write_frame_layout_verified"] and write["apply_sequence_verified"]
        assert write["note"].startswith("Guided:")
        assert caps["battery_power_sign_convention"]["battery_discharge_positive"] is True
        assert caps["grid_power_sign_convention"]["grid_import_positive"] is True
        assert (await unverified_command_status(harness, headers))[0] == 200  # now the gate is open


async def test_a_failed_control_test_keeps_the_gate_closed_and_restores_the_inverter(
    tmp_path: Path, fast_probe: None
) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await assistant_ready(harness)
        await harness.client.post(f"{ASSIST}/start", headers=headers)
        await harness.client.post(f"{ASSIST}/direction", headers=headers, json={"kind": "battery", "answer": "discharging"})
        await harness.client.post(f"{ASSIST}/direction", headers=headers, json={"kind": "grid", "answer": "importing"})
        # Nobody mirrors the setpoint: the battery keeps its 400 W, so external control is not proven.
        tested = (await harness.client.post(f"{ASSIST}/control-test", headers=headers, json={"confirm": True})).json()
        step = next(item for item in tested["steps"] if item["id"] == "control_test")
        assert step["status"] == "failed" and "did not follow" in step["message"]
        assert tested["can_commit"] is False
        catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
        assert harness.net.payloads[catalog.object_entry("power_mng_soc_strategy").object_id] == b"\x00"
        commit = await harness.client.post(f"{ASSIST}/commit", headers=headers, json={"confirm": True})
        assert commit.status_code == 409
        entry = (await harness.client.get("/admin/api/energy/devices", headers=headers)).json()[0]
        assert {c["status"] for n, c in _capabilities(entry).items() if n in _VERIFICATION_NAMES} == {"unverified"}
        assert (await unverified_command_status(harness, headers))[0] != 200


async def test_the_assistant_refuses_readings_it_cannot_interpret(tmp_path: Path, fast_probe: None) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await assistant_ready(harness)
        seed_identity(harness, battery_w=40.0)  # almost idle
        await harness.client.post(f"{ASSIST}/start", headers=headers)
        weak = (await harness.client.post(
            f"{ASSIST}/direction", headers=headers, json={"kind": "battery", "answer": "charging"}
        )).json()
        assert weak["steps"][0]["status"] == "failed" and "too little" in weak["steps"][0]["message"]
        mismatched = await harness.client.post(
            f"{ASSIST}/direction", headers=headers, json={"kind": "battery", "answer": "importing"}
        )
        assert mismatched.status_code == 422
        await harness.client.post(f"{ASSIST}/direction", headers=headers, json={"kind": "grid", "answer": "importing"})
        # An idle battery cannot show that the hold took effect, so the test must not pass.
        seed_identity(harness, battery_w=400.0)  # the balance makes 400 W a discharge, so "charging" would be refused
        await harness.client.post(f"{ASSIST}/direction", headers=headers, json={"kind": "battery", "answer": "discharging"})
        seed_identity(harness, battery_w=0.0)
        idle = (await harness.client.post(f"{ASSIST}/control-test", headers=headers, json={"confirm": True})).json()
        step = next(item for item in idle["steps"] if item["id"] == "control_test")
        assert step["status"] == "failed" and "almost idle" in step["message"]
        assert idle["can_commit"] is False


async def test_an_unreadable_target_unit_blocks_the_commit(tmp_path: Path, fast_probe: None) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await assistant_ready(harness)
        seed_identity(harness, target=1.0)  # 100 % in ratio units reads like 1 % in percent units
        state = (await harness.client.post(f"{ASSIST}/start", headers=headers)).json()
        assert state["started"] is True and state["can_commit"] is False
        assert any("unit cannot be determined" in blocker for blocker in state["blockers"])



def sample(battery: float, grid: float, solar: float, load: float) -> BalanceSample:
    return BalanceSample(battery_w=battery, grid_w=grid, solar_w=solar, load_w=load)


@pytest.mark.parametrize(
    ("readings", "expected"),
    [
        pytest.param(sample(0, 500, 0, 500), (None, True), id="night-battery-idle-grid-import-fixes-grid"),
        pytest.param(sample(1000, 0, 0, 1000), (True, None), id="night-battery-discharging-grid-idle"),
        pytest.param(sample(600, 400, 0, 1000), (True, True), id="night-discharge-plus-import"),
        pytest.param(sample(-1500, -500, 3000, 1000), (True, True), id="day-charging-and-export"),
        pytest.param(sample(1500, 500, 3000, 1000), (False, False), id="same-day-with-both-signs-mirrored"),
        pytest.param(sample(0, 0, 0, 0), (None, None), id="everything-idle"),
        pytest.param(sample(0, 520, 0, 500), (None, True), id="meter-noise-inside-the-tolerance"),
        pytest.param(sample(50, 450, 0, 500), (None, True), id="battery-below-the-direction-threshold"),
        pytest.param(sample(100, 100, 0, 3000), (None, None), id="inconsistent-balance"),
        pytest.param(sample(-500, 500, 0, 0), (None, None), id="symmetric-hypotheses-cannot-be-told-apart"),
        pytest.param(sample(0, 500, 0, -500), (None, None), id="negative-load-is-not-a-load"),
        pytest.param(sample(0, 500, -400, 100), (None, None), id="negative-solar-is-not-generation"),
        pytest.param(sample(0, math.nan, 0, 500), (None, None), id="not-a-number"),
        pytest.param(sample(2000, 0, 0, 500), (None, None), id="unmetered-extra-source-breaks-the-balance"),
    ],
)
def test_the_power_balance_proves_only_what_a_unique_fit_supports(readings: BalanceSample, expected) -> None:
    proof = prove_signs_from_balance([readings, readings])
    assert (proof.battery_discharge_positive, proof.grid_import_positive) == expected


def test_samples_that_disagree_prove_nothing() -> None:
    discharging = sample(1000, 0, 0, 1000)
    mirrored = sample(-1000, 0, 0, 1000)  # the same load explained by the opposite convention
    proof = prove_signs_from_balance([discharging, mirrored])
    assert (proof.battery_discharge_positive, proof.grid_import_positive) == (None, None)
    assert prove_signs_from_balance([]) == BalanceProof()


def step_of(state: dict, step_id: str) -> dict:
    return next(item for item in state["steps"] if item["id"] == step_id)


async def test_proven_signs_skip_the_questions_and_are_recorded_as_such(tmp_path: Path, fast_probe: None) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await assistant_ready(harness)  # solar 2400 + import 1200 + discharge 400 = load 4000
        await harness.client.post(f"{ASSIST}/start", headers=headers)
        state = (await harness.client.post(f"{ASSIST}/auto-check", headers=headers)).json()
        battery, grid = step_of(state, "battery"), step_of(state, "grid")
        assert (battery["status"], battery["method"], battery["direction"]) == ("done", "balance", "discharging")
        assert (grid["status"], grid["method"], grid["direction"]) == ("done", "balance", "importing")
        assert battery["power_w"] == 400.0 and state["min_direction_w"] == 100.0

        stop = asyncio.Event()
        follower = asyncio.create_task(follow_the_setpoint(harness, stop))
        try:
            tested = await harness.client.post(f"{ASSIST}/control-test", headers=headers, json={"confirm": True})
        finally:
            stop.set()
            await follower
        assert tested.json()["can_commit"] is True, tested.json()
        done = await harness.client.post(f"{ASSIST}/commit", headers=headers, json={"confirm": True})
        assert done.status_code == 200, done.text
        caps = _capabilities(done.json())
        assert caps["battery_power_sign_convention"]["battery_discharge_positive"] is True
        assert caps["grid_power_sign_convention"]["grid_import_positive"] is True
        note = caps["write_path_convention"]["note"]
        assert "battery sign by power balance" in note and "grid sign by power balance" in note


@pytest.mark.parametrize("grid_follows", [True, False])
async def test_the_hold_can_prove_an_initially_idle_grid_without_guessing(
    tmp_path: Path, fast_probe: None, grid_follows: bool
) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await assistant_ready(harness)
        seed_identity(harness, grid_w=2.0)
        await harness.client.post(f"{ASSIST}/start", headers=headers)
        initial = (await harness.client.post(f"{ASSIST}/auto-check", headers=headers)).json()
        assert step_of(initial, "battery")["status"] == "done"
        assert step_of(initial, "grid")["status"] == "pending"

        stop = asyncio.Event()
        follower = asyncio.create_task(follow_the_setpoint(harness, stop, grid_w=2.0 if grid_follows else None))
        try:
            tested = await harness.client.post(f"{ASSIST}/control-test", headers=headers, json={"confirm": True})
        finally:
            stop.set()
            await follower
        assert tested.status_code == 200, tested.text
        state = tested.json()
        assert step_of(state, "control_test")["status"] == "done"
        grid = step_of(state, "grid")
        if grid_follows:
            assert (grid["status"], grid["method"], grid["direction"]) == ("done", "balance", "importing")
            assert state["can_commit"] is True
            saved = await harness.client.post(f"{ASSIST}/commit", headers=headers, json={"confirm": True})
            assert saved.status_code == 200, saved.text
        else:
            assert grid["status"] == "pending" and state["can_commit"] is False
            refused = await harness.client.post(f"{ASSIST}/commit", headers=headers, json={"confirm": True})
            assert refused.status_code == 409


async def test_a_mirrored_register_convention_is_proven_as_such(tmp_path: Path, fast_probe: None) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await assistant_ready(harness)
        # The battery discharges 400 W but its register is negative: discharge is NOT positive here.
        seed_identity(harness, battery_w=-400.0, load_w=2400.0 + 1200.0 + 400.0)
        await harness.client.post(f"{ASSIST}/start", headers=headers)
        state = (await harness.client.post(f"{ASSIST}/auto-check", headers=headers)).json()
        battery = step_of(state, "battery")
        assert (battery["status"], battery["direction"], battery["power_w"]) == ("done", "discharging", 400.0)
        session = harness.app.state.energy_verification_sessions["main"]
        assert session.battery_discharge_positive is False


async def test_an_idle_battery_is_never_guessed_and_the_proof_arrives_when_it_moves(
    tmp_path: Path, fast_probe: None
) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await assistant_ready(harness)
        seed_identity(harness, battery_w=0.0)  # night-like balance: the grid alone fixes only the grid sign
        await harness.client.post(f"{ASSIST}/start", headers=headers)
        state = (await harness.client.post(f"{ASSIST}/auto-check", headers=headers)).json()
        assert step_of(state, "grid")["status"] == "done" and step_of(state, "grid")["method"] == "balance"
        waiting = step_of(state, "battery")
        assert (waiting["status"], waiting["message"], waiting["method"]) == ("pending", None, None)
        # "Idle" is only a statement: validated, never a proof.
        idle = (await harness.client.post(
            f"{ASSIST}/direction", headers=headers, json={"kind": "battery", "answer": "idle"}
        )).json()
        assert (step_of(idle, "battery")["status"], step_of(idle, "battery")["message"]) == ("pending", None)
        assert idle["can_commit"] is False
        assert (await harness.client.post(f"{ASSIST}/commit", headers=headers, json={"confirm": True})).status_code == 409
        # The inverter reports power, so "idle" contradicts it and is refused with a plain reason.
        seed_identity(harness, battery_w=400.0, load_w=0.0)
        contradicted = (await harness.client.post(
            f"{ASSIST}/direction", headers=headers, json={"kind": "battery", "answer": "idle"}
        )).json()
        message = step_of(contradicted, "battery")["message"]
        assert step_of(contradicted, "battery")["status"] == "failed" and "not idle" in message
        # The battery starts moving with a consistent balance: the next check proves it by itself.
        seed_identity(harness, battery_w=400.0)
        proven = (await harness.client.post(f"{ASSIST}/auto-check", headers=headers)).json()
        assert step_of(proven, "battery")["status"] == "done" and step_of(proven, "battery")["method"] == "balance"


async def test_without_a_usable_balance_the_operator_is_asked_and_a_contradiction_is_refused(
    tmp_path: Path, fast_probe: None
) -> None:
    async with running_app(energy_settings(tmp_path)) as harness:
        headers = await assistant_ready(harness)
        seed_identity(harness, load_w=0.0)  # no usable load figure: the balance proves nothing
        await harness.client.post(f"{ASSIST}/start", headers=headers)
        state = (await harness.client.post(f"{ASSIST}/auto-check", headers=headers)).json()
        assert [item["status"] for item in state["steps"]] == ["pending", "pending", "pending"]
        answered = (await harness.client.post(
            f"{ASSIST}/direction", headers=headers, json={"kind": "battery", "answer": "discharging"}
        )).json()
        assert (step_of(answered, "battery")["status"], step_of(answered, "battery")["method"]) == ("done", "operator")
        # A consistent balance now proves discharge; an operator answer to the contrary is not stored.
        seed_identity(harness)
        await harness.client.delete(ASSIST, headers=headers)
        await harness.client.post(f"{ASSIST}/start", headers=headers)
        conflict = (await harness.client.post(
            f"{ASSIST}/direction", headers=headers, json={"kind": "battery", "answer": "charging"}
        )).json()
        step = step_of(conflict, "battery")
        assert step["status"] == "failed" and "points the other way" in step["message"]
        agreed = (await harness.client.post(
            f"{ASSIST}/direction", headers=headers, json={"kind": "battery", "answer": "discharging"}
        )).json()
        assert step_of(agreed, "battery")["method"] == "both"



from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.admin.forecast_refresh import solar_config_key, spot_config_key
from app.optimizer.models import SolarForecast, SolarForecastInterval
from app.spotmarket.base import SpotMarketConnectionError
from app.spotmarket.models import (
    SpotMarketConfig,
    SpotMarketProvider,
    SpotPriceInterval,
    SpotPriceSnapshot,
)
from app.weather.models import WeatherConfig

SPOT_CONFIG = "/admin/api/energy/spot-market/config"
SPOT_PREVIEW = "/admin/api/energy/spot-market/preview"


def spot_market_settings(tmp_path: Path):
    """Settings with an admin store (so the admin energy router is mounted) but no dispatch.

    Spot-market config needs the admin surface, not write support; this mirrors how
    ``energy_settings`` pre-creates the admin database at the path ``create_app`` will reopen, with
    the bootstrap password already changed to ``PASSWORD``.
    """
    admin_path = tmp_path / "spot-admin.db"
    store = AdminStore(admin_path, SECRET)
    password = store.initialize()
    assert password is not None
    assert store.change_password(password, PASSWORD)
    store.accept_disclaimer()
    store.close()
    return make_settings(hmac_secret=SECRET, admin_db_path=admin_path)


def _spot_body(**overrides) -> dict:
    base = {
        "enabled": True,
        "provider": "energy_charts",
        "bidding_zone": "DE-LU",
        "fee_region": {"code": "netze-bw", "label": "Netze BW"},
    }
    return {**base, **overrides}


def _seed_spot_prices(store, intervals, timezone="UTC") -> None:
    config = SpotMarketConfig.model_validate(_spot_body())
    fetched = datetime.now(UTC).isoformat()
    zone = ZoneInfo(timezone)
    store.replace_spot_market_intervals([
        {
            "config_key": spot_config_key(config),
            "local_date": interval.start.astimezone(zone).date().isoformat(),
            "interval_start": interval.start.isoformat(),
            "interval_end": interval.end.isoformat(),
            "price_eur_per_kwh": float(interval.price_eur_per_kwh),
            "timezone": timezone,
            "fetched_at": fetched,
        }
        for interval in intervals
    ])


def _activate_spot_market(harness) -> None:
    config = SpotMarketConfig.model_validate(_spot_body())
    harness.app.state.spotmarket_config = config
    harness.app.state.admin_store.put("spotmarket_config", config.model_dump(mode="json"))


def _stub_background_refreshes(harness) -> None:
    class _SpotProvider:
        def _empty(self, config):
            return SpotPriceSnapshot(
                SpotMarketProvider.ENERGY_CHARTS, config.bidding_zone, "EUR", datetime.now(UTC), (),
                timezone="Europe/Berlin",
            )

        def fetch_today(self, config):
            return self._empty(config)

        def fetch_current(self, config):
            return self._empty(config)

        def fetch_yesterday(self, config, timezone):
            return self._empty(config)

        def fetch_day_ahead(self, config):
            return self._empty(config)

    class _SolarProvider:
        def fetch_preview(self, config, now):
            return SolarForecast(now, ())

    harness.app.state.spotmarket_provider = _SpotProvider()
    harness.app.state.solar_forecast_provider = _SolarProvider()


def _seed_solar_forecast(store, config: WeatherConfig, forecast: SolarForecast) -> None:
    zone = ZoneInfo(config.timezone or "UTC")
    store.replace_solar_forecast_intervals([
        {
            "config_key": solar_config_key(config),
            "local_date": interval.start.astimezone(zone).date().isoformat(),
            "interval_start": interval.start.isoformat(),
            "interval_end": interval.end.isoformat(),
            "energy_wh": interval.energy_wh,
            "timezone": config.timezone or "UTC",
            "fetched_at": forecast.created_at.isoformat(),
        }
        for interval in forecast.intervals
    ])


async def test_spot_market_config_defaults_then_saves_and_reloads(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_background_refreshes(harness)
        headers = await admin_session(harness)
        default = await harness.client.get(SPOT_CONFIG, headers=headers)
        assert default.status_code == 200, default.text
        assert default.json()["enabled"] is False
        assert default.json()["bidding_zone"] == "DE-LU"

        saved = await harness.client.put(SPOT_CONFIG, headers=headers, json=_spot_body())
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["enabled"] is True
        # Fee region and bidding zone stay separate in the stored and returned shape.
        assert body["bidding_zone"] == "DE-LU"
        assert body["fee_region"] == {"code": "netze-bw", "label": "Netze BW"}
        # Currency and price unit are provider metadata, returned read-only, not operator input.
        assert body["currency"] == "EUR"
        assert body["price_unit"] == "EUR/MWh"

        reloaded = await harness.client.get(SPOT_CONFIG, headers=headers)
        assert reloaded.json() == body
        # The in-process copy on app.state is updated too, not only the stored record.
        assert harness.app.state.spotmarket_config.bidding_zone == "DE-LU"
        assert harness.app.state.spotmarket_config.fee_region.code == "netze-bw"


async def test_spot_market_config_persists_in_the_admin_store(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_background_refreshes(harness)
        headers = await admin_session(harness)
        await harness.client.put(SPOT_CONFIG, headers=headers, json=_spot_body(bidding_zone="AT"))
        stored = harness.app.state.admin_store.get("spotmarket_config")
    assert stored["bidding_zone"] == "AT"
    assert stored["provider"] == "energy_charts"


async def test_spot_market_save_leaves_db_and_runtime_in_agreement(tmp_path: Path) -> None:
    # Finding A invariant: the persist and the app.state swap happen in the same critical section,
    # so after a save the stored config and the in-process config describe the same bidding zone.
    # A concurrent save can no longer leave the DB on one config and runtime on another.
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_background_refreshes(harness)
        headers = await admin_session(harness)
        saved = await harness.client.put(SPOT_CONFIG, headers=headers, json=_spot_body(bidding_zone="AT"))
        assert saved.status_code == 200, saved.text
        stored = harness.app.state.admin_store.get("spotmarket_config")
        assert stored["bidding_zone"] == harness.app.state.spotmarket_config.bidding_zone == "AT"


async def test_identical_spot_config_save_preserves_prices_without_refresh(tmp_path: Path, monkeypatch) -> None:
    from app.admin import energy_api

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_background_refreshes(harness)
        headers = await admin_session(harness)
        body = _spot_body()
        assert (await harness.client.put(SPOT_CONFIG, headers=headers, json=body)).status_code == 200
        now = datetime.now(UTC)
        interval = SpotPriceInterval(now, now + timedelta(minutes=15), Decimal("0.23"))
        store = harness.app.state.admin_store
        _seed_spot_prices(store, (interval,))
        signals = []
        monkeypatch.setattr(energy_api, "_signal_forecast_refresh", lambda *args: signals.append(args))
        assert (await harness.client.put(SPOT_CONFIG, headers=headers, json=body)).status_code == 200
        assert len(store.get_spot_market_intervals(spot_config_key(harness.app.state.spotmarket_config))) == 1
        assert signals == []


async def test_spot_market_config_rejects_a_bad_provider_or_zone(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        for bad in (
            _spot_body(provider="made_up"),
            _spot_body(bidding_zone=""),
        ):
            response = await harness.client.put(SPOT_CONFIG, headers=headers, json=bad)
            assert response.status_code == 422, (bad, response.text)


async def test_spot_market_config_rejects_an_operator_supplied_api_url(tmp_path: Path) -> None:
    # The save body forbids extra fields, so an attempt to smuggle a network
    # target back in through ``api_url`` is a hard 422, never silently stored.
    async with running_app(spot_market_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        response = await harness.client.put(
            SPOT_CONFIG, headers=headers, json=_spot_body(api_url="http://127.0.0.1/price")
        )
        assert response.status_code == 422, response.text


async def test_spot_market_config_requires_an_admin_session(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        # No session cookie and no CSRF header: the mutation is refused.
        anonymous = await harness.client.put(SPOT_CONFIG, json=_spot_body(), headers={"Origin": "http://test"})
        assert anonymous.status_code in (401, 403)


async def test_spot_market_preview_summarises_a_fetched_snapshot(tmp_path: Path) -> None:
    from decimal import Decimal

    # Each fetch returns its OWN calendar day (yesterday 01-01, today 01-02, tomorrow 01-03). The
    # preview builder re-buckets by each interval's local date, so the three days land in distinct
    # series. ``fetched_at`` is 01-03 00:20 so "now" falls inside tomorrow's second interval and the
    # summary (which prefers the next-day series) still resolves a current price.
    def _three(day: int) -> tuple[SpotPriceInterval, ...]:
        base_day = datetime.now(UTC).date() + timedelta(days=day - 2)
        base = datetime.combine(base_day, datetime.min.time(), tzinfo=UTC)
        return (
            SpotPriceInterval(base, base.replace(minute=15), Decimal("0.20")),
            SpotPriceInterval(base.replace(minute=15), base.replace(minute=30), Decimal("0.10")),
            SpotPriceInterval(base.replace(minute=30), base.replace(minute=45), Decimal("0.30")),
        )

    # "Now" is 01-02 00:20 — the real current day — so yesterday/today/tomorrow classify as
    # 01-01/01-02/01-03.
    fetched = datetime.now(UTC)

    def _snapshot(config, intervals):
        return SpotPriceSnapshot(
            provider=SpotMarketProvider.ENERGY_CHARTS,
            bidding_zone=config.bidding_zone,
            currency="EUR",  # provider metadata, not operator config
            fetched_at=fetched,
            intervals=intervals,
            timezone="UTC",
        )

    class _StubProvider:
        def fetch_today(self, config):
            return _snapshot(config, _three(2))

        def fetch_yesterday(self, config, timezone):
            return _snapshot(config, _three(1))

        def fetch_day_ahead(self, config):
            return _snapshot(config, _three(3))

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _activate_spot_market(harness)
        _seed_spot_prices(harness.app.state.admin_store, (*_three(1), *_three(2), *_three(3)))
        headers = await admin_session(harness)
        response = await harness.client.post(SPOT_PREVIEW, headers=headers, json=_spot_body())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "connected"
        assert body["intervals_available"] == 3
        assert body["min_price_eur_per_kwh"] == 0.10
        assert body["max_price_eur_per_kwh"] == 0.30
        assert len(body["today_intervals"]) == 3
        assert len(body["next_day_intervals"]) == 3
        assert len(body["yesterday_intervals"]) == 3
        # The summary (min/max) prefers tomorrow's series when it is published. "Now" (01-02) is not
        # inside tomorrow's intervals (01-03), so there is no current price for that series.
        assert body["current_price_eur_per_kwh"] is None


async def test_spot_market_preview_reuses_the_overwritten_database_snapshot(tmp_path: Path) -> None:
    today = datetime.now(UTC).date()
    intervals = []
    for day_offset, price in ((-1, "0.10"), (0, "0.20"), (1, "0.30")):
        midnight = datetime.combine(today + timedelta(days=day_offset), datetime.min.time(), tzinfo=UTC)
        for slot in range(96):
            start = midnight + timedelta(minutes=15 * slot)
            intervals.append(SpotPriceInterval(start, start + timedelta(minutes=15), Decimal(price)))

    class _ProviderTripwire:
        def fetch_today(self, config):
            raise AssertionError("the preview must read the database, not the provider")

        def fetch_yesterday(self, config, timezone):
            raise AssertionError("the preview must read the database, not the provider")

        def fetch_day_ahead(self, config):
            raise AssertionError("the preview must read the database, not the provider")

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _activate_spot_market(harness)
        _seed_spot_prices(harness.app.state.admin_store, intervals)
        harness.app.state.spotmarket_provider = _ProviderTripwire()
        headers = await admin_session(harness)
        first = await harness.client.post(SPOT_PREVIEW, headers=headers, json=_spot_body())
        assert first.status_code == 200, first.text
        with harness.app.state.admin_store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM spot_market_intervals").fetchone()[0] == 3 * 96

        second = await harness.client.post(SPOT_PREVIEW, headers=headers, json=_spot_body())
        assert second.status_code == 200, second.text
        assert second.json() == first.json()


async def test_spot_market_preview_summarises_available_series_when_tomorrow_unpublished(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: in the normal daily pre-auction state the next-day curve is not published yet
    # (fetch_day_ahead raises SpotMarketNotAvailableYet), but yesterday and today exist. The
    # Lowest/Highest figures must still be computed from the available series (today), not gated on
    # tomorrow — before the fix min/max came back null whenever tomorrow was absent. The intervals
    # are given in the bidding zone's LOCAL zone (Europe/Berlin) so the date bucketing is exercised
    # across the local-day boundary, not only in UTC.
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from app.spotmarket.base import SpotMarketNotAvailableYet

    berlin = ZoneInfo("Europe/Berlin")  # CET (+01:00) on these January dates
    # Fixed instant: the wall clock made the live quarter collide with a static interval (same
    # primary key); 10:20 on a January day also keeps CET/+01:00 stable.
    now_local = datetime(2026, 1, 15, 10, 20, tzinfo=berlin)
    local_today = now_local.date()
    fetched = now_local.astimezone(UTC)

    def _quarter(local_dt: datetime, price: str) -> SpotPriceInterval:
        return SpotPriceInterval(local_dt, local_dt + timedelta(minutes=15), Decimal(price))

    # The first interval starts at 00:30 LOCAL, which can belong to the previous UTC date.
    current_start = now_local.replace(minute=now_local.minute // 15 * 15, second=0, microsecond=0)
    today_intervals = (
        _quarter(datetime.combine(local_today, datetime.min.time(), tzinfo=berlin) + timedelta(minutes=30), "0.20"),
        _quarter(current_start, "0.08"),
        _quarter(datetime.combine(local_today, datetime.min.time(), tzinfo=berlin) + timedelta(hours=18), "0.25"),
    )
    # Yesterday in the configured bidding-zone timezone.
    yesterday_intervals = (
        _quarter(datetime.combine(local_today - timedelta(days=1), datetime.min.time(), tzinfo=berlin) + timedelta(hours=12), "0.15"),
        _quarter(datetime.combine(local_today - timedelta(days=1), datetime.min.time(), tzinfo=berlin) + timedelta(hours=13), "0.17"),
    )

    class _StubProvider:
        def fetch_today(self, config):
            return SpotPriceSnapshot(
                provider=SpotMarketProvider.ENERGY_CHARTS,
                bidding_zone=config.bidding_zone,
                currency="EUR",
                fetched_at=fetched,
                intervals=today_intervals,
                timezone="Europe/Berlin",
            )

        def fetch_yesterday(self, config, timezone):
            return SpotPriceSnapshot(
                provider=SpotMarketProvider.ENERGY_CHARTS,
                bidding_zone=config.bidding_zone,
                currency="EUR",
                fetched_at=fetched,
                intervals=yesterday_intervals,
                timezone="Europe/Berlin",
            )

        def fetch_day_ahead(self, config):
            raise SpotMarketNotAvailableYet("Energy-Charts next-day prices are not published yet")

    # The preview route reads the wall clock directly (``datetime.now(UTC)``), not the injected
    # clock, so pin that module reference to the fixed instant. The preview then classifies today
    # and selects the current-slot price deterministically, whatever time the suite runs at.
    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fetched if tz is None else fetched.astimezone(tz)

    monkeypatch.setattr("app.admin.energy_api.datetime", _FixedDatetime)
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _activate_spot_market(harness)
        _seed_spot_prices(harness.app.state.admin_store, (*today_intervals, *yesterday_intervals), "Europe/Berlin")
        headers = await admin_session(harness)
        response = await harness.client.post(SPOT_PREVIEW, headers=headers, json=_spot_body())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "connected"
        # Tomorrow is empty and its unavailability is reported, but it is not an error.
        assert body["next_day_intervals"] == []
        assert body["next_day_message"]
        # Both available series render, bucketed by their own local date and kept distinct.
        assert len(body["today_intervals"]) == 3
        assert len(body["yesterday_intervals"]) == 2
        today_starts = {item["start"] for item in body["today_intervals"]}
        yesterday_starts = {item["start"] for item in body["yesterday_intervals"]}
        assert today_starts.isdisjoint(yesterday_starts)
        # Midnight-boundary proof: the 00:30 LOCAL interval (23:30 UTC the previous day) is filed
        # under today, not yesterday — i.e. the bucketing used the zone, not UTC.
        assert any(start.endswith("00:30:00+01:00") or "T00:30" in start for start in today_starts)
        # The key assertion: Lowest/Highest are computed over today (the best available populated
        # series), not left null just because tomorrow is unpublished.
        assert body["min_price_eur_per_kwh"] == 0.08
        assert body["max_price_eur_per_kwh"] == 0.25
        assert body["min_price_at"] is not None
        assert body["max_price_at"] is not None
        # 10:20 local falls inside today's second interval (10:15–10:30).
        assert body["current_price_eur_per_kwh"] == 0.08


async def test_spot_market_preview_buckets_a_misfiled_today_curve_by_local_date(tmp_path: Path) -> None:
    # The exact user regression: the provider's "today" fetch returns a curve whose intervals are
    # actually TODAY's local day, but a naive builder that trusts the fetch source would need the
    # provider to be perfect. Here the provider mislabels — fetch_today returns an EMPTY snapshot
    # while fetch_yesterday returns today's full curve plus yesterday's. Re-bucketing by each
    # interval's own local date must still split them into distinct today/yesterday series and
    # produce non-null Lowest/Highest, so the chart draws BOTH grey (yesterday) and green (today).
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from app.spotmarket.base import SpotMarketNotAvailableYet

    berlin = ZoneInfo("Europe/Berlin")
    fetched = datetime.now(berlin)
    local_today = fetched.date()

    def _quarter(local_dt: datetime, price: str) -> SpotPriceInterval:
        return SpotPriceInterval(local_dt, local_dt + timedelta(minutes=15), Decimal(price))

    # A mixed snapshot delivered through fetch_yesterday that actually spans both local days.
    mixed = (
        _quarter(datetime.combine(local_today - timedelta(days=1), datetime.min.time(), tzinfo=berlin) + timedelta(hours=9), "0.15"),  # yesterday
        _quarter(datetime.combine(local_today, datetime.min.time(), tzinfo=berlin) + timedelta(hours=9), "0.07"),  # today (lowest)
        _quarter(datetime.combine(local_today, datetime.min.time(), tzinfo=berlin) + timedelta(hours=19), "0.28"),  # today (highest)
    )

    class _StubProvider:
        def fetch_today(self, config):
            # Mislabelled/empty today window: the data really arrives via the yesterday fetch.
            return SpotPriceSnapshot(
                provider=SpotMarketProvider.ENERGY_CHARTS,
                bidding_zone=config.bidding_zone,
                currency="EUR",
                fetched_at=fetched,
                intervals=(),
                timezone="Europe/Berlin",
            )

        def fetch_yesterday(self, config, timezone):
            return SpotPriceSnapshot(
                provider=SpotMarketProvider.ENERGY_CHARTS,
                bidding_zone=config.bidding_zone,
                currency="EUR",
                fetched_at=fetched,
                intervals=mixed,
                timezone="Europe/Berlin",
            )

        def fetch_day_ahead(self, config):
            raise SpotMarketNotAvailableYet("not published yet")

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _activate_spot_market(harness)
        _seed_spot_prices(harness.app.state.admin_store, mixed, "Europe/Berlin")
        headers = await admin_session(harness)
        response = await harness.client.post(SPOT_PREVIEW, headers=headers, json=_spot_body())
        assert response.status_code == 200, response.text
        body = response.json()
        # Despite the empty "today" fetch, today's curve is recovered from the actual interval dates.
        assert len(body["today_intervals"]) == 2
        assert len(body["yesterday_intervals"]) == 1
        # Lowest/Highest come from today's populated series, not n/a.
        assert body["min_price_eur_per_kwh"] == 0.07
        assert body["max_price_eur_per_kwh"] == 0.28


async def test_spot_market_preview_reports_empty_database_without_fetching_provider(tmp_path: Path) -> None:
    class _ProviderTripwire:
        def fetch_today(self, config):
            raise AssertionError("preview requests must not fetch provider data")

        fetch_yesterday = fetch_today
        fetch_day_ahead = fetch_today

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _activate_spot_market(harness)
        harness.app.state.spotmarket_provider = _ProviderTripwire()
        headers = await admin_session(harness)
        response = await harness.client.post(SPOT_PREVIEW, headers=headers, json=_spot_body())
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "unavailable"
        assert "background" in response.json()["message"]


async def test_spot_market_config_rejects_operator_currency_or_price_unit(tmp_path: Path) -> None:
    # Currency and price unit are provider metadata, not operator input. The save body
    # forbids extra fields, so smuggling either one back in is a hard 422, never stored.
    async with running_app(spot_market_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        for bad in (_spot_body(currency="USD"), _spot_body(price_unit="EUR/kWh")):
            response = await harness.client.put(SPOT_CONFIG, headers=headers, json=bad)
            assert response.status_code == 422, (bad, response.text)


async def test_spot_market_config_preserves_the_fee_region_concept(tmp_path: Path) -> None:
    # The fee_region concept stays in the model and persistence even though the UI no
    # longer offers a field for it; a save that carries it through keeps the stored value.
    async with running_app(spot_market_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        await harness.client.put(
            SPOT_CONFIG, headers=headers, json=_spot_body(fee_region={"code": "netze-bw", "label": "Netze BW"})
        )
        reloaded = await harness.client.get(SPOT_CONFIG, headers=headers)
    assert reloaded.json()["fee_region"] == {"code": "netze-bw", "label": "Netze BW"}
    assert harness.app.state.spotmarket_config.fee_region.code == "netze-bw"


SPOT_ZONES = "/admin/api/energy/spot-market/bidding-zones"


async def test_spot_market_bidding_zones_are_served_from_the_cache(tmp_path: Path) -> None:
    # The endpoint reads the zone list cache-first; the stub cache answers without any network call.
    from app.spotmarket.metadata import ZoneListing

    listing = ZoneListing(
        zones=("DE-LU", "AT", "FR"), default="DE-LU", stale=False, last_update=datetime(2026, 1, 1, tzinfo=UTC)
    )

    class _StubCache:
        def __init__(self) -> None:
            self.calls = 0

        def get(self) -> ZoneListing:
            self.calls += 1
            return listing

    async with running_app(spot_market_settings(tmp_path)) as harness:
        harness.app.state.spotmarket_zone_cache = _StubCache()
        headers = await admin_session(harness)
        response = await harness.client.get(SPOT_ZONES, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["zones"] == ["DE-LU", "AT", "FR"]
        assert body["default"] == "DE-LU"
        assert body["stale"] is False


async def test_spot_market_bidding_zones_report_a_stale_list(tmp_path: Path) -> None:
    from app.spotmarket.metadata import ZoneListing

    class _StaleCache:
        def get(self) -> ZoneListing:
            return ZoneListing(
                zones=("DE-LU",), default="DE-LU", stale=True, last_update=datetime(2026, 1, 1, tzinfo=UTC)
            )

    async with running_app(spot_market_settings(tmp_path)) as harness:
        harness.app.state.spotmarket_zone_cache = _StaleCache()
        headers = await admin_session(harness)
        response = await harness.client.get(SPOT_ZONES, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["stale"] is True


async def test_spot_market_bidding_zones_without_a_stored_list_answer_200_and_never_call_the_provider(
    tmp_path: Path,
) -> None:
    from app.spotmarket.metadata import BiddingZoneMetadataCache

    fetches: list[int] = []

    def fetch():
        fetches.append(1)
        raise SpotMarketConnectionError("down")

    async with running_app(spot_market_settings(tmp_path)) as harness:
        harness.app.state.spotmarket_zone_cache = BiddingZoneMetadataCache(fetch)
        headers = await admin_session(harness)
        for _ in range(2):
            response = await harness.client.get(SPOT_ZONES, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json() == {"zones": [], "default": "", "stale": True, "last_update": None}
        assert fetches == []  # opening the form never reaches the provider


async def test_spot_market_preview_and_dashboard_never_call_the_provider(tmp_path: Path) -> None:
    calls: list[str] = []

    class _Tripwire:
        def __getattr__(self, name):
            def record(*args, **kwargs):
                calls.append(name)
                raise AssertionError("a page view must not reach the provider")

            return record

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _activate_spot_market(harness)
        _stub_background_refreshes(harness)
        headers = await admin_session(harness)
        harness.app.state.spotmarket_provider = _Tripwire()
        harness.app.state.solar_forecast_provider = _Tripwire()
        for _ in range(2):
            assert (await harness.client.post(SPOT_PREVIEW, headers=headers, json=_spot_body())).status_code == 200
            dashboard = await harness.client.get(
                "/admin/api/energy/dashboard-forecast?include=spot_current&include=spot_day_ahead&include=solar",
                headers=headers,
            )
            assert dashboard.status_code == 200
        assert calls == []


#
# Weather / solar forecast admin surface: config round-trip, strict validation, preview and the
# server-side location search. The provider and geocoder are stubbed; no test reaches the network.


from app.weather.base import WeatherConnectionError
from app.weather.geocoding import GeocodingResult

WEATHER_CONFIG = "/admin/api/energy/weather/config"
WEATHER_PREVIEW = "/admin/api/energy/weather/preview"
WEATHER_LOCATIONS = "/admin/api/energy/weather/locations"
WEATHER_TIMEZONE = "/admin/api/energy/weather/timezone"


class _WeatherTimezoneResolver:
    def resolve(self, latitude, longitude):
        return "America/New_York" if longitude < -30 else "Europe/Berlin"


def _stub_weather_timezone(harness) -> None:
    harness.app.state.solar_timezone_resolver = _WeatherTimezoneResolver()


def _weather_body(**overrides) -> dict:
    base = {
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
    return {**base, **overrides}


async def test_weather_config_defaults_then_saves_and_reloads(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        _stub_background_refreshes(harness)
        headers = await admin_session(harness)
        default = await harness.client.get(WEATHER_CONFIG, headers=headers)
        assert default.status_code == 200, default.text
        body = default.json()
        assert body["enabled"] is False
        assert body["latitude"] is None and body["longitude"] is None
        # A config saved before the region field existed degrades to no region line.
        assert body["location_region"] is None
        # Read-only provider metadata is projected for the GUI, never operator input.
        assert body["provider_meta"]["source"] == "Open-Meteo"

        saved = await harness.client.put(WEATHER_CONFIG, headers=headers, json=_weather_body())
        assert saved.status_code == 200, saved.text
        stored = saved.json()
        assert stored["enabled"] is True
        assert stored["location_name"] == "Remscheid, North Rhine-Westphalia, Germany"
        assert stored["location_region"] == "North Rhine-Westphalia, Germany"
        assert stored["latitude"] == pytest.approx(51.1798)
        assert stored["timezone"] == "Europe/Berlin"
        assert stored["pv_peak_power_wp"] == 10000.0

        reloaded = await harness.client.get(WEATHER_CONFIG, headers=headers)
        assert reloaded.json() == stored
        # The in-process copy on app.state is updated too, not only the stored record.
        assert harness.app.state.weather_config.location_name == "Remscheid, North Rhine-Westphalia, Germany"
        assert harness.app.state.weather_config.timezone == "Europe/Berlin"


async def test_weather_config_persists_in_the_admin_store(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        _stub_background_refreshes(harness)
        headers = await admin_session(harness)
        await harness.client.put(WEATHER_CONFIG, headers=headers, json=_weather_body(azimuth_deg=-90))
        stored = harness.app.state.admin_store.get("weather_config")
    assert stored["azimuth_deg"] == -90
    assert stored["provider"] == "open_meteo"


async def test_weather_save_leaves_db_and_runtime_in_agreement(tmp_path: Path) -> None:
    # Finding B invariant: the whole persist + state swap runs in one off-loop critical section, so
    # after a save the stored config and the in-process config agree. The async handler no longer
    # does SQLite work on the event loop, and the transaction stays atomic.
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        _stub_background_refreshes(harness)
        headers = await admin_session(harness)
        saved = await harness.client.put(WEATHER_CONFIG, headers=headers, json=_weather_body(azimuth_deg=-90))
        assert saved.status_code == 200, saved.text
        stored = harness.app.state.admin_store.get("weather_config")
        assert stored["azimuth_deg"] == harness.app.state.weather_config.azimuth_deg == -90


async def test_identical_weather_config_save_preserves_forecast_without_refresh(tmp_path: Path, monkeypatch) -> None:
    from app.admin import energy_api

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        _stub_background_refreshes(harness)
        headers = await admin_session(harness)
        body = _weather_body()
        assert (await harness.client.put(WEATHER_CONFIG, headers=headers, json=body)).status_code == 200
        now = datetime.now(UTC)
        forecast = SolarForecast(now, (
            SolarForecastInterval(now, now + timedelta(minutes=15), 100.0),
        ))
        store = harness.app.state.admin_store
        config = harness.app.state.weather_config
        _seed_solar_forecast(store, config, forecast)
        signals = []
        monkeypatch.setattr(energy_api, "_signal_forecast_refresh", lambda *args: signals.append(args))
        assert (await harness.client.put(WEATHER_CONFIG, headers=headers, json=body)).status_code == 200
        today = now.astimezone(ZoneInfo(config.timezone)).date().isoformat()
        assert len(store.get_solar_forecast_intervals(solar_config_key(config), today, today)) == 1
        assert signals == []


async def test_a_disabled_config_may_be_saved_incomplete(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        headers = await admin_session(harness)
        body = {"enabled": False, "provider": "open_meteo", "latitude": 51.0}
        response = await harness.client.put(WEATHER_CONFIG, headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["enabled"] is False


async def test_an_enabled_config_requires_every_forecast_value(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        headers = await admin_session(harness)
        incomplete = _weather_body()
        del incomplete["pv_peak_power_wp"]
        incomplete["pv_peak_power_wp"] = None
        response = await harness.client.put(WEATHER_CONFIG, headers=headers, json=incomplete)
        assert response.status_code == 422, response.text
        # The 422 carries the real pydantic errors (a list of {loc, msg}) so the form can show which
        # field is missing, never the old opaque "Invalid weather configuration" string.
        detail = response.json()["detail"]
        assert isinstance(detail, list) and detail, detail
        assert all(isinstance(item.get("msg"), str) and "loc" in item for item in detail), detail
        assert any("pv_peak_power_wp" in item["msg"] for item in detail), detail
        assert response.text.count("Invalid weather configuration") == 0


async def test_an_enabled_config_with_an_out_of_range_field_returns_structured_errors(tmp_path: Path) -> None:
    # An out-of-range single field is rejected at the request-body boundary, so it already carries
    # a per-field problem-document entry naming the field; the client renders it inline.
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        headers = await admin_session(harness)
        response = await harness.client.put(WEATHER_CONFIG, headers=headers, json=_weather_body(performance_ratio=1.5))
        assert response.status_code == 422, response.text
        body = response.json()
        assert any(err.get("parameter") == "performance_ratio" for err in body.get("errors", [])), body


async def test_weather_config_rejects_out_of_range_and_unknown_fields(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        headers = await admin_session(harness)
        for bad in (
            _weather_body(tilt_deg=120),
            _weather_body(azimuth_deg=270),
            _weather_body(performance_ratio=1.5),
            _weather_body(provider="made_up"),
            {**_weather_body(), "api_url": "http://127.0.0.1/forecast"},  # extra="forbid"
        ):
            response = await harness.client.put(WEATHER_CONFIG, headers=headers, json=bad)
            assert response.status_code == 422, (bad, response.text)


async def test_weather_config_timezone_follows_coordinates(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        _stub_background_refreshes(harness)
        headers = await admin_session(harness)
        saved = await harness.client.put(
            WEATHER_CONFIG,
            headers=headers,
            json=_weather_body(latitude=40.7128, longitude=-74.0060),
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["timezone"] == "America/New_York"


async def test_weather_config_requires_an_admin_session(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        anonymous = await harness.client.put(WEATHER_CONFIG, json=_weather_body(), headers={"Origin": "http://test"})
        assert anonymous.status_code in (401, 403)


async def test_weather_preview_summarises_yesterday_today_and_tomorrow(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        config = WeatherConfig.model_validate({**_weather_body(), "timezone": "Europe/Berlin"})
        harness.app.state.weather_config = config
        harness.app.state.admin_store.put("weather_config", config.model_dump(mode="json"))
        zone = ZoneInfo(config.timezone)
        midnight = datetime.now(UTC).astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        starts = [
            midnight - timedelta(days=1) + timedelta(hours=12),
            midnight + timedelta(hours=12),
            midnight + timedelta(days=1, hours=12),
        ]
        forecast = SolarForecast(
            created_at=datetime.now(UTC),
            intervals=tuple(
                SolarForecastInterval(
                    start.astimezone(UTC), (start + timedelta(minutes=15)).astimezone(UTC), 1000.0
                )
                for start in starts
            ),
        )
        _seed_solar_forecast(harness.app.state.admin_store, config, forecast)
        headers = await admin_session(harness)
        response = await harness.client.post(WEATHER_PREVIEW, headers=headers, json=_weather_body())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "connected"
        assert body["source"] == "Open-Meteo"
        assert body["timezone"] == "Europe/Berlin"
        assert len(body["yesterday_intervals"]) == 1
        assert len(body["today_intervals"]) == 1
        assert len(body["next_day_intervals"]) == 1
        assert body["yesterday_intervals"][0]["power_kw"] == pytest.approx(4.0)
        assert body["today_intervals"][0]["power_kw"] == pytest.approx(4.0)
        assert body["next_day_intervals"][0]["power_kw"] == pytest.approx(4.0)
        # Exactly one of the two noon intervals (today/tomorrow) falls inside the rolling next 24h.
        assert body["energy_next_24h_kwh"] == pytest.approx(1.0)


async def test_weather_preview_reports_disabled_for_an_incomplete_config(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        headers = await admin_session(harness)
        response = await harness.client.post(
            WEATHER_PREVIEW, headers=headers, json={"enabled": False, "provider": "open_meteo"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "disabled"


async def test_weather_preview_reports_disabled_for_an_enabled_but_incomplete_config(tmp_path: Path) -> None:
    # Flipping enable on before the forecast fields are filled must not 422 the preview: an
    # enabled-but-incomplete config is simply not forecastable yet and reports the clean disabled
    # view, so the operator fills the form without hitting an opaque error.
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        headers = await admin_session(harness)
        response = await harness.client.post(
            WEATHER_PREVIEW, headers=headers, json={"enabled": True, "provider": "open_meteo"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "disabled"


async def test_weather_preview_still_rejects_an_out_of_range_field(tmp_path: Path) -> None:
    # A genuinely invalid field value is still a structured 422 in the preview path; only the
    # enabled-but-incomplete case degrades to the disabled view.
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        headers = await admin_session(harness)
        response = await harness.client.post(WEATHER_PREVIEW, headers=headers, json=_weather_body(tilt_deg=120))
        assert response.status_code == 422, response.text
        body = response.json()
        assert any(err.get("parameter") == "tilt_deg" for err in body.get("errors", [])), body


async def test_weather_preview_reports_an_empty_database_without_fetching_provider(tmp_path: Path) -> None:
    class _ProviderTripwire:
        def fetch_preview(self, config, now):
            raise AssertionError("preview requests must not fetch provider data")

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        config = WeatherConfig.model_validate({**_weather_body(), "timezone": "Europe/Berlin"})
        harness.app.state.weather_config = config
        harness.app.state.admin_store.put("weather_config", config.model_dump(mode="json"))
        harness.app.state.solar_forecast_provider = _ProviderTripwire()
        headers = await admin_session(harness)
        response = await harness.client.post(WEATHER_PREVIEW, headers=headers, json=_weather_body())
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "unavailable"
        assert "background" in response.json()["message"]


async def test_weather_preview_of_an_unsaved_configuration_reports_unsaved_without_message(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        config = WeatherConfig.model_validate({**_weather_body(), "timezone": "Europe/Berlin"})
        harness.app.state.weather_config = config
        harness.app.state.admin_store.put("weather_config", config.model_dump(mode="json"))
        headers = await admin_session(harness)
        response = await harness.client.post(
            WEATHER_PREVIEW, headers=headers, json=_weather_body(latitude=49.8728, longitude=8.6512),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "unsaved"
        # The saved zone belongs to the old location and must not leak into the unsaved view.
        assert body["message"] is None and body["timezone"] is None


async def test_weather_locations_returns_mapped_hits(tmp_path: Path) -> None:
    class _StubGeocoder:
        def search(self, query, *, count, country_code=None):
            assert query == "Rem"
            return [
                GeocodingResult(
                    name="Remscheid", latitude=51.1798, longitude=7.1925, country="Germany",
                    country_code="de", admin1="North Rhine-Westphalia", admin2="Remscheid",
                    district="Lüttringhausen", city="Remscheid", timezone=None, postal_code="42857",
                ),
                GeocodingResult(
                    name="Remagen", latitude=50.579, longitude=7.227, country="Germany",
                    country_code="de", admin1="Rhineland-Palatinate", admin2=None,
                    city=None, timezone=None, postal_code=None,
                ),
            ]

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        harness.app.state.solar_geocoder = _StubGeocoder()
        headers = await admin_session(harness)
        response = await harness.client.get(f"{WEATHER_LOCATIONS}?q=Rem", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ok"
        assert [r["name"] for r in body["results"]] == ["Remscheid", "Remagen"]
        # admin2, district, country_code and the canonical postal_code are projected for display
        # (not persisted).
        assert body["results"][0]["admin2"] == "Remscheid"
        assert body["results"][0]["district"] == "Lüttringhausen"
        assert body["results"][0]["country_code"] == "de"
        assert body["results"][0]["postal_code"] == "42857"
        assert body["results"][1]["postal_code"] is None


async def test_weather_config_attribution_reflects_the_active_geocoder(tmp_path: Path) -> None:
    from app.weather.geocoding import OpenMeteoGeocoder, PhotonGeocoder

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        headers = await admin_session(harness)

        harness.app.state.solar_geocoder = PhotonGeocoder()
        meta = (await harness.client.get(WEATHER_CONFIG, headers=headers)).json()["provider_meta"]
        assert "Open-Meteo" in meta["attribution"]
        assert "OpenStreetMap" in meta["location_attribution"]

        harness.app.state.solar_geocoder = OpenMeteoGeocoder()
        meta = (await harness.client.get(WEATHER_CONFIG, headers=headers)).json()["provider_meta"]
        assert "Open-Meteo" in meta["attribution"]
        assert "GeoNames" in meta["location_attribution"]


async def test_weather_locations_forwards_optional_country_code(tmp_path: Path) -> None:
    class _RecordingGeocoder:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def search(self, query, *, count, country_code=None):
            self.calls.append(country_code)
            return []

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        geocoder = _RecordingGeocoder()
        harness.app.state.solar_geocoder = geocoder
        headers = await admin_session(harness)
        with_code = await harness.client.get(f"{WEATHER_LOCATIONS}?q=London&country_code=gb", headers=headers)
        without_code = await harness.client.get(f"{WEATHER_LOCATIONS}?q=London", headers=headers)
        assert with_code.status_code == without_code.status_code == 200
        assert geocoder.calls == ["gb", None]


async def test_weather_locations_handles_an_empty_result(tmp_path: Path) -> None:
    class _EmptyGeocoder:
        def search(self, query, *, count, country_code=None):
            return []

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        harness.app.state.solar_geocoder = _EmptyGeocoder()
        headers = await admin_session(harness)
        response = await harness.client.get(f"{WEATHER_LOCATIONS}?q=zzzzz", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ok", "message": None, "results": []}


async def test_weather_locations_reports_a_geocoder_failure_as_a_status(tmp_path: Path) -> None:
    class _DeadGeocoder:
        def search(self, query, *, count, country_code=None):
            raise WeatherConnectionError("geocoding is unreachable")

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        harness.app.state.solar_geocoder = _DeadGeocoder()
        headers = await admin_session(harness)
        response = await harness.client.get(f"{WEATHER_LOCATIONS}?q=Rem", headers=headers)
        # A dead geocoder is a 200 with status=error, never a 5xx, so location search cannot look
        # like a server fault or reach the inverter control path.
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "error"
        assert "unreachable" in response.json()["message"]


async def test_weather_locations_short_query_makes_no_geocoder_call(tmp_path: Path) -> None:
    class _TrippedGeocoder:
        def search(self, query, *, count, country_code=None):
            raise AssertionError("a one-character query must not reach the geocoder")

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        harness.app.state.solar_geocoder = _TrippedGeocoder()
        headers = await admin_session(harness)
        response = await harness.client.get(f"{WEATHER_LOCATIONS}?q=R", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["results"] == []


async def test_weather_timezone_is_derived_from_coordinates(tmp_path: Path) -> None:
    # Photon carries no zone, so the server derives it from lat/lon. The resolver is stubbed; no
    # test reaches the network. Remscheid 51.18/7.19 -> Europe/Berlin.
    class _StubResolver:
        def resolve(self, latitude, longitude):
            assert (round(latitude, 2), round(longitude, 2)) == (51.18, 7.19)
            return "Europe/Berlin"

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        harness.app.state.solar_timezone_resolver = _StubResolver()
        headers = await admin_session(harness)
        response = await harness.client.get(
            f"{WEATHER_TIMEZONE}?latitude=51.18&longitude=7.19", headers=headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body == {"status": "ok", "message": None, "timezone": "Europe/Berlin"}


async def test_weather_timezone_resolves_a_second_zone(tmp_path: Path) -> None:
    class _StubResolver:
        def resolve(self, latitude, longitude):
            return "America/New_York"

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        harness.app.state.solar_timezone_resolver = _StubResolver()
        headers = await admin_session(harness)
        response = await harness.client.get(
            f"{WEATHER_TIMEZONE}?latitude=40.7128&longitude=-74.0060", headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["timezone"] == "America/New_York"


async def test_weather_timezone_reports_a_derivation_failure_as_a_status(tmp_path: Path) -> None:
    class _DeadResolver:
        def resolve(self, latitude, longitude):
            raise WeatherConnectionError("Open-Meteo timezone lookup is unreachable")

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        harness.app.state.solar_timezone_resolver = _DeadResolver()
        headers = await admin_session(harness)
        response = await harness.client.get(
            f"{WEATHER_TIMEZONE}?latitude=51.18&longitude=7.19", headers=headers
        )
        # A failed derivation is a 200 with status=error, never a 5xx: it stays isolated from the
        # inverter/dispatch/forecast path and never claims an empty or wrong zone as a success.
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "error"
        assert body["timezone"] is None
        assert "unreachable" in body["message"]


async def test_weather_timezone_rejects_out_of_range_coordinates(tmp_path: Path) -> None:
    class _TrippedResolver:
        def resolve(self, latitude, longitude):
            raise AssertionError("out-of-range coordinates must be rejected before the resolver")

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        harness.app.state.solar_timezone_resolver = _TrippedResolver()
        headers = await admin_session(harness)
        response = await harness.client.get(
            f"{WEATHER_TIMEZONE}?latitude=120&longitude=7.19", headers=headers
        )
        # An out-of-range coordinate is a request-boundary 422, before the resolver is consulted.
        assert response.status_code == 422, response.text


async def test_weather_config_derives_timezone_and_rejects_timezone_input(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        _stub_background_refreshes(harness)
        headers = await admin_session(harness)
        saved = await harness.client.put(WEATHER_CONFIG, headers=headers, json=_weather_body())
        assert saved.status_code == 200, saved.text
        assert saved.json()["timezone"] == "Europe/Berlin"
        assert harness.app.state.weather_config.timezone == "Europe/Berlin"

        # Timezone is not part of the request contract: coordinates are the sole authority.
        rejected = await harness.client.put(
            WEATHER_CONFIG,
            headers=headers,
            json={**_weather_body(), "timezone": "America/New_York"},
        )
        assert rejected.status_code == 422, rejected.text


async def test_dashboard_forecast_include_solar_loads_only_the_solar_projection(tmp_path: Path) -> None:
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    forecast = SolarForecast(
        created_at=datetime.now(UTC),
        intervals=(SolarForecastInterval(start, start + timedelta(minutes=15), 500.0),),
    )

    async with running_app(spot_market_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        config = WeatherConfig.model_validate({**_weather_body(), "timezone": "Europe/Berlin"})
        harness.app.state.weather_config = config
        harness.app.state.admin_store.put("weather_config", config.model_dump(mode="json"))
        _seed_solar_forecast(harness.app.state.admin_store, config, forecast)
        response = await harness.client.get(
            "/admin/api/energy/dashboard-forecast?include=solar", headers=headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "solar" in body and "spot" not in body
        assert body["solar"]["source"] == "Open-Meteo"


async def test_dashboard_forecast_solar_is_disabled_without_a_forecastable_config(tmp_path: Path) -> None:
    async with running_app(spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        headers = await admin_session(harness)

        class _Tripwire:
            def get(self, config):
                raise AssertionError("a disabled config must not touch the solar cache")

        harness.app.state.solar_forecast_provider = _Tripwire()
        response = await harness.client.get(
            "/admin/api/energy/dashboard-forecast?include=solar", headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["solar"]["status"] == "disabled"


async def test_spot_market_preview_reports_a_failed_daily_retrieval(tmp_path: Path) -> None:
    from app.admin.forecast_refresh import SPOT_STATUS_KEY, record_refresh_status

    today = datetime.now(UTC).date()

    def day(offset: int) -> SpotPriceInterval:
        start = datetime.combine(today + timedelta(days=offset), datetime.min.time(), tzinfo=UTC)
        return SpotPriceInterval(start, start + timedelta(minutes=15), Decimal("0.2"))

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _activate_spot_market(harness)
        store = harness.app.state.admin_store
        key = spot_config_key(harness.app.state.spotmarket_config)
        headers = await admin_session(harness)

        # No stored data: the failure replaces the endless "being loaded" hint, without the raw cause.
        record_refresh_status(store, SPOT_STATUS_KEY, key, False, "provider down")
        empty = (await harness.client.post(SPOT_PREVIEW, headers=headers, json=_spot_body())).json()
        assert empty["status"] == "error"
        assert "provider down" not in empty["message"] and empty["refresh_error"] == "provider down"

        # Today is missing although it is due: stored data stays visible and the warning appears.
        _seed_spot_prices(store, [day(-1)])
        stale = (await harness.client.post(SPOT_PREVIEW, headers=headers, json=_spot_body())).json()
        assert stale["status"] == "connected" and stale["refresh_error"] == "provider down"
        assert stale["stale"] is True
        dashboard = (await harness.client.get(
            "/admin/api/energy/dashboard-forecast?include=spot_day_ahead", headers=headers)).json()["spot"]
        assert dashboard["status"] == "stale" and dashboard["refresh_error"] == "provider down"

        # All three displayed days are stored: nothing is missing, so an old failure raises no warning.
        _seed_spot_prices(store, [day(-1), day(0), day(1)])
        calm = (await harness.client.post(SPOT_PREVIEW, headers=headers, json=_spot_body())).json()
        assert calm["stale"] is False and calm["refresh_error"] == "provider down"
        dashboard = (await harness.client.get(
            "/admin/api/energy/dashboard-forecast?include=spot_day_ahead", headers=headers)).json()["spot"]
        assert dashboard["status"] == "available"

        record_refresh_status(store, SPOT_STATUS_KEY, key, True)
        ok = (await harness.client.post(SPOT_PREVIEW, headers=headers, json=_spot_body())).json()
        assert ok["refresh_error"] is None and ok["refresh_error_at"] is None


async def test_weather_preview_and_dashboard_report_a_failed_background_refresh(tmp_path: Path) -> None:
    from app.admin.forecast_refresh import SOLAR_STATUS_KEY, record_refresh_status

    async with running_app(spot_market_settings(tmp_path)) as harness:
        headers = await admin_session(harness)
        config = WeatherConfig.model_validate({**_weather_body(), "timezone": "Europe/Berlin"})
        harness.app.state.weather_config = config
        store = harness.app.state.admin_store
        record_refresh_status(store, SOLAR_STATUS_KEY, solar_config_key(config), False, "weather down")
        preview = (await harness.client.post(WEATHER_PREVIEW, headers=headers, json=_weather_body())).json()
        assert preview["status"] == "error" and preview["refresh_error"] == "weather down"
        dashboard = (await harness.client.get(
            "/admin/api/energy/dashboard-forecast?include=solar", headers=headers)).json()["solar"]
        assert dashboard["status"] == "error" and dashboard["refresh_error"] == "weather down"


async def test_config_save_clears_rows_under_the_refresh_lock_and_blocks_a_stale_refresh(tmp_path: Path) -> None:
    from app.admin.forecast_refresh import refresh_spot_market

    today = datetime.now(UTC).date()
    midnight = datetime.combine(today, datetime.min.time(), tzinfo=UTC)

    class _Provider:
        """Simulates a save for another zone landing while the old-zone fetch is in flight."""

        def __init__(self, harness):
            self.harness = harness

        def _snapshot(self, config):
            return SpotPriceSnapshot(
                config.provider, config.bidding_zone, "EUR", datetime.now(UTC),
                (SpotPriceInterval(midnight, midnight + timedelta(minutes=15), Decimal("0.1")),), timezone="UTC",
            )

        def fetch_current(self, config):
            return self._snapshot(config)

        def fetch_today(self, config):
            self.harness.app.state.spotmarket_config = SpotMarketConfig.model_validate(
                {**_spot_body(), "bidding_zone": "AT"}
            )
            return self._snapshot(config)

        def fetch_yesterday(self, config, timezone):
            return self._snapshot(config)

        def fetch_day_ahead(self, config):
            return self._snapshot(config)

    async with running_app(spot_market_settings(tmp_path)) as harness:
        _activate_spot_market(harness)
        store = harness.app.state.admin_store
        old = harness.app.state.spotmarket_config
        old_key = spot_config_key(old)
        wrote = await refresh_spot_market(
            store, _Provider(harness), old,
            lambda: spot_config_key(harness.app.state.spotmarket_config) == old_key,
        )
        assert wrote is False
        assert store.get_spot_market_intervals(old_key) == []


def test_dry_run_price_disclaimer_matches_the_strict_latest_wins_planner() -> None:
    """P3.3: the price text must not claim the price breaks ties between equivalent slots.

    The V1 planner is strict latest-wins (``planner.py`` sorts candidates ``reverse=True``; a later
    slot wins even when an earlier one is cheaper), so the day-ahead price never moves charging into
    an earlier slot. The disclaimer and the model docstring must say that, not the old tie-breaker
    claim, and must still flag that no absolute household cost is implied.
    """
    from app.admin.energy_api import DryRunPlanView

    view = DryRunPlanView(device_id="main", status="ok", horizon_hours=1.0)
    disclaimer = view.price_disclaimer
    assert "breaks ties" not in disclaimer
    assert "equivalent slots" not in disclaimer
    assert "never moves charging into an earlier slot" in disclaimer
    assert "household cost" in disclaimer
    docstring = DryRunPlanView.__doc__ or ""
    assert "break\n    ties" not in docstring and "ties between slots equivalent" not in docstring
