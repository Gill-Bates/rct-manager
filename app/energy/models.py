#!/usr/bin/env python3
#
# app/energy/models.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The Energy Manager's command model and its business projection of the dispatch layer.

Everything here is business vocabulary: an action an operator (or later a tariff engine) asks for,
and the state the service reports back. No register name, no raw strategy code, no byte width and no
sign convention ever appears in this module — those stay inside ``app/gateway/``.

Import direction (plan.md Decision 2): this module may import ``app.dispatch.models`` and
``app.dispatch.soc_policy`` and must never import ``app.dispatch.store``, which imports
``ArmedRecord`` from here.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.dispatch.models import (
    DispatchMode,
    DispatchState,
    PowerDirection,
    StopReason,
    check_printable_ascii,
)
from app.energy.readings import EnergyReadings

TARGET_SOC_MIN_PERCENT = 7.0  # hard bound of the product's target range
TARGET_SOC_MAX_PERCENT = 97.0
# Server-set lifetime of one manual command. A code constant, not an operator setting: it is a
# safety bound, and the operator-tunable cap already exists one layer down (DispatchConfig).
COMMAND_TTL_SECONDS = 3600.0

ARMED_BY_MAX_LENGTH = 64


class EnergyAction(StrEnum):
    CHARGE = "charge"
    DISCHARGE = "discharge"
    HOLD = "hold"
    AUTO = "auto"


class ActionReason(StrEnum):
    """Why an action is not offered — business language, one producer per value (design 2.3.3).

    The raw capability names behind ``HARDWARE_NOT_VERIFIED`` stay on the admin surface and in the
    log; the public status says that the hardware is not verified, not which register is in doubt.
    """

    NOT_ARMED = "not_armed"
    WRITE_NOT_PERMITTED = "write_not_permitted"
    LIMITS_MISSING = "limits_missing"
    HARDWARE_NOT_VERIFIED = "hardware_not_verified"
    RESTORE_REQUIRED = "restore_required"


class EnergyState(StrEnum):
    AUTOMATIC = "automatic"
    STARTING = "starting"
    CHARGING = "charging"
    DISCHARGING = "discharging"
    HOLDING = "holding"
    STOPPING = "stopping"
    FAULT = "fault"


@dataclass(frozen=True, slots=True)
class EnergyCommand:
    """The single input type of the service — nothing else is accepted.

    The HTTP routers build it from their request model and a tariff engine builds it directly, so a
    later caller cannot smuggle a TTL, a register name or a raw dispatch mode through.
    """

    action: EnergyAction
    target_soc_percent: float | None = None  # required for CHARGE/DISCHARGE, forbidden otherwise
    max_power_w: float | None = None  # absent -> the per-device configured limit


@dataclass(frozen=True, slots=True)
class ArmedRecord:
    """Whether one device accepts Energy Manager commands, plus what arming contributed.

    ``added_write_names`` is for display only: it records which register approvals arming added, so
    an operator can see what was granted. Disarming removes none of them.
    """

    device_id: str
    armed: bool = False
    added_write_names: tuple[str, ...] = ()
    armed_at: datetime | None = None  # UTC
    armed_by: str | None = None

    def __post_init__(self) -> None:
        check_printable_ascii(self.armed_by, "armed_by", ARMED_BY_MAX_LENGTH)


# The three projection tables below are the published contract. They are dicts, and a test asserts
# that every enum member is covered, so adding a dispatch state, mode or stop reason without a
# business mapping fails the test instead of inventing a contract value at runtime.

_STATE_FOR_DISPATCH_STATE: dict[DispatchState, EnergyState] = {
    DispatchState.IDLE: EnergyState.AUTOMATIC,
    DispatchState.PRECHECK: EnergyState.STARTING,
    DispatchState.APPLYING: EnergyState.STARTING,
    DispatchState.REPLACING: EnergyState.STARTING,
    DispatchState.CHARGING: EnergyState.CHARGING,
    DispatchState.DISCHARGING: EnergyState.DISCHARGING,
    DispatchState.HOLDING: EnergyState.HOLDING,
    DispatchState.TARGET_REACHED: EnergyState.STOPPING,
    DispatchState.RESTORING: EnergyState.STOPPING,
    DispatchState.FAULT: EnergyState.FAULT,
    DispatchState.FAULT_RESTORE_PENDING: EnergyState.FAULT,
}

# EXPORT_TO_GRID is not an Energy Manager action: the projection reports no action for it (and the
# state still reports what the device is doing). Read with .get(), never [], so an unmapped future
# mode degrades to null instead of failing a status call.
_ACTION_FOR_MODE: dict[DispatchMode, EnergyAction | None] = {
    DispatchMode.CHARGE_FROM_GRID: EnergyAction.CHARGE,
    DispatchMode.DISCHARGE_TO_LOAD: EnergyAction.DISCHARGE,
    DispatchMode.HOLD: EnergyAction.HOLD,
    DispatchMode.EXPORT_TO_GRID: None,
}


@dataclass(frozen=True, slots=True)
class ActionAvailability:
    action: EnergyAction
    available: bool
    reason: ActionReason | None = None


@dataclass(frozen=True, slots=True)
class TargetSocWindow:
    """The window a target SoC may be inside, after the configured bounds narrowed the hard ones."""

    min: float
    max: float


@dataclass(frozen=True, slots=True)
class EnergyDeviceStatus:
    """What the Energy Manager publishes about one device — business semantics only.

    ``power_limit_w`` is ``None`` while idle **and** while holding: a hold has no power budget, and
    ``0 W`` as a *limit* would read as "limited to zero" where the truth is "no limit applies". The
    commanded pair carries the zero instead (``commanded_power_w = 0.0``,
    ``commanded_direction = none``), which is what a GUI needs to render "Holding - battery idle".
    """

    device_id: str
    armed: bool
    state: EnergyState
    action: EnergyAction | None  # null whenever the device runs its own automatic operation
    target_soc_percent: float | None
    power_limit_w: float | None
    power_limit_clamped: bool
    commanded_power_w: float
    commanded_direction: PowerDirection
    until: datetime | None
    stop_reason: str | None
    time_limited: bool
    target_soc_window: TargetSocWindow
    readings: EnergyReadings
    actions: tuple[ActionAvailability, ...]


_STOP_REASON_PUBLIC: dict[StopReason, str] = {
    StopReason.TARGET_REACHED: "target_reached",
    StopReason.TTL_EXPIRED: "time_limit_reached",
    StopReason.TELEMETRY_STALE: "telemetry_unavailable",
    StopReason.OPERATOR_CANCELLED: "stopped_by_operator",
    StopReason.SNAPSHOT_STALE: "device_state_unreadable",
    StopReason.WRITE_NOT_ALLOWED: "write_not_permitted",
    StopReason.DEVICE_ERROR: "device_error",
    StopReason.STORE_UNAVAILABLE: "service_unavailable",
    StopReason.SHUTDOWN: "service_shutdown",
    StopReason.DEVICE_RECONFIGURED: "device_changed",
}
