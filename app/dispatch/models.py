#!/usr/bin/env python3
#
# app/dispatch/models.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Vendor-neutral battery dispatch models (REQ-010, REQ-054, REQ-101)."""

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

NOTE_MAX_LENGTH = 200


def check_printable_ascii(value: str | None, field: str, max_len: int) -> None:
    """Reject a free-text field that is too long or holds a non-printable or non-ASCII character."""
    if value is None:
        return
    if len(value) > max_len:
        raise ValueError(f"{field} must contain at most {max_len} characters")
    if any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError(f"{field} must contain printable ASCII")


class DispatchRecordCorrupt(ValueError):
    """A persisted ``DispatchRecord`` is incomplete, inconsistent, or otherwise untrustworthy.

    Raised by ``DispatchRecord.from_dict()`` instead of silently filling the gap with a default.
    A record this safety-critical must fail closed: an unreadable persisted state is never treated
    as "nothing to do, device clean" (IDLE).
    """


class DispatchMode(StrEnum):
    CHARGE_FROM_GRID = "charge_from_grid"
    DISCHARGE_TO_LOAD = "discharge_to_load"
    EXPORT_TO_GRID = "export_to_grid"
    # Pin the battery at 0 W under external control: no SoC goal, no power budget.
    HOLD = "hold"


class DispatchState(StrEnum):
    IDLE = "idle"
    PRECHECK = "precheck"
    APPLYING = "applying"
    CHARGING = "charging"
    DISCHARGING = "discharging"
    REPLACING = "replacing"
    TARGET_REACHED = "target_reached"
    RESTORING = "restoring"
    FAULT = "fault"
    FAULT_RESTORE_PENDING = "fault_restore_pending"
    HOLDING = "holding"


class DispatchPhase(StrEnum):
    IDLE = "idle"
    PRECHECK = "precheck"
    APPLYING = "applying"
    CONTROLLING = "controlling"
    REPLACING = "replacing"
    RESTORING = "restoring"
    FAULT = "fault"


class PowerDirection(StrEnum):
    NONE = "none"
    CHARGE = "charge"
    DISCHARGE = "discharge"


class StopReason(StrEnum):
    TARGET_REACHED = "target_reached"
    TTL_EXPIRED = "ttl_expired"
    TELEMETRY_STALE = "telemetry_stale"
    OPERATOR_CANCELLED = "operator_cancelled"
    SHUTDOWN = "shutdown"
    DEVICE_ERROR = "device_error"
    WRITE_NOT_ALLOWED = "write_not_allowed"
    STORE_UNAVAILABLE = "store_unavailable"
    DEVICE_RECONFIGURED = "device_reconfigured"  # host/port changed or the device was removed
    SNAPSHOT_STALE = "snapshot_stale"  # D5: read_snapshot() returned all_fresh=False at dispatch start


@dataclass(frozen=True, slots=True)
class PowerSetpoint:
    direction: PowerDirection = PowerDirection.NONE
    watts: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.watts):
            raise ValueError("power setpoint watts must be finite")
        if self.watts < 0:
            raise ValueError("power setpoint watts must be non-negative")
        if self.direction is PowerDirection.NONE and self.watts != 0:
            raise ValueError("direction none requires zero watts")


@dataclass(frozen=True, slots=True)
class DispatchCommand:
    mode: DispatchMode
    # The business stop goal, not a register value: None for a mode without one (HOLD). The field
    # keeps its position and gets no default, so the positional call sites stay valid.
    target_soc_percent: float | None
    max_power_w: float
    valid_until: datetime
    expected_operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchIntent:
    operation_id: str
    mode: DispatchMode
    target_soc_percent: float | None
    max_power_w: float
    max_power_w_requested: float
    valid_until: datetime
    valid_until_requested: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DeviceControlSnapshot:
    battery_setpoint: PowerSetpoint
    soc_target_ratio: float
    soc_strategy_code: int
    grid_charge_enabled: bool
    read_at: datetime
    all_fresh: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.soc_target_ratio):
            raise ValueError("soc_target_ratio must be finite")


