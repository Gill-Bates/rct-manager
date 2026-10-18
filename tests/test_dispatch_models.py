#!/usr/bin/env python3
#
# tests/test_dispatch_models.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Model-layer safety invariants (fail-closed persistence, finite values, state consistency) and RCT sign conventions."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.dispatch.capabilities import CapabilityRegistry
from app.dispatch.controller import DispatchController
from app.dispatch.models import (
    ControlTelemetry,
    DeviceControlSnapshot,
    DeviceLimits,
    DispatchConfig,
    DispatchMode,
    DispatchPhase,
    DispatchRecord,
    DispatchRecordCorrupt,
    DispatchState,
    PowerDirection,
    PowerSetpoint,
    phase_for,
)
from app.gateway.base import MetricReading
from app.gateway.conventions import RctBatteryPowerConvention, RctGridPowerConvention
from app.gateway.rct_dispatch import RctDispatchGateway
from tests.conftest import ManualClock

AWARE_NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_sequential_control_reads_age_at_assembly_time() -> None:
    clock = ManualClock()

    class Gateway:
        _clock = clock

        async def read_system(self, _device_id, name):
            reading = MetricReading(name, 0.5 if name == "battery_soc" else 100.0,
                                    "ratio" if name == "battery_soc" else "W",
                                    clock.now(), 0.0, "device", False)
            clock.advance(6.0)
            return reading

    dispatch = RctDispatchGateway(Gateway(), capabilities=CapabilityRegistry(),
                                  soc_target_policies=None)  # type: ignore[arg-type]
    telemetry = await dispatch.read_control_telemetry("main")
    assert telemetry.soc_age_seconds >= 18.0
    controller = DispatchController.__new__(DispatchController)
    controller._config = SimpleNamespace(soc_telemetry_max_age_seconds=15.0,
                                         control_telemetry_max_age_seconds=5.0)
    assert controller._telemetry_stale(telemetry)


def _snapshot_dict(**overrides) -> dict:
    data = {
        "battery_setpoint": {"direction": "none", "watts": 0.0},
        "soc_target_ratio": 0.5,
        "soc_strategy_code": 1,
        "grid_charge_enabled": False,
        "read_at": AWARE_NOW.isoformat(),
    }
    data.update(overrides)
    return data


def _intent_dict(**overrides) -> dict:
    data = {
        "operation_id": "op-1",
        "mode": "charge_from_grid",
        "target_soc_percent": 80.0,
        "max_power_w": 2000.0,
        "max_power_w_requested": 2000.0,
        "valid_until": AWARE_NOW.isoformat(),
        "valid_until_requested": AWARE_NOW.isoformat(),
        "created_at": AWARE_NOW.isoformat(),
    }
    data.update(overrides)
    return data


def test_from_dict_on_a_bare_device_id_raises_instead_of_returning_a_clean_idle_record() -> None:
    """The exact review example: {"device_id": "battery-1"} must not silently become IDLE."""
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict({"device_id": "battery-1"})


def test_from_dict_requires_restore_required_to_be_present_and_a_strict_bool() -> None:
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict({"device_id": "main", "state": "idle"})
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict({"device_id": "main", "state": "idle", "restore_required": "false"})


def test_from_dict_on_a_complete_idle_record_still_succeeds() -> None:
    record = DispatchRecord.from_dict({"device_id": "main", "state": "idle", "restore_required": False})
    assert record.state is DispatchState.IDLE
    assert record.intent is None


def test_power_setpoint_rejects_nan_and_inf() -> None:
    with pytest.raises(ValueError):
        PowerSetpoint(PowerDirection.CHARGE, float("nan"))
    with pytest.raises(ValueError):
        PowerSetpoint(PowerDirection.CHARGE, float("inf"))


def test_control_telemetry_rejects_nan_age_fields() -> None:
    with pytest.raises(ValueError):
        ControlTelemetry(
            soc_percent=50.0,
            soc_age_seconds=0.0,
            soc_source="device",
            grid_import_w=0.0,
            grid_age_seconds=float("nan"),
            grid_source="device",
            battery_setpoint=PowerSetpoint(),
            battery_age_seconds=0.0,
            battery_source="device",
        )


