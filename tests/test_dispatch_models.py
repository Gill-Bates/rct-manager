#!/usr/bin/env python3
#
# tests/test_dispatch_models.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Model-layer safety invariants (list C): fail-closed persistence, finite values, and the
state/intent/snapshot/restore_required consistency rule.
"""

from datetime import UTC, datetime

import pytest

from app.dispatch.models import (
    ControlTelemetry,
    DeviceControlSnapshot,
    DeviceLimits,
    DispatchConfig,
    DispatchRecord,
    DispatchRecordCorrupt,
    DispatchState,
    PowerDirection,
    PowerSetpoint,
)

AWARE_NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


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


# --- C1: fail-closed from_dict() ---------------------------------------------------------------


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


# --- C2: NaN/inf rejected -----------------------------------------------------------------------


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


# --- naive datetimes rejected --------------------------------------------------------------------


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


# --- D5: all_fresh defaults to False on a missing key -------------------------------------------


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


# --- C5: invalid state/intent/snapshot/restore_required combinations rejected -------------------


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


# --- DeviceLimits / DispatchConfig domain validation ---------------------------------------------


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


# --- strict bool/number coercion in from_dict() --------------------------------------------------


def test_from_dict_rejects_a_truthy_string_instead_of_a_real_bool() -> None:
    with pytest.raises(DispatchRecordCorrupt):
        DispatchRecord.from_dict({"device_id": "x", "state": "idle", "restore_required": "yes"})


def test_device_control_snapshot_rejects_non_finite_soc_target_ratio() -> None:
    with pytest.raises(ValueError):
        DeviceControlSnapshot(PowerSetpoint(), float("nan"), 1, False, AWARE_NOW)
