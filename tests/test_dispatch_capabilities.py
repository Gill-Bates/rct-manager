#!/usr/bin/env python3
#
# tests/test_dispatch_capabilities.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Per-device hardware capabilities: model, gate, port, adapter and the dispatch lock.

The invariant under test is that unverified hardware cannot be driven: the shipping state blocks
the affected mode for exactly one device, a verification of device A never releases device B, and
the only way onto unverified hardware is that device's engineering mode with its shorter TTL cap.
"""

import asyncio
import contextlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from app.dispatch.capabilities import (
    CapabilityName,
    CapabilityRecord,
    CapabilityRegistry,
    CapabilityStatus,
    evaluate_gate,
    required_for,
)
from app.dispatch.models import (
    DeviceControlSnapshot,
    DeviceLimits,
    DispatchCommand,
    DispatchConfig,
    DispatchIntent,
    DispatchMode,
    DispatchRecord,
    DispatchState,
    PowerSetpoint,
)
from app.dispatch.store import DispatchStore
from app.errors import DeviceApiError
from tests.conftest import ManualClock
from tests.test_dispatch_core import (
    FakeDispatchGateway,
    controller_with_store,
    verified_registry,
)

VERIFIED_AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def verified(
    device_id: str,
    name: CapabilityName,
    *,
    soc_strategy_external_code: int = 1,
    **overrides,
) -> CapabilityRecord:
    """A record carrying every partial condition its capability needs for ``verified``."""
    evidence: dict[str, object] = {
        "verified_device_model": "RCT-Power-Storage-DC",
        "verified_firmware": "1.0.0",
        "verified_at": VERIFIED_AT,
        "verified_by": "tester",
    }
    if name is CapabilityName.WRITE_PATH:
        evidence.update(
            soc_strategy_external_code=soc_strategy_external_code,
            enum_byte_width=1,
            bool_byte_width=1,
            write_frame_layout_verified=True,
            apply_sequence_verified=True,
            sequence_order_relevant=True,
        )
    if name is CapabilityName.EXPORT_LIMIT:
        evidence["export_limit_zero_blocks_export"] = True
    if name is CapabilityName.SETPOINT_VOLATILITY:
        evidence.update(volatile=False)
    return CapabilityRecord(
        device_id=device_id,
        name=name,
        status=CapabilityStatus.VERIFIED,
        **{**evidence, **overrides},
    )


def registry_for(device_id: str, *names: CapabilityName, **kwargs) -> CapabilityRegistry:
    return CapabilityRegistry(verified(device_id, name, **kwargs) for name in names)


CHARGE_CAPABILITIES = (CapabilityName.WRITE_PATH, CapabilityName.BATTERY_POWER_SIGN)
DISCHARGE_CAPABILITIES = (*CHARGE_CAPABILITIES, CapabilityName.GRID_POWER_SIGN)


def test_a_never_set_capability_is_unverified_with_the_assumption_defaults() -> None:
    """REQ-061: a fresh database carries no release, only the assumptions the adapter works with."""
    record = CapabilityRegistry().record("main", CapabilityName.WRITE_PATH)
    assert record.status is CapabilityStatus.UNVERIFIED
    assert record.battery_discharge_positive is True  # assumption AN-1, not a release
    assert record.grid_import_positive is True
    assert record.soc_strategy_external_code is None
    assert record.verified_at is None and record.verified_by is None


def test_required_capabilities_per_mode_and_export_limit() -> None:
    assert required_for(DispatchMode.CHARGE_FROM_GRID, limit_export=False) == frozenset(CHARGE_CAPABILITIES)
    assert required_for(DispatchMode.CHARGE_FROM_GRID, limit_export=True) == frozenset(CHARGE_CAPABILITIES)
    assert required_for(DispatchMode.DISCHARGE_TO_LOAD, limit_export=False) == frozenset(
        DISCHARGE_CAPABILITIES
    )
    assert required_for(DispatchMode.DISCHARGE_TO_LOAD, limit_export=True) == frozenset(
        (*DISCHARGE_CAPABILITIES, CapabilityName.EXPORT_LIMIT)
    )
    # EXPORT_TO_GRID is refused with dispatch_mode_unavailable long before the gate.
    assert required_for(DispatchMode.EXPORT_TO_GRID, limit_export=True) == frozenset()


def test_setpoint_volatility_feeds_no_gate() -> None:
    registry = registry_for("main", *DISCHARGE_CAPABILITIES)
    assert registry.record("main", CapabilityName.SETPOINT_VOLATILITY).status is CapabilityStatus.UNVERIFIED
    decision = evaluate_gate(
        DispatchMode.DISCHARGE_TO_LOAD,
        registry,
        device_id="main",
        device_engineering_mode=False,
        limit_export=False,
    )
    assert decision.allowed is True


def test_gate_allows_verified_hardware_without_the_engineering_switch() -> None:
    decision = evaluate_gate(
        DispatchMode.CHARGE_FROM_GRID,
        registry_for("main", *CHARGE_CAPABILITIES),
        device_id="main",
        device_engineering_mode=False,
        limit_export=False,
    )
    assert decision == type(decision)(allowed=True, engineering_mode=False, unverified=())


def test_a_forgotten_engineering_switch_does_not_devalue_a_verification() -> None:
    """Table row 2: verified hardware stays productive; ``active`` is false, so the TTL stays long."""
    decision = evaluate_gate(
        DispatchMode.CHARGE_FROM_GRID,
        registry_for("main", *CHARGE_CAPABILITIES),
        device_id="main",
        device_engineering_mode=True,
        limit_export=False,
    )
    assert decision.allowed is True
    assert decision.engineering_mode is False  # enabled, but not active
    assert decision.unverified == ()


def test_engineering_mode_with_the_strategy_code_drives_unverified_hardware_as_active() -> None:
    registry = CapabilityRegistry(
        [
            CapabilityRecord(
                device_id="main",
                name=CapabilityName.WRITE_PATH,
                soc_strategy_external_code=1,  # settable while unverified: V-06 is what is measured
            )
        ]
    )
    decision = evaluate_gate(
        DispatchMode.CHARGE_FROM_GRID,
        registry,
        device_id="main",
        device_engineering_mode=True,
        limit_export=False,
    )
    assert decision.allowed is True
    assert decision.engineering_mode is True
    assert decision.unverified == (CapabilityName.BATTERY_POWER_SIGN, CapabilityName.WRITE_PATH)


def test_engineering_mode_without_the_strategy_code_is_refused_with_a_speaking_detail() -> None:
    decision = evaluate_gate(
        DispatchMode.CHARGE_FROM_GRID,
        CapabilityRegistry(),
        device_id="main",
        device_engineering_mode=True,
        limit_export=False,
    )
    assert decision.allowed is False
    assert decision.engineering_mode is False
    assert decision.reject_detail == "soc_strategy_external_code required for engineering mode"


def test_gate_refuses_unverified_hardware_and_names_the_missing_capabilities() -> None:
    decision = evaluate_gate(
        DispatchMode.DISCHARGE_TO_LOAD,
        registry_for("main", CapabilityName.WRITE_PATH),
        device_id="main",
        device_engineering_mode=False,
        limit_export=False,
    )
    assert decision.allowed is False
    assert decision.unverified == (CapabilityName.BATTERY_POWER_SIGN, CapabilityName.GRID_POWER_SIGN)
    assert decision.reject_detail is not None
    assert "battery_power_sign_convention" in decision.reject_detail
    assert "grid_power_sign_convention" in decision.reject_detail


def test_export_limit_is_only_required_when_the_export_write_is_switched_on() -> None:
    registry = registry_for("main", *DISCHARGE_CAPABILITIES)
    assert (
        evaluate_gate(
            DispatchMode.DISCHARGE_TO_LOAD,
            registry,
            device_id="main",
            device_engineering_mode=False,
            limit_export=False,
        ).allowed
        is True
    )
    blocked = evaluate_gate(
        DispatchMode.DISCHARGE_TO_LOAD,
        registry,
        device_id="main",
        device_engineering_mode=False,
        limit_export=True,
    )
    assert blocked.allowed is False
    assert blocked.unverified == (CapabilityName.EXPORT_LIMIT,)


def test_verifying_device_a_does_not_release_device_b() -> None:
    """D-01: capabilities are per device; model equality inherits nothing."""
    registry = registry_for("main", *CHARGE_CAPABILITIES)
    for device_id, allowed in (("main", True), ("slave1", False)):
        decision = evaluate_gate(
            DispatchMode.CHARGE_FROM_GRID,
            registry,
            device_id=device_id,
            device_engineering_mode=False,
            limit_export=False,
        )
        assert decision.allowed is allowed, device_id
    assert registry.record("slave1", CapabilityName.WRITE_PATH).status is CapabilityStatus.UNVERIFIED


def test_engineering_mode_of_device_a_does_not_release_device_b() -> None:
    registry = CapabilityRegistry(
        [CapabilityRecord("main", CapabilityName.WRITE_PATH, soc_strategy_external_code=1)]
    )
    assert (
        evaluate_gate(
            DispatchMode.CHARGE_FROM_GRID,
            registry,
            device_id="slave1",
            device_engineering_mode=False,
            limit_export=False,
        ).allowed
        is False
    )


def test_replace_takes_a_committed_record_over_live() -> None:
    registry = CapabilityRegistry()
    assert registry.record("main", CapabilityName.GRID_POWER_SIGN).status is CapabilityStatus.UNVERIFIED
    registry.replace(verified("main", CapabilityName.GRID_POWER_SIGN, grid_import_positive=False))
    stored = registry.record("main", CapabilityName.GRID_POWER_SIGN)
    assert stored.status is CapabilityStatus.VERIFIED
    assert stored.grid_import_positive is False


def test_all_lists_every_capability_of_one_device_including_the_unset_ones() -> None:
    registry = registry_for("main", CapabilityName.WRITE_PATH)
    records = registry.all("main")
    assert {record.name for record in records} == set(CapabilityName)
    assert all(record.device_id == "main" for record in records)
    assert [record.name for record in records if record.status is CapabilityStatus.VERIFIED] == [
        CapabilityName.WRITE_PATH
    ]


def test_capability_record_round_trips_through_its_dict_form() -> None:
    record = verified("main", CapabilityName.WRITE_PATH, note="checked on site")
    restored = CapabilityRecord.from_dict(record.to_dict())
    assert restored == record


def test_capability_record_dict_form_is_json_serializable() -> None:
    data = json.loads(json.dumps(verified("main", CapabilityName.WRITE_PATH).to_dict()))
    assert data["verified_at"] == VERIFIED_AT.isoformat()
    assert data["status"] == "verified"
    assert CapabilityRecord.from_dict(data).verified_at == VERIFIED_AT


@pytest.mark.parametrize("note", ["x" * 201, "tab\tcharacter", "ümlaut"])
def test_capability_note_is_bounded_printable_ascii(note: str) -> None:
    with pytest.raises(ValueError):
        CapabilityRecord("main", CapabilityName.WRITE_PATH, note=note)


def test_verified_export_limit_requires_its_safety_flag() -> None:
    """H6 fail-closed: the gate decides on status alone, so a VERIFIED EXPORT_LIMIT whose
    export_limit_zero_blocks_export is still None must be rejected at the dataclass boundary — it
    would otherwise pass the safety gate without the fact the status claims.
    """
    with pytest.raises(ValueError, match="export_limit_zero_blocks_export"):
        CapabilityRecord(
            "main",
            CapabilityName.EXPORT_LIMIT,
            status=CapabilityStatus.VERIFIED,
            export_limit_zero_blocks_export=None,
        )
    # The same record with the flag set is accepted.
    CapabilityRecord(
        "main",
        CapabilityName.EXPORT_LIMIT,
        status=CapabilityStatus.VERIFIED,
        export_limit_zero_blocks_export=True,
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("soc_strategy_external_code", 256),
        ("soc_strategy_external_code", -1),
        ("enum_byte_width", 0),
        ("enum_byte_width", 5),
        ("bool_byte_width", 5),
        ("refresh_interval_seconds", float("inf")),
        ("refresh_interval_seconds", -1.0),
        ("refresh_interval_seconds", 86_401.0),
    ],
)
def test_capability_value_ranges_are_enforced_in_the_domain(field: str, value) -> None:
    with pytest.raises(ValueError):
        CapabilityRecord("main", CapabilityName.WRITE_PATH, **{field: value})


def test_optional_bool_is_fail_closed_on_deserialization() -> None:
    """A stored "false" must not deserialize to True: a bool-looking string is corruption, which
    from_dict reports so the store treats the row as unreadable (reads as unverified).
    """
    data = CapabilityRecord("main", CapabilityName.EXPORT_LIMIT).to_dict()
    data["export_limit_zero_blocks_export"] = "false"
    with pytest.raises(TypeError):
        CapabilityRecord.from_dict(data)


# --- The port is the only writer (design 4.4) ------------------------------------------------


def dispatch_store(tmp_path: Path) -> DispatchStore:
    store = DispatchStore(tmp_path / "dispatch.db", "s" * 48)
    store.initialize()
    return store


async def test_set_capability_persists_before_the_registry_takes_the_record_over(tmp_path: Path) -> None:
    clock = ManualClock()
    store = dispatch_store(tmp_path)
    registry = CapabilityRegistry()
    dispatch = controller_with_store(
        store, clock, FakeDispatchGateway(clock), capabilities=registry
    )
    record = verified("main", CapabilityName.GRID_POWER_SIGN, grid_import_positive=False)
    await dispatch.set_capability("main", record)
    assert store.get_capabilities() == [record]
    assert registry.record("main", CapabilityName.GRID_POWER_SIGN) == record
    assert dispatch.capabilities("main") == registry.all("main")


async def test_set_capability_refuses_a_record_of_another_device(tmp_path: Path) -> None:
    clock = ManualClock()
    dispatch = controller_with_store(dispatch_store(tmp_path), clock, FakeDispatchGateway(clock))
    with pytest.raises(ValueError):
        await dispatch.set_capability("slave1", verified("main", CapabilityName.WRITE_PATH))


async def test_a_failed_commit_leaves_the_registry_untouched(tmp_path: Path) -> None:
    """Fail closed: if the verification is not durable, the gate must not act on it either."""
    clock = ManualClock()
    store = dispatch_store(tmp_path)

    def refuse(record: CapabilityRecord) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    store.put_capability = refuse  # type: ignore[method-assign]
    registry = CapabilityRegistry()
    dispatch = controller_with_store(
        store, clock, FakeDispatchGateway(clock), capabilities=registry
    )
    with pytest.raises(sqlite3.OperationalError):
        await dispatch.set_capability("main", verified("main", CapabilityName.WRITE_PATH))
    assert registry.record("main", CapabilityName.WRITE_PATH).status is CapabilityStatus.UNVERIFIED


async def test_the_capability_write_is_offloaded_from_the_event_loop(tmp_path: Path) -> None:
    """REQ-162: the store fsyncs, so the write must not run in the event loop."""
    clock = ManualClock()
    store = dispatch_store(tmp_path)
    dispatch = controller_with_store(store, clock, FakeDispatchGateway(clock))
    with patch("asyncio.to_thread", wraps=asyncio.to_thread) as offloaded:
        await dispatch.set_capability("main", verified("main", CapabilityName.WRITE_PATH))
        await dispatch.set_device_limits("main", DeviceLimits(1000, 2000, engineering_mode=True))
    offloaded_functions = {call.args[0] for call in offloaded.call_args_list}
    assert store.put_capability in offloaded_functions
    assert store.put_device_config in offloaded_functions


async def test_set_device_limits_persists_the_switch_and_the_limits(tmp_path: Path) -> None:
    clock = ManualClock()
    store = dispatch_store(tmp_path)
    dispatch = controller_with_store(store, clock, FakeDispatchGateway(clock))
    await dispatch.set_device_limits("main", DeviceLimits(1000, 2000, engineering_mode=True))
    assert store.get_device_configs() == {"main": DeviceLimits(1000, 2000, engineering_mode=True)}
    assert dispatch.device_limits("main") == DeviceLimits(1000, 2000, engineering_mode=True)
    assert dispatch.device_limits("slave1") is None


async def test_a_capability_write_on_one_device_does_not_block_another_device(tmp_path: Path) -> None:
    """D-01: the lock is per device, so verifying A cannot stall an operation on B."""
    clock = ManualClock()
    store = dispatch_store(tmp_path)
    gateway = FakeDispatchGateway(clock)
    started, release = threading.Event(), threading.Event()
    original = store.put_capability

    def blocking(record: CapabilityRecord) -> None:
        started.set()
        assert release.wait(5)
        original(record)

    store.put_capability = blocking  # type: ignore[method-assign]
    dispatch = controller_with_store(
        store,
        clock,
        gateway,
        capabilities=verified_registry("main", "slave1"),
        limits={"main": DeviceLimits(3000, 5000), "slave1": DeviceLimits(3000, 5000)},
    )
    pending = asyncio.create_task(
        dispatch.set_capability("main", verified("main", CapabilityName.WRITE_PATH))
    )
    await asyncio.to_thread(started.wait, 5)
    status = await dispatch.submit(
        "slave1",
        DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1)),
    )
    assert status.state is DispatchState.CHARGING
    assert not pending.done()
    release.set()
    await pending


# --- Admin API: entering a verification is the only way to lift the gate (4.6) ---------------


async def test_admin_api_rejects_verified_without_a_bearer_token(tmp_path: Path) -> None:
    from app.admin.store import AdminStore
    from tests.api_helpers import running_app

    admin_path = tmp_path / "admin.db"
    store = AdminStore(admin_path, "s" * 48)
    assert store.initialize() is not None
    store.close()
    # The password change is deliberately left pending here, so device jobs never leave "starting"
    # (app_factory._await_password_change); settle=False skips waiting on that gate.
    async with running_app(_admin_settings(tmp_path, admin_path), settle=False) as harness:
        resp = await harness.client.get("/admin/api/dispatch/devices")
        assert resp.status_code == 401


async def test_admin_api_verified_capability_lifts_the_gate_for_exactly_that_device(tmp_path: Path) -> None:
    async with _admin_harness(tmp_path) as (harness, headers):
        put = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/battery_power_sign_convention",
            headers=headers,
            json={"status": "verified", "verified_device_model": "RCT-Power-Storage-DC", "verified_firmware": "1.0.0", "battery_discharge_positive": True},
        )
        assert put.status_code == 200
        assert put.json()["status"] == "verified"
        assert put.json()["verified_by"] == "admin"

        unaffected = await harness.client.get(
            "/admin/api/dispatch/devices/slave1/capabilities", headers=headers
        )
        names = {row["name"]: row["status"] for row in unaffected.json()}
        assert names["battery_power_sign_convention"] == "unverified"


async def test_admin_api_capabilities_get_round_trips_evidence_fields(tmp_path: Path) -> None:
    """Round-trip plumbing only: the strategy code is a neutral fixture value, not a claim.

    The code and the note say nothing about hardware; `power_mng_soc_strategy` has no resolved enum
    in this project, so no value may appear here as if it were verified.
    """
    async with _admin_harness(tmp_path) as (harness, headers):
        body = _verified_write_path_body()
        put = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/write_path_convention",
            headers=headers,
            json=body,
        )
        assert put.status_code == 200

        get = await harness.client.get("/admin/api/dispatch/devices/main/capabilities", headers=headers)
        assert get.status_code == 200
        row = next(r for r in get.json() if r["name"] == "write_path_convention")
        assert row["soc_strategy_external_code"] == body["soc_strategy_external_code"]
        assert row["note"] == body["note"]
        # Fields this capability does not govern are null, not an echoed inert value.
        assert row["battery_discharge_positive"] is None
        assert row["grid_import_positive"] is None
        # PUT's own response is the same shape as GET's rows (round-trip consistency).
        assert put.json()["soc_strategy_external_code"] == body["soc_strategy_external_code"]
        assert put.json()["note"] == body["note"]
        assert put.json()["battery_discharge_positive"] is None


async def test_admin_api_reports_each_evidence_field_only_on_the_capability_it_governs(
    tmp_path: Path,
) -> None:
    """The adapter reads each evidence field from exactly one capability; the API says so too."""
    async with _admin_harness(tmp_path) as (harness, headers):
        put = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/battery_power_sign_convention",
            headers=headers,
            json={
                "status": "verified",
                "verified_device_model": "RCT-Power-Storage-DC",
                "verified_firmware": "1.0.0",
                "battery_discharge_positive": False,
            },
        )
        assert put.status_code == 200

        get = await harness.client.get("/admin/api/dispatch/devices/main/capabilities", headers=headers)
        rows = {row["name"]: row for row in get.json()}

        battery = rows["battery_power_sign_convention"]
        assert battery["battery_discharge_positive"] is False
        assert battery["grid_import_positive"] is None
        assert battery["soc_strategy_external_code"] is None

        grid = rows["grid_power_sign_convention"]
        assert grid["grid_import_positive"] is True  # assumption default, still unverified
        assert grid["battery_discharge_positive"] is None
        assert grid["soc_strategy_external_code"] is None


@pytest.mark.parametrize(
    ("name", "field_name"),
    [
        ("write_path_convention", "battery_discharge_positive"),
        ("battery_power_sign_convention", "soc_strategy_external_code"),
        ("grid_power_sign_convention", "battery_discharge_positive"),
    ],
)
async def test_admin_api_refuses_evidence_set_on_a_capability_it_does_not_govern(
    tmp_path: Path, name: str, field_name: str
) -> None:
    """Silently storing an inert value would show an operator a setting the adapter ignores."""
    async with _admin_harness(tmp_path) as (harness, headers):
        resp = await harness.client.put(
            f"/admin/api/dispatch/devices/main/capabilities/{name}",
            headers=headers,
            json={"status": "unverified", field_name: 1 if "code" in field_name else False},
        )
        assert resp.status_code == 422
        assert field_name in resp.json()["detail"]
        assert name in resp.json()["detail"]


async def test_admin_api_put_replaces_the_record_so_an_omitted_evidence_field_resets(
    tmp_path: Path,
) -> None:
    """PUT is a replace, not a merge: an omitted evidence field falls back to its default.

    Pinned deliberately because it is a footgun on a sign convention — a follow-up PUT that only
    means to change the note silently flips `battery_discharge_positive` back to the assumption
    default unless the caller resends it. Documented in docs/operation.md.
    """
    async with _admin_harness(tmp_path) as (harness, headers):
        route = "/admin/api/dispatch/devices/main/capabilities/battery_power_sign_convention"
        verified = {
            "status": "verified",
            "verified_device_model": "RCT-Power-Storage-DC",
            "verified_firmware": "1.0.0",
            "battery_discharge_positive": False,
        }
        first = await harness.client.put(route, headers=headers, json=verified)
        assert first.status_code == 200
        assert first.json()["battery_discharge_positive"] is False

        # Same request minus the evidence field (and not VERIFIED, which now demands it): the stored
        # False is not preserved.
        resent = await harness.client.put(route, headers=headers, json={"status": "unverified"})
        assert resent.status_code == 200
        assert resent.json()["battery_discharge_positive"] is True


async def test_admin_api_refuses_verified_sign_convention_without_explicit_flag(tmp_path: Path) -> None:
    """The flag defaults to True, so an omitted value is no evidence."""
    async with _admin_harness(tmp_path) as (harness, headers):
        resp = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/grid_power_sign_convention",
            headers=headers,
            json={"status": "verified", "verified_device_model": "RCT-Power-Storage-DC", "verified_firmware": "1.0.0"},
        )
        assert resp.status_code == 400
        assert "grid_import_positive" in resp.json()["detail"]


async def test_admin_api_refuses_verified_write_path_without_explicit_soc_target_unit(tmp_path: Path) -> None:
    """V-17: soc_target_unit has a non-None default ("ratio"), so like the sign flags only an
    explicitly sent value counts as evidence that the register representation was attested.
    """
    async with _admin_harness(tmp_path) as (harness, headers):
        body = _verified_write_path_body()
        del body["soc_target_unit"]
        resp = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/write_path_convention",
            headers=headers,
            json=body,
        )
        assert resp.status_code == 400
        assert "soc_target_unit" in resp.json()["detail"]


@pytest.mark.parametrize("note", [None, "", "   "])
async def test_admin_api_requires_a_non_empty_note_for_a_verified_write_path(
    tmp_path: Path, note: str | None
) -> None:
    """The raw strategy code is uninterpretable without the evidence behind it, so it is required."""
    async with _admin_harness(tmp_path) as (harness, headers):
        body = _verified_write_path_body()
        if note is None:
            del body["note"]
        else:
            body["note"] = note
        refused = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/write_path_convention",
            headers=headers,
            json=body,
        )
        assert refused.status_code == 400
        assert "note" in refused.json()["detail"]

        accepted = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/write_path_convention",
            headers=headers,
            json=_verified_write_path_body(),
        )
        assert accepted.status_code == 200


async def test_admin_api_rejects_verified_write_path_with_missing_evidence(tmp_path: Path) -> None:
    async with _admin_harness(tmp_path) as (harness, headers):
        resp = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/write_path_convention",
            headers=headers,
            json={"status": "verified", "verified_device_model": "RCT-Power-Storage-DC", "verified_firmware": "1.0.0"},
        )
        assert resp.status_code == 400
        assert "soc_strategy_external_code" in resp.json()["detail"]


async def test_admin_api_ignores_a_caller_supplied_verified_at_and_verified_by(tmp_path: Path) -> None:
    async with _admin_harness(tmp_path) as (harness, headers):
        resp = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/grid_power_sign_convention",
            headers=headers,
            json={
                "status": "verified",
                "verified_device_model": "RCT-Power-Storage-DC",
                "verified_firmware": "1.0.0",
                "grid_import_positive": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["verified_by"] == "admin"


async def test_admin_api_copy_from_requires_matching_model_and_firmware(tmp_path: Path) -> None:
    async with _admin_harness(tmp_path) as (harness, headers):
        await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/grid_power_sign_convention",
            headers=headers,
            json={"status": "verified", "verified_device_model": "RCT-Power-Storage-DC", "verified_firmware": "1.0.0", "grid_import_positive": True},
        )
        await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/battery_power_sign_convention",
            headers=headers,
            json={"status": "verified", "verified_device_model": "RCT-Power-Storage-DC", "verified_firmware": "2.0.0", "battery_discharge_positive": True},
        )
        # Matching model and firmware: the one capability recorded under that firmware is copied.
        copied = await harness.client.post(
            "/admin/api/dispatch/devices/slave1/capabilities:copy-from",
            headers=headers,
            json={
                "source_device_id": "main",
                "target_device_model": "RCT-Power-Storage-DC",
                "target_firmware": "1.0.0",
            },
        )
        assert copied.status_code == 200
        assert {row["name"] for row in copied.json()} == {"grid_power_sign_convention"}

        # A firmware that matches no verified source record copies nothing.
        refused = await harness.client.post(
            "/admin/api/dispatch/devices/slave1/capabilities:copy-from",
            headers=headers,
            json={
                "source_device_id": "main",
                "target_device_model": "RCT-Power-Storage-DC",
                "target_firmware": "9.9.9",
            },
        )
        assert refused.status_code == 200
        assert {row["name"] for row in refused.json()} == set()

        # Copying a device onto itself is a client error, not a no-op rewrite of its own evidence.
        itself = await harness.client.post(
            "/admin/api/dispatch/devices/main/capabilities:copy-from",
            headers=headers,
            json={
                "source_device_id": "main",
                "target_device_model": "RCT-Power-Storage-DC",
                "target_firmware": "1.0.0",
            },
        )
        assert itself.status_code == 400


async def test_admin_api_withdraws_an_idle_capability_without_force(tmp_path: Path) -> None:
    async with _admin_harness(tmp_path) as (harness, headers):
        entered = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/write_path_convention",
            headers=headers,
            json=_verified_write_path_body(),
        )
        assert entered.status_code == 200
        withdrawn = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/write_path_convention",
            headers=headers,
            json={"status": "unverified"},
        )
        assert withdrawn.status_code == 200
        assert withdrawn.json()["status"] == "unverified"


async def test_admin_api_rejects_an_unknown_device(tmp_path: Path) -> None:
    async with _admin_harness(tmp_path) as (harness, headers):
        resp = await harness.client.get("/admin/api/dispatch/devices/not-configured/capabilities", headers=headers)
        assert resp.status_code == 404


# --- B2: VERIFIED->VERIFIED (and any other status) bypass is closed -----------------------------


async def test_admin_api_refuses_a_verified_write_while_the_matching_mode_is_active(tmp_path: Path) -> None:
    """Before the fix: setting status=verified never checked for an active operation at all, so
    this write would succeed even though a matching charge_from_grid operation is running.
    """
    async with _admin_harness(tmp_path) as (harness, headers):
        for name in CHARGE_CAPABILITIES:
            await harness.client.put(
                f"/admin/api/dispatch/devices/main/capabilities/{name.value}",
                headers=headers,
                json=_verified_write_path_body() if name is CapabilityName.WRITE_PATH else {
                    "status": "verified",
                    "verified_device_model": "RCT-Power-Storage-DC",
                    "verified_firmware": "1.0.0",
                },
            )
        now = harness.runtime.clock.now()
        intent = DispatchIntent(
            operation_id="active-op",
            mode=DispatchMode.CHARGE_FROM_GRID,
            target_soc_percent=80.0,
            max_power_w=2000.0,
            max_power_w_requested=2000.0,
            valid_until=now + timedelta(hours=1),
            valid_until_requested=now + timedelta(hours=1),
            created_at=now,
        )
        record = DispatchRecord(
            "main",
            state=DispatchState.CHARGING,
            intent=intent,
            snapshot=DeviceControlSnapshot(PowerSetpoint(), 0.5, 1, False, now, all_fresh=True),
            restore_required=True,
        )
        await asyncio.to_thread(harness.runtime.dispatch._store.put, record)
        harness.runtime.dispatch._locks.clear()  # a fresh controller process would re-create them via recover()

        resp = await harness.client.put(
            "/admin/api/dispatch/devices/main/capabilities/battery_power_sign_convention",
            headers=headers,
            json={
                "status": "verified",
                "verified_device_model": "RCT-Power-Storage-DC",
                "verified_firmware": "1.0.0",
                "battery_discharge_positive": False,
            },
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["operation_id"] == "active-op"


def _verified_write_path_body() -> dict:
    return {
        "status": "verified",
        "verified_device_model": "RCT-Power-Storage-DC",
        "verified_firmware": "1.0.0",
        "soc_strategy_external_code": 1,
        "enum_byte_width": 1,
        "bool_byte_width": 1,
        "write_frame_layout_verified": True,
        "apply_sequence_verified": True,
        "soc_target_unit": "ratio",
        # A non-empty note is required for a verified write_path_convention. Fixture text only: it
        # claims no hardware verification of the strategy code.
        "note": "fixture value; no hardware verification claimed",
    }


async def test_admin_api_copy_from_aborts_the_whole_copy_on_an_active_operation_conflict(
    tmp_path: Path,
) -> None:
    async with _admin_harness(tmp_path) as (harness, headers):
        for name in CHARGE_CAPABILITIES:
            await harness.client.put(
                f"/admin/api/dispatch/devices/main/capabilities/{name.value}",
                headers=headers,
                json=_verified_write_path_body() if name is CapabilityName.WRITE_PATH else {
                    "status": "verified",
                    "verified_device_model": "RCT-Power-Storage-DC",
                    "verified_firmware": "1.0.0",
                },
            )
        now = harness.runtime.clock.now()
        intent = DispatchIntent(
            operation_id="active-op",
            mode=DispatchMode.CHARGE_FROM_GRID,
            target_soc_percent=80.0,
            max_power_w=2000.0,
            max_power_w_requested=2000.0,
            valid_until=now + timedelta(hours=1),
            valid_until_requested=now + timedelta(hours=1),
            created_at=now,
        )
        record = DispatchRecord(
            "slave1",
            state=DispatchState.CHARGING,
            intent=intent,
            snapshot=DeviceControlSnapshot(PowerSetpoint(), 0.5, 1, False, now, all_fresh=True),
            restore_required=True,
        )
        await asyncio.to_thread(harness.runtime.dispatch._store.put, record)
        harness.runtime.dispatch._locks.clear()

        resp = await harness.client.post(
            "/admin/api/dispatch/devices/slave1/capabilities:copy-from",
            headers=headers,
            json={
                "source_device_id": "main",
                "target_device_model": "RCT-Power-Storage-DC",
                "target_firmware": "1.0.0",
            },
        )
        assert resp.status_code == 409
        assert all(
            record.status is CapabilityStatus.UNVERIFIED for record in harness.runtime.dispatch.capabilities("slave1")
        )


# --- B1: the capability-write/submit() TOCTOU window is closed by the shared per-device lock ----


async def test_set_capability_and_submit_of_the_same_device_never_interleave(tmp_path: Path) -> None:
    """Drives the interleaving deterministically: a concurrent submit() must either fully commit
    before the capability write's conflict check runs, or be blocked by it — never land in the gap
    between the former admin-layer check and the controller-side write (B1's TOCTOU).
    """
    clock = ManualClock()
    store = dispatch_store(tmp_path)
    gateway = FakeDispatchGateway(clock)
    started, release = threading.Event(), threading.Event()
    original = store.put_capability

    def blocking(record: CapabilityRecord) -> None:
        started.set()
        assert release.wait(5)
        original(record)

    store.put_capability = blocking  # type: ignore[method-assign]
    dispatch = controller_with_store(
        store,
        clock,
        gateway,
        capabilities=verified_registry("main"),
        limits={"main": DeviceLimits(3000, 5000)},
    )
    pending = asyncio.create_task(
        dispatch.set_capability("main", verified("main", CapabilityName.BATTERY_POWER_SIGN))
    )
    await asyncio.to_thread(started.wait, 5)
    # submit() must block on the same per-device lock the capability write already holds, not run
    # concurrently with it: the capability write started first, so submit() cannot complete yet.
    submit_task = asyncio.create_task(
        dispatch.submit("main", DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1)))
    )
    await asyncio.sleep(0)
    assert not submit_task.done()
    release.set()
    await pending
    status = await submit_task
    assert status.state is DispatchState.CHARGING


async def test_admin_api_sets_device_limits_and_the_engineering_switch(tmp_path: Path) -> None:
    async with _admin_harness(tmp_path) as (harness, headers):
        resp = await harness.client.put(
            "/admin/api/dispatch/devices/main",
            headers=headers,
            json={"max_charge_power_w": 1500, "max_discharge_power_w": 2500, "engineering_mode": True},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "device_id": "main",
            "max_charge_power_w": 1500.0,
            "max_discharge_power_w": 2500.0,
            "engineering_mode": True,
        }
        assert harness.runtime.dispatch.device_limits("main").engineering_mode is True


async def test_admin_api_rejects_non_finite_or_absurd_device_limits(tmp_path: Path) -> None:
    async with _admin_harness(tmp_path) as (harness, headers):
        for charge in ("Infinity", "NaN", "1e12"):
            resp = await harness.client.put(
                "/admin/api/dispatch/devices/main",
                headers={**headers, "Content-Type": "application/json"},
                content=f'{{"max_charge_power_w": {charge}, "max_discharge_power_w": 2500}}',
            )
            assert resp.status_code == 422, (charge, resp.text)


async def test_capability_get_returns_the_evidence_a_put_accepts(tmp_path: Path) -> None:
    body = {
        "status": "unverified", "soc_strategy_external_code": 2, "enum_byte_width": 1, "bool_byte_width": 2,
        "write_frame_layout_verified": True, "apply_sequence_verified": True, "sequence_order_relevant": True,
        "soc_target_unit": "percent", "note": "bench evidence",
    }
    url = "/admin/api/dispatch/devices/main/capabilities"
    async with _admin_harness(tmp_path) as (harness, headers):
        assert (await harness.client.put(f"{url}/write_path_convention", headers=headers, json=body)).status_code == 200
        listed = {row["name"]: row for row in (await harness.client.get(url, headers=headers)).json()}
        row = listed["write_path_convention"]
        assert {key: row[key] for key in body} == body
        # GET -> edit -> PUT: server-managed fields are dropped, nulls of ungoverned fields are tolerated.
        edited = {k: v for k, v in row.items() if k not in {"device_id", "name", "verified_at", "verified_by"}}
        edited["bool_byte_width"] = 4
        edited["battery_discharge_positive"] = None
        resp = await harness.client.put(f"{url}/write_path_convention", headers=headers, json=edited)
        assert resp.status_code == 200, resp.text
        assert resp.json()["bool_byte_width"] == 4


async def test_capability_optional_fields_belong_to_their_own_capability(tmp_path: Path) -> None:
    url = "/admin/api/dispatch/devices/main/capabilities"
    async with _admin_harness(tmp_path) as (harness, headers):
        wrong = await harness.client.put(
            f"{url}/write_path_convention", headers=headers, json={"status": "unverified", "volatile": True}
        )
        assert wrong.status_code == 422
        right = await harness.client.put(
            f"{url}/setpoint_volatility", headers=headers,
            json={"status": "unverified", "volatile": True, "refresh_interval_seconds": 30},
        )
        assert right.status_code == 200, right.text
        assert (right.json()["volatile"], right.json()["refresh_interval_seconds"]) == (True, 30.0)


def _admin_settings(tmp_path: Path, admin_path: Path, **overrides):
    from tests.api_helpers import dispatch_fixtures, make_settings

    paths = dispatch_fixtures(tmp_path)
    return make_settings(
        enable_write_support=True,
        hmac_secret="s" * 48,
        admin_db_path=admin_path,
        dispatch_db_path=tmp_path / "dispatch.db",
        dispatch_max_charge_power_w=3000,
        dispatch_max_discharge_power_w=5000,
        write_response_timeout_ms=50,
        **paths,
        **overrides,
    )


@contextlib.asynccontextmanager
async def _admin_harness(tmp_path: Path):
    """A running app logged in as the admin, for the admin dispatch API's own tests.

    The dispatch admin API is session-only (SEC-01), so a PAT would be refused with 403.
    """
    from app.admin.store import AdminStore
    from tests.api_helpers import admin_session_headers, running_app

    admin_path = tmp_path / "admin.db"
    store = AdminStore(admin_path, "s" * 48)
    password = store.initialize()
    assert store.change_password(password, "replacement-test-password")
    store.close()
    async with running_app(_admin_settings(tmp_path, admin_path)) as harness:
        headers = await admin_session_headers(harness.client, "replacement-test-password")
        yield harness, headers


# --- The gate is armed in submit(): unverified hardware is never touched (AK-18) --------------


def _charge(clock: ManualClock) -> DispatchCommand:
    return DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1))


async def test_unverified_hardware_is_refused_without_touching_the_device(tmp_path: Path) -> None:
    """The decisive invariant: a blocked dispatch issues no read and no write at all."""
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    # Strategy code present but nothing verified, and engineering mode off: the adapter's own lock
    # would let this through, so only the gate can refuse it.
    registry = CapabilityRegistry(
        [CapabilityRecord("main", CapabilityName.WRITE_PATH, soc_strategy_external_code=1)]
    )
    dispatch = controller_with_store(
        dispatch_store(tmp_path), clock, gateway, capabilities=registry,
        limits={"main": DeviceLimits(3000, 5000, engineering_mode=False)},
    )  # fmt: skip
    with pytest.raises(DeviceApiError) as raised:
        await dispatch.submit("main", _charge(clock))
    assert raised.value.code == "dispatch_unverified"
    assert gateway.calls == []  # not one frame, not even a SoC read
    assert (await dispatch.status("main")).state is DispatchState.IDLE


async def test_a_partially_verified_device_is_still_refused(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller_with_store(
        dispatch_store(tmp_path), clock, gateway,
        capabilities=registry_for("main", CapabilityName.WRITE_PATH),  # battery sign still missing
        limits={"main": DeviceLimits(3000, 5000)},
    )  # fmt: skip
    with pytest.raises(DeviceApiError) as raised:
        await dispatch.submit("main", _charge(clock))
    assert raised.value.code == "dispatch_unverified"
    assert raised.value.context["unverified"] == [CapabilityName.BATTERY_POWER_SIGN.value]
    assert gateway.calls == []


async def test_verifying_one_device_does_not_release_the_other_through_submit(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller_with_store(
        dispatch_store(tmp_path), clock, gateway,
        capabilities=registry_for("main", *CHARGE_CAPABILITIES),
        limits={"main": DeviceLimits(3000, 5000), "slave1": DeviceLimits(3000, 5000)},
    )  # fmt: skip
    assert (await dispatch.submit("main", _charge(clock))).state is DispatchState.CHARGING
    with pytest.raises(DeviceApiError) as raised:
        await dispatch.submit("slave1", _charge(clock))
    assert raised.value.code == "dispatch_unverified"


async def test_engineering_mode_without_a_strategy_code_is_refused_at_submit(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller_with_store(
        dispatch_store(tmp_path), clock, gateway, capabilities=CapabilityRegistry(),
        limits={"main": DeviceLimits(3000, 5000, engineering_mode=True)},
    )  # fmt: skip
    with pytest.raises(DeviceApiError) as raised:
        await dispatch.submit("main", _charge(clock))
    assert raised.value.code == "dispatch_unverified"
    assert raised.value.context["reason"] == "soc_strategy_external_code required for engineering mode"
    assert gateway.calls == []


async def test_engineering_mode_drives_unverified_hardware_under_the_shorter_ttl(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    registry = CapabilityRegistry(
        [CapabilityRecord("main", CapabilityName.WRITE_PATH, soc_strategy_external_code=1)]
    )
    dispatch = controller_with_store(
        dispatch_store(tmp_path), clock, gateway, capabilities=registry,
        limits={"main": DeviceLimits(3000, 5000, engineering_mode=True)},
        config=DispatchConfig(
            max_operation_duration_seconds=21600.0, max_operation_duration_engineering_seconds=1800.0
        ),
    )  # fmt: skip
    requested = clock.now() + timedelta(hours=4)
    status = await dispatch.submit(
        "main", DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, requested)
    )
    assert status.state is DispatchState.CHARGING
    # Capped to 1800 s instead of the requested 4 h, because the release came through the switch.
    assert status.valid_until == clock.now() + timedelta(seconds=1800)


async def test_a_verified_device_keeps_the_long_ttl_even_with_the_switch_on(tmp_path: Path) -> None:
    """A forgotten engineering switch must not devalue a real verification."""
    clock = ManualClock()
    dispatch = controller_with_store(
        dispatch_store(tmp_path), clock, FakeDispatchGateway(clock),
        capabilities=registry_for("main", *CHARGE_CAPABILITIES),
        limits={"main": DeviceLimits(3000, 5000, engineering_mode=True)},
        config=DispatchConfig(
            max_operation_duration_seconds=21600.0, max_operation_duration_engineering_seconds=1800.0
        ),
    )  # fmt: skip
    requested = clock.now() + timedelta(hours=4)
    status = await dispatch.submit(
        "main", DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, requested)
    )
    assert status.valid_until == requested  # below the 6 h cap, so passed through unchanged