def test_control_telemetry_rejects_nan_household_fields_when_present() -> None:
    with pytest.raises(ValueError):
        ControlTelemetry(
            soc_percent=50.0,
            soc_age_seconds=0.0,
            soc_source="device",
            grid_import_w=0.0,
            grid_age_seconds=0.0,
            grid_source="device",
            battery_setpoint=PowerSetpoint(),
            battery_age_seconds=0.0,
            battery_source="device",
            household_load_w=float("nan"),
        )


@pytest.mark.parametrize("soc", [-0.1, 100.1, 8000.0])
def test_control_telemetry_rejects_an_out_of_range_soc(soc: float) -> None:
    """SoC is a percentage by contract; a value outside 0..100 is a unit/decoding fault and must
    not reach a discharge guard as if it were a real charge level (the ratio/percent finding).
    """
    with pytest.raises(ValueError, match="soc_percent"):
        ControlTelemetry(
            soc_percent=soc,
            soc_age_seconds=0.0,
            soc_source="device",
            grid_import_w=0.0,
            grid_age_seconds=0.0,
            grid_source="device",
            battery_setpoint=PowerSetpoint(),
            battery_age_seconds=0.0,
            battery_source="device",
        )


def test_from_dict_rejects_a_non_finite_persisted_power_setpoint() -> None:
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict(
            {
                "device_id": "main",
                "state": "idle",
                "restore_required": False,
                "last_commanded": {"direction": "charge", "watts": float("nan")},
            }
        )


def test_from_dict_rejects_a_naive_last_write_at() -> None:
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict(
            {
                "device_id": "main",
                "state": "idle",
                "restore_required": False,
                "last_write_at": "2026-01-01T00:00:00",
            }
        )


def test_from_dict_rejects_a_naive_intent_valid_until() -> None:
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict(
            {
                "device_id": "main",
                "state": "charging",
                "restore_required": False,
                "intent": _intent_dict(valid_until="2026-01-01T00:00:00"),
            }
        )


def test_snapshot_all_fresh_defaults_to_false_when_absent_from_the_persisted_dict() -> None:
    data = _snapshot_dict()
    assert "all_fresh" not in data
    record = DispatchRecord.from_dict(
        {
            "device_id": "main",
            "state": "charging",
            "restore_required": True,
            "intent": _intent_dict(),
            "snapshot": data,
        }
    )
    assert record.snapshot is not None
    assert record.snapshot.all_fresh is False


def test_from_dict_rejects_idle_with_a_present_intent() -> None:
    """The review's headline scenario: IDLE + intent != None + restore_required == False."""
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict(
            {
                "device_id": "x",
                "state": "idle",
                "intent": _intent_dict(),
                "restore_required": False,
            }
        )


def test_from_dict_rejects_charging_with_no_intent() -> None:
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict({"device_id": "x", "state": "charging", "restore_required": False})


def test_from_dict_rejects_fault_restore_pending_without_restore_required() -> None:
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict(
            {
                "device_id": "x",
                "state": "fault_restore_pending",
                "intent": _intent_dict(),
                "restore_required": False,
            }
        )


def test_precheck_with_no_intent_is_a_valid_crash_window_not_corruption() -> None:
    """recover() constructs exactly this combination for a crash before the first intent write."""
    record = DispatchRecord.from_dict({"device_id": "x", "state": "precheck", "restore_required": False})
    assert record.intent is None


def test_device_limits_rejects_non_positive_power() -> None:
    with pytest.raises(ValueError):
        DeviceLimits(-100, 500)
    with pytest.raises(ValueError):
        DeviceLimits(100, 0)


def test_dispatch_config_rejects_inverted_soc_bounds() -> None:
    with pytest.raises(ValueError):
        DispatchConfig(min_soc=90, max_soc=10)


def test_dispatch_config_rejects_engineering_ttl_above_the_normal_cap() -> None:
    with pytest.raises(ValueError):
        DispatchConfig(max_operation_duration_seconds=1000, max_operation_duration_engineering_seconds=2000)