@dataclass(frozen=True, slots=True)
class ControlTelemetry:
    soc_percent: float
    soc_age_seconds: float
    soc_source: Literal["device", "cache"]
    grid_import_w: float
    grid_age_seconds: float
    grid_source: Literal["device", "cache"]
    battery_setpoint: PowerSetpoint
    battery_age_seconds: float
    battery_source: Literal["device", "cache"]
    household_load_w: float | None = None
    household_age_seconds: float | None = None

    def __post_init__(self) -> None:
        # Every field a staleness check or calculate_setpoint() can act on must be finite: a NaN
        # age compares False against any threshold (`nan > 5.0` is False), silently bypassing the
        # stale-telemetry guard that is supposed to stop dispatch (C2).
        required = (
            self.soc_percent,
            self.soc_age_seconds,
            self.grid_import_w,
            self.grid_age_seconds,
            self.battery_age_seconds,
        )
        if not all(math.isfinite(value) for value in required):
            raise ValueError("control telemetry values must be finite")
        optional = (self.household_load_w, self.household_age_seconds)
        if any(value is not None and not math.isfinite(value) for value in optional):
            raise ValueError("household telemetry values must be finite when present")


@dataclass(frozen=True, slots=True)
class DeviceLimits:
    max_charge_power_w: float
    max_discharge_power_w: float
    engineering_mode: bool = False

    def __post_init__(self) -> None:
        for name in ("max_charge_power_w", "max_discharge_power_w"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class DispatchConfig:
    min_soc: float = 5.0
    max_soc: float = 95.0
    grid_import_reserve_w: float = 100.0
    grid_control_deadband_w: float = 200.0
    power_write_deadband_w: float = 200.0
    min_write_interval_seconds: float = 5.0
    cycle_interval_seconds: float = 5.0
    telemetry_timeout_seconds: float = 15.0
    control_telemetry_max_age_seconds: float = 5.0
    soc_telemetry_max_age_seconds: float = 15.0
    max_operation_duration_seconds: float = 21600.0
    # Applies only while a device's engineering mode is *active*, i.e. while dispatch runs on
    # hardware that is not verified for the requested mode.
    max_operation_duration_engineering_seconds: float = 1800.0
    # Writing the export limit needs its own verification, so it only enters the gate's required
    # set when that write is switched on. Single source for gate and adapter.
    limit_export_during_discharge: bool = False

    def __post_init__(self) -> None:
        numeric_fields = (
            "min_soc", "max_soc", "grid_import_reserve_w", "grid_control_deadband_w",
            "power_write_deadband_w", "min_write_interval_seconds", "cycle_interval_seconds",
            "telemetry_timeout_seconds", "control_telemetry_max_age_seconds",
            "soc_telemetry_max_age_seconds", "max_operation_duration_seconds",
            "max_operation_duration_engineering_seconds",
        )  # fmt: skip
        for name in numeric_fields:
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.min_soc < 0 or self.max_soc > 100 or self.min_soc >= self.max_soc:
            raise ValueError("min_soc must be smaller than max_soc, both within 0..100")
        if self.max_operation_duration_engineering_seconds > self.max_operation_duration_seconds:
            raise ValueError(
                "max_operation_duration_engineering_seconds must not exceed max_operation_duration_seconds"
            )
        if self.power_write_deadband_w > self.grid_control_deadband_w:
            raise ValueError("power_write_deadband_w must not exceed grid_control_deadband_w")


def _strict_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a bool, got {type(value).__name__}")
    return value


def _strict_number(value: object, field_name: str, kind: type) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number, got {type(value).__name__}")
    return kind(value)


def _tz_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _tz_aware_or_none(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be an ISO datetime string")
    return _tz_aware(datetime.fromisoformat(value), field_name)


def _optional_number(value: object, field_name: str) -> float | None:
    """A persisted ``float | None`` field: absent/null is a real value, a non-number is corruption."""
    if value is None:
        return None
    return float(_strict_number(value, field_name, float))


def _intent_from_dict(intent_data: dict) -> DispatchIntent:
    return DispatchIntent(
        operation_id=intent_data["operation_id"],
        mode=DispatchMode(intent_data["mode"]),
        # A mode without a SoC goal (HOLD) persists target_soc_percent as null, and a record
        # written before the field could be null carries a number — both load; a string does not.
        target_soc_percent=_optional_number(intent_data.get("target_soc_percent"), "target_soc_percent"),
        max_power_w=_strict_number(intent_data["max_power_w"], "max_power_w", float),
        max_power_w_requested=_strict_number(
            intent_data["max_power_w_requested"], "max_power_w_requested", float
        ),
        valid_until=_tz_aware(datetime.fromisoformat(intent_data["valid_until"]), "intent.valid_until"),
        valid_until_requested=_tz_aware(
            datetime.fromisoformat(intent_data["valid_until_requested"]), "intent.valid_until_requested"
        ),
        created_at=_tz_aware(datetime.fromisoformat(intent_data["created_at"]), "intent.created_at"),
    )


def _snapshot_from_dict(snapshot_data: dict) -> DeviceControlSnapshot:
    setpoint = snapshot_data["battery_setpoint"]
    return DeviceControlSnapshot(
        battery_setpoint=PowerSetpoint(
            PowerDirection(setpoint["direction"]), _strict_number(setpoint["watts"], "watts", float)
        ),
        soc_target_ratio=_strict_number(snapshot_data["soc_target_ratio"], "soc_target_ratio", float),
        soc_strategy_code=_strict_number(snapshot_data["soc_strategy_code"], "soc_strategy_code", int),
        grid_charge_enabled=_strict_bool(snapshot_data["grid_charge_enabled"], "grid_charge_enabled"),
        read_at=_tz_aware(datetime.fromisoformat(snapshot_data["read_at"]), "snapshot.read_at"),
        # Fail-closed (D5): a snapshot with no freshness information on record is not assumed
        # fresh. The real adapter (app/gateway/rct_dispatch.py) can and does return False.
        all_fresh=_strict_bool(snapshot_data.get("all_fresh", False), "all_fresh"),
    )


def validate_record_invariants(record: "DispatchRecord") -> None:
    """Reject state/intent/snapshot/restore_required combinations the state machine cannot have
    produced itself (C5). Called only at the persistence boundary (``from_dict``), not on every
    in-memory transition the controller makes under its own control.
    """
    if record.state is DispatchState.IDLE:
        if record.intent is not None:
            raise DispatchRecordCorrupt("state is idle but intent is present")
        if record.restore_required:
            raise DispatchRecordCorrupt("state is idle but restore_required is set")
    # PRECHECK without an intent is legal: recover() builds and persists exactly that combination,
    # so from_dict() must be able to round-trip it.
    elif record.state is not DispatchState.PRECHECK and record.intent is None:
        raise DispatchRecordCorrupt(f"state is {record.state.value} but intent is missing")
    if record.state in (DispatchState.RESTORING, DispatchState.FAULT_RESTORE_PENDING) and not record.restore_required:
        raise DispatchRecordCorrupt(f"state is {record.state.value} but restore_required is not set")


@dataclass(slots=True)
class DispatchRecord:
    device_id: str
    state: DispatchState = DispatchState.IDLE
    intent: DispatchIntent | None = None
    snapshot: DeviceControlSnapshot | None = None
    last_commanded: PowerSetpoint = field(default_factory=PowerSetpoint)
    last_write_at: datetime | None = None
    stop_reason: StopReason | None = None
    fault_code: str | None = None
    restore_required: bool = False
    plan: list[dict[str, str]] = field(default_factory=list)
    record_version: int = 0
    # Earliest time a FAULT_RESTORE_PENDING device is eligible for an automatic retry, and the
    # number of consecutive failed restore attempts that drives the backoff.
    next_restore_at: datetime | None = None
    restore_attempts: int = 0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        data["last_commanded"]["direction"] = self.last_commanded.direction.value
        for name in ("last_write_at", "next_restore_at"):
            if data[name] is not None:
                data[name] = data[name].isoformat()
        if self.stop_reason is not None:
            data["stop_reason"] = self.stop_reason.value
        if self.intent is not None:
            data["intent"]["mode"] = self.intent.mode.value
            for name in ("valid_until", "valid_until_requested", "created_at"):
                data["intent"][name] = getattr(self.intent, name).isoformat()
        if self.snapshot is not None:
            data["snapshot"]["battery_setpoint"]["direction"] = self.snapshot.battery_setpoint.direction.value
            data["snapshot"]["read_at"] = self.snapshot.read_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DispatchRecord":
        """Fail-closed deserialization (C1): a record that cannot be read whole is never silently
        treated as a clean IDLE record. Missing or malformed safety-deciding fields raise
        ``DispatchRecordCorrupt`` instead of being defaulted.
        """
        if not isinstance(data, dict) or "device_id" not in data:
            raise DispatchRecordCorrupt("dispatch record is missing device_id")
        if "state" not in data or "restore_required" not in data:
            raise DispatchRecordCorrupt("dispatch record is missing state or restore_required")
        try:
            state = DispatchState(data["state"])
            restore_required = _strict_bool(data["restore_required"], "restore_required")
            intent_data = data.get("intent")
            intent = _intent_from_dict(intent_data) if intent_data else None
            snapshot_data = data.get("snapshot")
            snapshot = _snapshot_from_dict(snapshot_data) if snapshot_data else None
            setpoint = data.get("last_commanded", {"direction": "none", "watts": 0.0})
            last_commanded = PowerSetpoint(PowerDirection(setpoint["direction"]), float(setpoint["watts"]))
            last_write_at = _tz_aware_or_none(data.get("last_write_at"), "last_write_at")
            next_restore_at = _tz_aware_or_none(data.get("next_restore_at"), "next_restore_at")
            stop_reason = StopReason(data["stop_reason"]) if data.get("stop_reason") else None
            record_version = _strict_number(data.get("record_version", 0), "record_version", int)
            restore_attempts = max(0, _strict_number(data.get("restore_attempts", 0), "restore_attempts", int))
        except (KeyError, TypeError, ValueError) as exc:
            raise DispatchRecordCorrupt(f"dispatch record is malformed: {exc}") from exc
        record = cls(
            device_id=data["device_id"],
            state=state,
            intent=intent,
            snapshot=snapshot,
            last_commanded=last_commanded,
            last_write_at=last_write_at,
            stop_reason=stop_reason,
            fault_code=data.get("fault_code"),
            restore_required=restore_required,
            plan=list(data.get("plan", [])),
            record_version=record_version,
            next_restore_at=next_restore_at,
            restore_attempts=restore_attempts,
        )
        validate_record_invariants(record)
        return record


def phase_for(state: DispatchState) -> DispatchPhase:
    if state in (
        DispatchState.CHARGING,
        DispatchState.DISCHARGING,
        DispatchState.HOLDING,
        DispatchState.TARGET_REACHED,
    ):
        return DispatchPhase.CONTROLLING
    if state in (DispatchState.FAULT, DispatchState.FAULT_RESTORE_PENDING):
        return DispatchPhase.FAULT
    return DispatchPhase(state.value)


@dataclass(frozen=True, slots=True)
class DispatchStatus:
    device_id: str
    operation_id: str | None
    mode: DispatchMode | None
    state: DispatchState
    phase: DispatchPhase
    control_state: Literal["controlled", "restored", "unknown"]
    restore_required: bool
    target_soc_percent: float | None
    max_power_w: float
    max_power_w_requested: float
    valid_until: datetime | None
    commanded_direction: PowerDirection
    commanded_power_w: float
    stop_reason: StopReason | None = None
    fault_code: str | None = None
    replaced: bool = False

