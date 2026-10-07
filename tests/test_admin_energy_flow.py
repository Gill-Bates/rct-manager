#
# tests/test_admin_energy_flow.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The read-only ``energy_flow`` projection on ``GET /admin/api/devices`` (dashboard stage 1).

Independent of dispatch/Energy Manager state by construction: the projection is sourced from
``RctEnergyReadings`` (cache-only, server-decoded sign conventions), never from the dispatch
subsystem, so it must be present and correctly populated whether or not write support is enabled.
"""

import asyncio
from pathlib import Path

import pytest

from app.admin.store import AdminStore
from app.catalog.registry import RegistryCatalog
from tests.api_helpers import READ_TOKEN, float_payload, make_settings, running_app

PUBLIC_READER = {"Authorization": f"Bearer {READ_TOKEN}"}

PASSWORD = "replacement-test-password"
SECRET = "s" * 48
FLOW_KEYS = ("pv_power_w", "grid_power_w", "house_load_w", "battery_power_w", "battery_soc_percent")
FLOW_METRIC_NAMES = (
    "battery_soc", "grid_power", "battery_power", "household_load_power", "solar_a_power", "solar_b_power",
)


def bootstrapped_settings(tmp_path: Path, **overrides):
    """``GET /admin/api/devices`` is gated by ``require_admin``, which checks the admin store at
    ``settings.admin_db_path`` directly; ``running_app`` only patches a *separate* token store for
    the public API, so the real admin store needs its first-start password changed up front (same
    pattern as ``tests/test_energy_api.py::energy_settings``) or the device never leaves
    ``starting`` (periodic reads stay paused until the admin password is changed). Returns the
    settings and a bearer header for a read-scoped admin PAT.
    """
    admin_db_path = tmp_path / "rct.db"
    store = AdminStore(admin_db_path, SECRET)
    password = store.initialize()
    assert store.change_password(password, PASSWORD)
    token = store.create_token("read test token", "read", None)[1]
    store.close()
    settings = make_settings(hmac_secret=SECRET, admin_db_path=admin_db_path, **overrides)
    return settings, {"Authorization": f"Bearer {token}"}


def seed_flow_payloads(harness) -> None:
    """Fresh cache values for every figure the projection reads, device-side sign on the wire."""
    catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
    values = {
        "battery_soc": float_payload(0.5),  # ratio -> 50.0 %
        "grid_power": float_payload(1200.0),  # RctGridPowerConvention default import_positive=True
        "battery_power": float_payload(-300.0),  # RctBatteryPowerConvention default discharge_positive=True
        "household_load_power": float_payload(800.0),
        "solar_a_power": float_payload(1500.0),
        "solar_b_power": float_payload(900.0),
    }
    for name, payload in values.items():
        harness.net.payloads[catalog.object_entry(name).object_id] = payload


async def fill_cache(harness) -> None:
    """Reads through the public API's own token store, which is independent of the admin store."""
    for name in FLOW_METRIC_NAMES:
        filled = await harness.client.get(f"/api/v1/devices/main/metrics/{name}", headers=PUBLIC_READER)
        assert filled.status_code == 200, filled.text


async def fetch_devices(harness, headers: dict) -> list[dict]:
    response = await harness.client.get("/admin/api/devices", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["devices"]


@pytest.mark.asyncio
async def test_energy_flow_present_and_correctly_signed_with_fresh_readings(tmp_path: Path):
    settings, headers = bootstrapped_settings(tmp_path)
    async with running_app(settings) as harness:
        seed_flow_payloads(harness)
        await fill_cache(harness)  # one ordinary read per figure fills the projection's cache

        devices = await fetch_devices(harness, headers)
        main = next(d for d in devices if d["id"] == "main")
        flow = main["energy_flow"]
        assert set(flow) == set(FLOW_KEYS)
        for key in FLOW_KEYS:
            assert set(flow[key]) == {"value", "stale", "age_seconds"}
        assert flow["pv_power_w"]["value"] == pytest.approx(2400.0)  # solar_a + solar_b
        assert flow["house_load_w"]["value"] == pytest.approx(800.0)
        # Default capability assumptions: grid import_positive=True, battery discharge_positive=True.
        assert flow["grid_power_w"]["value"] == pytest.approx(1200.0)  # positive = import
        assert flow["battery_power_w"]["value"] == pytest.approx(-300.0)  # negative = charging
        assert flow["battery_soc_percent"]["value"] == pytest.approx(50.0)
        assert all(not flow[key]["stale"] for key in FLOW_KEYS)
        assert all(flow[key]["age_seconds"] is not None for key in FLOW_KEYS)


@pytest.mark.asyncio
async def test_energy_flow_stale_reading_reports_stale_true_and_an_age(tmp_path: Path):
    settings, headers = bootstrapped_settings(tmp_path, cache_ttl_seconds=0, enable_periodic_reads=False)
    async with running_app(settings) as harness:
        seed_flow_payloads(harness)
        await fill_cache(harness)
        await asyncio.sleep(0.05)  # past the zero TTL, still inside the (default) grace window

        devices = await fetch_devices(harness, headers)
        main = next(d for d in devices if d["id"] == "main")
        flow = main["energy_flow"]
        for key in FLOW_KEYS:
            assert flow[key]["stale"] is True, key
            assert flow[key]["age_seconds"] is not None, key
        assert flow["pv_power_w"]["value"] == pytest.approx(2400.0)


@pytest.mark.asyncio
async def test_energy_flow_absent_device_is_five_null_stale_fields(tmp_path: Path):
    settings, headers = bootstrapped_settings(tmp_path, enable_periodic_reads=False)
    async with running_app(settings) as harness:
        devices = await fetch_devices(harness, headers)
        main = next(d for d in devices if d["id"] == "main")
        flow = main["energy_flow"]
        assert set(flow) == set(FLOW_KEYS)
        for key in FLOW_KEYS:
            assert flow[key] == {"value": None, "stale": True, "age_seconds": None}


@pytest.mark.asyncio
async def test_energy_flow_present_with_write_support_disabled(tmp_path: Path):
    """Regression for the dispatch-independent wiring: no dispatch/Energy Manager required."""
    settings, headers = bootstrapped_settings(tmp_path, enable_write_support=False)
    async with running_app(settings) as harness:
        assert harness.runtime.dispatch is None
        assert harness.runtime.energy_readings is not None
        seed_flow_payloads(harness)
        await fill_cache(harness)

        devices = await fetch_devices(harness, headers)
        main = next(d for d in devices if d["id"] == "main")
        flow = main["energy_flow"]
        assert set(flow) == set(FLOW_KEYS)
        assert flow["pv_power_w"]["value"] == pytest.approx(2400.0)
        assert all(not flow[key]["stale"] for key in FLOW_KEYS)