def test_dispatch_config_rejects_a_non_positive_operation_duration_cap() -> None:
    """A zero/negative TTL cap makes submit() derive an already-expired deadline yet still write
    hardware before the next tick() expires it; the domain must reject it, not only the config layer.
    """
    with pytest.raises(ValueError, match="max_operation_duration_seconds"):
        DispatchConfig(max_operation_duration_seconds=0)
    with pytest.raises(ValueError, match="max_operation_duration_seconds"):
        DispatchConfig(max_operation_duration_seconds=-1)


@pytest.mark.parametrize(
    "field",
    [
        "grid_import_reserve_w", "grid_control_deadband_w", "power_write_deadband_w",
        "min_write_interval_seconds", "cycle_interval_seconds", "telemetry_timeout_seconds",
        "control_telemetry_max_age_seconds", "soc_telemetry_max_age_seconds",
    ],
)
def test_dispatch_config_rejects_a_negative_duration_or_power(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        DispatchConfig(**{field: -1.0})


def test_dispatch_config_rejects_a_zero_cycle_interval() -> None:
    """P3-01: cycle_interval_seconds=0 would turn run()'s `while True: sleep(interval)` into an
    unthrottled tick/telemetry loop; the domain must reject it (Settings already pins the operator
    value to >= 1, so this is the defense-in-depth guard for a directly constructed config)."""
    with pytest.raises(ValueError, match="cycle_interval_seconds must be positive"):
        DispatchConfig(cycle_interval_seconds=0)
    with pytest.raises(ValueError, match="cycle_interval_seconds must be positive"):
        DispatchConfig(cycle_interval_seconds=-1.0)


def test_from_dict_rejects_a_truthy_string_instead_of_a_real_bool() -> None:
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict({"device_id": "x", "state": "idle", "restore_required": "yes"})


def test_device_control_snapshot_rejects_non_finite_soc_target_ratio() -> None:
    with pytest.raises(ValueError):
        DeviceControlSnapshot(PowerSetpoint(), float("nan"), 1, False, AWARE_NOW)


def test_a_holding_record_round_trips_and_reports_the_controlling_phase() -> None:
    """HOLDING must reach DispatchPhase.CONTROLLING: without the mapping phase_for() would raise,
    and control_state must say `controlled` — the battery is held under external control.
    """
    data = {
        "device_id": "main",
        "state": "holding",
        "restore_required": False,
        "intent": _intent_dict(mode="hold", target_soc_percent=None, max_power_w=0.0),
        # HOLDING is a hardware-touching state, so a persisted record must carry the snapshot the
        # restore path reads back (the P2-04 snapshot invariant).
        "snapshot": _snapshot_dict(),
    }
    record = DispatchRecord.from_dict(data)
    assert record.state is DispatchState.HOLDING
    assert record.intent is not None
    assert record.intent.mode is DispatchMode.HOLD
    assert record.intent.target_soc_percent is None
    assert phase_for(record.state) is DispatchPhase.CONTROLLING
    assert DispatchRecord.from_dict(record.to_dict()).to_dict() == record.to_dict()


def test_a_persisted_intent_without_a_target_soc_percent_key_loads_as_none() -> None:
    """A mode without a SoC goal carries no target; the key may also be absent entirely."""
    data = _intent_dict()
    del data["target_soc_percent"]
    record = DispatchRecord.from_dict(
        {
            "device_id": "main",
            "state": "holding",
            "restore_required": False,
            "intent": data,
            "snapshot": _snapshot_dict(),  # HOLDING requires a snapshot (P2-04)
        }
    )
    assert record.intent is not None and record.intent.target_soc_percent is None


def test_a_non_numeric_target_soc_percent_is_still_corruption_not_none() -> None:
    """Optional must not mean lenient: a string target is a malformed record, never a silent None."""
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict(
            {
                "device_id": "main",
                "state": "charging",
                "restore_required": False,
                "intent": _intent_dict(target_soc_percent="80"),
            }
        )


@pytest.mark.parametrize(
    "state",
    ["applying", "replacing", "charging", "discharging", "holding", "target_reached", "restoring", "fault_restore_pending"],
)
def test_an_active_record_without_a_snapshot_is_corrupt(state: str) -> None:
    """P2-04: a state in which the state machine may already have written hardware must carry a
    snapshot. Without it, _restore_guarded() would treat snapshot=None as "nothing written" and
    reset to IDLE without touching the device — a fail-open pretend restore. from_dict() rejects it
    as DispatchRecordCorrupt instead."""
    restore_required = state in ("restoring", "fault_restore_pending")
    with pytest.raises(DispatchRecordCorrupt, match="snapshot is missing"):
        DispatchRecord.from_dict(
            {
                "device_id": "main",
                "state": state,
                "restore_required": restore_required,
                "intent": _intent_dict(),
            }
        )


def test_precheck_without_a_snapshot_is_the_legitimate_crash_window() -> None:
    """P2-04 exemption: PRECHECK+snapshot=None is the narrow crash window after the durable-intent
    write but before read_snapshot(); nothing was written, so it stays valid (recover() resets it
    to IDLE). The invariant must not reject it."""
    record = DispatchRecord.from_dict(
        {"device_id": "main", "state": "precheck", "restore_required": False}
    )
    assert record.state is DispatchState.PRECHECK
    assert record.snapshot is None


def test_an_active_record_with_a_snapshot_still_loads() -> None:
    """The invariant is snapshot-presence only: a legitimate active record round-trips unchanged."""
    record = DispatchRecord.from_dict(
        {
            "device_id": "main",
            "state": "charging",
            "restore_required": False,
            "intent": _intent_dict(),
            "snapshot": _snapshot_dict(),
        }
    )
    assert record.state is DispatchState.CHARGING
    assert record.snapshot is not None


def test_default_battery_convention() -> None:
    convention = RctBatteryPowerConvention()
    assert convention.target(PowerSetpoint(PowerDirection.CHARGE, 3000)) == -3000
    assert convention.target(PowerSetpoint(PowerDirection.DISCHARGE, 3000)) == 3000
    assert convention.setpoint(-3000) == PowerSetpoint(PowerDirection.CHARGE, 3000)


def test_sign_conventions_can_be_inverted_after_hardware_verification() -> None:
    battery = RctBatteryPowerConvention(discharge_positive=False)
    grid = RctGridPowerConvention(import_positive=False)
    assert battery.target(PowerSetpoint(PowerDirection.DISCHARGE, 500)) == -500
    assert grid.import_watts(-700) == 700


def test_legacy_snapshot_without_battery_power_raw_deserializes_as_none() -> None:
    """P2-02 backward-compat: a snapshot persisted before battery_power_raw existed carries no such
    key. It must still deserialize and round-trip; battery_power_raw stays None, which marks the
    restore path to fall back to the current convention (not sign-safe, documented as legacy)."""
    data = _snapshot_dict()
    assert "battery_power_raw" not in data
    record = DispatchRecord.from_dict(
        {
            "device_id": "main",
            "state": "charging",
            "restore_required": True,
            "intent": _intent_dict(),
            "snapshot": data,
        }
    )
    assert record.snapshot is not None
    assert record.snapshot.battery_power_raw is None
    assert DispatchRecord.from_dict(record.to_dict()).to_dict() == record.to_dict()


def test_snapshot_carrying_battery_power_raw_round_trips() -> None:
    """A new snapshot persists the raw wire value and restores it unchanged through from_dict()."""
    record = DispatchRecord.from_dict(
        {
            "device_id": "main",
            "state": "charging",
            "restore_required": True,
            "intent": _intent_dict(),
            "snapshot": _snapshot_dict(battery_power_raw=-1500.0),
        }
    )
    assert record.snapshot is not None
    assert record.snapshot.battery_power_raw == -1500.0
    assert DispatchRecord.from_dict(record.to_dict()).to_dict() == record.to_dict()


def test_device_control_snapshot_rejects_non_finite_battery_power_raw() -> None:
    with pytest.raises(ValueError, match="battery_power_raw"):
        DeviceControlSnapshot(PowerSetpoint(), 0.5, 1, False, AWARE_NOW, battery_power_raw=float("nan"))


def test_from_dict_rejects_a_string_last_commanded_watts() -> None:
    """P3-3: last_commanded.watts is a safety-deciding persisted number; a stored string such as
    "123" is corruption and must fail closed, not be coerced through a permissive float()."""
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict(
            {
                "device_id": "main",
                "state": "idle",
                "restore_required": False,
                "last_commanded": {"direction": "charge", "watts": "123"},
            }
        )
