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
import re
from pathlib import Path

import pytest
from starlette.requests import Request

from app.admin.store import AdminStore
from app.api.app_factory import (
    _ENERGY_METRIC_NAMES,
    _effective_periodic_names,
    create_app,
)
from app.api.problems import _is_write_path
from app.catalog.base import is_numeric
from app.catalog.registry import RegistryCatalog
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


# --- item 19: the happy path per action -----------------------------------------------------------


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


# --- item 20: refusals ----------------------------------------------------------------------------


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


# --- item 21: one GET serves the whole card -------------------------------------------------------


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


# --- item 22: _is_write_path ----------------------------------------------------------------------


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


# --- item 22b: the shutdown drain -----------------------------------------------------------------


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


# --- item 22c: the periodic wiring ----------------------------------------------------------------


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


# --- item 23: the admin surface -------------------------------------------------------------------


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


# --- item 24: OpenAPI purity ----------------------------------------------------------------------


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


# --- item 25: source purity -----------------------------------------------------------------------


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
        assert on.json()["restart_required"] == []
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
        assert off.json()["restart_required"] == []
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


# --- operating modes ------------------------------------------------------------------------------


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


# --- guided hardware verification (Basic mode assistant) -------------------------------------------

ASSIST = "/admin/api/energy/devices/main/verification-assistant"


def seed_identity(harness, *, battery_w: float = 400.0, target: float = 0.5) -> None:
    catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
    for name, text in (("android_description", "RCT Power DC 10.0"), ("svnversion", "2.3.5687")):
        entry = catalog.object_entry(name)
        harness.net.payloads[entry.object_id] = encode_value(entry.data_type, text, byte_width=entry.byte_width)
    harness.net.payloads[catalog.object_entry("battery_power").object_id] = float_payload(battery_w)
    harness.net.payloads[catalog.object_entry("power_mng_soc_target_set").object_id] = float_payload(target)


async def follow_the_setpoint(harness, stop: asyncio.Event) -> None:
    """Stand-in for the inverter's own control: with the external strategy active, battery power
    follows the written setpoint."""
    catalog = RegistryCatalog.from_file(harness.runtime.settings.object_registry_path)
    strategy = catalog.object_entry("power_mng_soc_strategy").object_id
    extern = catalog.object_entry("power_mng_battery_power_extern").object_id
    power = catalog.object_entry("battery_power").object_id
    while not stop.is_set():
        if harness.net.payloads.get(strategy) == b"\x01":
            harness.net.payloads[power] = harness.net.payloads[extern]
        await asyncio.sleep(0.005)


@pytest.fixture
def fast_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.admin.energy_verification.PROBE_POLL_S", 0.02)
    monkeypatch.setattr("app.admin.energy_verification.PROBE_TIMEOUT_S", 0.5)


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
        seed_identity(harness, battery_w=400.0)
        await harness.client.post(f"{ASSIST}/direction", headers=headers, json={"kind": "battery", "answer": "charging"})
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
