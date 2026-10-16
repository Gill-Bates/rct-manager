#!/usr/bin/env python3
#
# app/optimizer/models.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Validated, vendor-neutral inputs and outputs for the future energy optimizer."""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from app.clock import require_aware as _require_aware
from app.dispatch.models import check_printable_ascii
from app.limits import TARGET_SOC_MAX_PERCENT, TARGET_SOC_MIN_PERCENT
from app.spotmarket.models import SpotPriceSnapshot

# The hard product target-SoC window is shared without importing the executor domain.
REASON_MAX_LENGTH = 128


class PlanPolicy(StrEnum):
    """How the plan trades the preferred power range against a target SoC and its deadline.

    Hard limits and the reserve are never traded in either policy. ``GENTLE`` stays inside the
    confirmed preferred power range for the target and reports what it cannot reach;
    ``MEET_DEADLINE`` raises the power up to the hard limit when the preferred range is not enough.
    """

    GENTLE = "gentle"
    MEET_DEADLINE = "meet_deadline"


class ForecastBasis(StrEnum):
    """How a plan's consumption input was obtained; stable public contract values.

    ``BOOTSTRAP_BASELINE``: no usable consumption history, a configured flat baseline load is used.
    ``HISTORY_FALLBACK``: thin or partial history, a median of the stored history is used.
    ``ADAPTIVE_HISTORY``: sufficient history, the adaptive profile is used.
    Free-form descriptions stay in the plan assumptions; renaming an internal provider must not
    change these values.
    """

    BOOTSTRAP_BASELINE = "bootstrap_baseline"
    HISTORY_FALLBACK = "history_fallback"
    ADAPTIVE_HISTORY = "adaptive_history"


class ForecastConfidence(StrEnum):
    """How far a plan's consumption forecast can be trusted; stable public contract values.

    ``LOW``: baseline or fallback forecast. ``NORMAL``: sufficient, well-covered history.
    """

    LOW = "low"
    NORMAL = "normal"


class EfficiencySource(StrEnum):
    """Where a charge efficiency comes from; ``ASSUMED`` marks the documented planning default."""

    CONFIGURED = "configured"
    VERIFIED = "verified"
    ASSUMED = "assumed"


class LimitingReasonCode(StrEnum):
    """Machine-readable reasons a plan could not do everything it was asked to."""

    RESERVE_NOT_SECURABLE = "reserve_not_securable"
    TARGET_UNREACHABLE_IN_PREFERRED_RANGE = "target_unreachable_in_preferred_range"
    TARGET_UNREACHABLE_AT_HARD_LIMIT = "target_unreachable_at_hard_limit"
    TARGET_ABOVE_MAX_SOC = "target_above_max_soc"
    TARGET_BEYOND_HORIZON = "target_beyond_horizon"
    PREFERRED_RANGE_EXCEEDED_FOR_RESERVE = "preferred_range_exceeded_for_reserve"
    PREFERRED_RANGE_EXCEEDED_FOR_DEADLINE = "preferred_range_exceeded_for_deadline"
    PREFERRED_RANGE_CLAMPED = "preferred_range_clamped"
    PREFERRED_MINIMUM_EXTRA_ENERGY = "preferred_minimum_extra_energy"
    PREFERRED_RANGE_NOT_CONFIRMED = "preferred_range_not_confirmed"
    LIVE_LIMIT_UNAVAILABLE = "live_limit_unavailable"
    LIVE_LIMIT_BELOW_STATIC = "live_limit_below_static"


class EnergyAction(StrEnum):
    """A planning action, intentionally distinct from the executor's action vocabulary."""

    IDLE = "idle"
    CHARGE = "charge"
    DISCHARGE = "discharge"
    HOLD = "hold"
    AUTO = "auto"


def _require_finite(value: float, field: str, *, positive: bool = False, non_negative: bool = False) -> None:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        finite = math.isfinite(value)
    except TypeError as error:
        raise ValueError(f"{field} must be a finite number") from error
    if not finite:
        raise ValueError(f"{field} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{field} must be greater than zero")
    if non_negative and value < 0:
        raise ValueError(f"{field} must be non-negative")


def _require_sorted_non_overlapping(
    intervals: tuple[object, ...], interval_type: type, field: str = "intervals"
) -> None:
    if not all(isinstance(interval, interval_type) for interval in intervals):
        raise ValueError(f"{field} must contain {interval_type.__name__} values")
    for previous, current in pairwise(intervals):
        if current.start < previous.end:  # type: ignore[attr-defined]
            raise ValueError("intervals must be ordered and non-overlapping")


@dataclass(frozen=True, slots=True)
class ChargeEfficiency:
    """A constant AC-grid-to-battery-terminal charge efficiency with its provenance.

    A single constant by design: no efficiency curve over power, SoC or temperature is modelled.
    """

    value: float
    source: EfficiencySource

    def __post_init__(self) -> None:
        _require_finite(self.value, "charge_efficiency")
        if not 0 < self.value <= 1:
            raise ValueError("charge_efficiency must be within (0, 1]")
        if not isinstance(self.source, EfficiencySource):
            raise ValueError("source must be an EfficiencySource")


# Used when no configured or verified value exists. It is a conservative planning assumption, not a
# measurement, and the plan always reports it as ASSUMED; 100 % is never assumed silently.
DEFAULT_ASSUMED_CHARGE_EFFICIENCY = ChargeEfficiency(0.90, EfficiencySource.ASSUMED)


@dataclass(frozen=True, slots=True)
class LimitingReason:
    """One reason the plan falls short of, or deviates from, what was asked."""

    code: LimitingReasonCode
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, LimitingReasonCode):
            raise ValueError("code must be a LimitingReasonCode")
        check_printable_ascii(self.detail, "detail", REASON_MAX_LENGTH)


@dataclass(frozen=True, slots=True)
class ChargePowerEnvelope:
    """Permitted (hard) and preferred charge power, kept apart from the required energy.

    ``static_charge_limit_w`` is the configured device limit and stays authoritative.
    ``live_charge_limit_w`` is a verified, fresh live limit or ``None``; ``None`` never means
    unlimited. ``permitted_charge_power_w`` is the minimum of the two. The preferred range is a
    confirmed, optional sub-range of the permitted power (``None`` when nothing is confirmed).
    """

    static_charge_limit_w: float
    live_charge_limit_w: float | None
    live_limit_available: bool
    permitted_charge_power_w: float
    preferred_min_w: float | None = None
    preferred_max_w: float | None = None


@dataclass(frozen=True, slots=True)
class PlanEnergy:
    """The four charge-energy domains of a plan or plan slot, in Wh. Efficiency sits only between
    the terminal and the grid term.

    * ``commanded_charge_energy_wh``: dispatch setpoint power times duration.
    * ``battery_terminal_energy_wh``: energy that actually reaches the battery terminals.
    * ``stored_energy_delta_wh``: SoC change times usable capacity.
    * ``grid_charge_energy_wh``: AC grid draw caused by charging alone, house consumption excluded.
    """

    commanded_charge_energy_wh: float = 0.0
    battery_terminal_energy_wh: float = 0.0
    stored_energy_delta_wh: float = 0.0
    grid_charge_energy_wh: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "commanded_charge_energy_wh",
            "battery_terminal_energy_wh",
            "stored_energy_delta_wh",
            "grid_charge_energy_wh",
        ):
            _require_finite(getattr(self, name), name, non_negative=True)


@dataclass(frozen=True, slots=True)
class PlanAssumptions:
    """Everything the plan took as given and could not prove; shown to the operator as such."""

    charge_efficiency: ChargeEfficiency
    power_reference: str
    power_reference_verified: bool
    live_limit_status: str
    forecast_basis: str
    battery_internal_losses: str
    charge_efficiency_note: str = ""
    preferred_range_status: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.charge_efficiency, ChargeEfficiency):
            raise ValueError("charge_efficiency must be a ChargeEfficiency")
        for name in (
            "power_reference", "live_limit_status", "forecast_basis", "battery_internal_losses",
            "charge_efficiency_note", "preferred_range_status",
        ):  # fmt: skip
            check_printable_ascii(getattr(self, name), name, 256)


SlotSeries = tuple[tuple[datetime, float | None], ...]


@dataclass(frozen=True, slots=True)
class PlannedCharge:
    """One planned charge slot: its interval and the battery-side energy it should deliver."""

    start: datetime
    end: datetime
    terminal_energy_wh: float


@dataclass(frozen=True, slots=True)
class PlanBasis:
    """What a plan was computed from, so :mod:`app.optimizer.replan` can tell when it went stale.

    ``trajectory`` is the simulated SoC (percent) at the creation instant and after every slot.
    The three series are keyed by slot start; prices are EUR/kWh, solar and consumption Wh.
    """

    soc_percent: float
    trajectory: tuple[tuple[datetime, float], ...]
    permitted_charge_power_w: float
    live_charge_limit_w: float | None
    prices: SlotSeries
    solar_wh: SlotSeries
    consumption_wh: SlotSeries
    planned_charges: tuple[PlannedCharge, ...]


@dataclass(frozen=True, slots=True)
class BatteryState:
    """A measured battery snapshot and the trustworthy planning limits supplied with it.

    ``max_charge_power_w`` is the static (configured) hard limit. ``live_charge_limit_w`` is a
    hardware-verified, fresh live limit, or ``None`` when none is available; it only ever lowers the
    permitted power and is never invented.
    """

    soc_percent: float
    usable_capacity_wh: float
    max_charge_power_w: float
    max_discharge_power_w: float
    measured_at: datetime
    live_charge_limit_w: float | None = None

    @property
    def permitted_charge_power_w(self) -> float:
        """The hard charge ceiling: the static limit, lowered by a verified live limit if present."""
        if self.live_charge_limit_w is None:
            return self.max_charge_power_w
        return min(self.max_charge_power_w, self.live_charge_limit_w)

    def __post_init__(self) -> None:
        _require_aware(self.measured_at, "measured_at")
        _require_finite(self.soc_percent, "soc_percent")
        if not 0 <= self.soc_percent <= 100:
            raise ValueError("soc_percent must be between 0 and 100")
        _require_finite(self.usable_capacity_wh, "usable_capacity_wh", positive=True)
        _require_finite(self.max_charge_power_w, "max_charge_power_w", positive=True)
        _require_finite(self.max_discharge_power_w, "max_discharge_power_w", positive=True)
        if self.live_charge_limit_w is not None:
            _require_finite(self.live_charge_limit_w, "live_charge_limit_w", positive=True)


@dataclass(frozen=True, slots=True)
class SolarForecastInterval:
    """Forecast solar production for one explicit interval, measured in Wh."""

    start: datetime
    end: datetime
    energy_wh: float

    def __post_init__(self) -> None:
        _require_aware(self.start, "start")
        _require_aware(self.end, "end")
        if self.end <= self.start:
            raise ValueError("end must be strictly after start")
        _require_finite(self.energy_wh, "energy_wh", non_negative=True)


@dataclass(frozen=True, slots=True)
class SolarForecast:
    """An ordered, non-overlapping solar-production forecast."""

    created_at: datetime
    intervals: tuple[SolarForecastInterval, ...]

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        if not isinstance(self.intervals, tuple):
            raise ValueError("intervals must be a tuple")
        _require_sorted_non_overlapping(self.intervals, SolarForecastInterval)


@dataclass(frozen=True, slots=True)
class ConsumptionForecastInterval:
    """Forecast household consumption for one explicit interval, measured in Wh."""

    start: datetime
    end: datetime
    energy_wh: float

    def __post_init__(self) -> None:
        _require_aware(self.start, "start")
        _require_aware(self.end, "end")
        if self.end <= self.start:
            raise ValueError("end must be strictly after start")
        _require_finite(self.energy_wh, "energy_wh", non_negative=True)


@dataclass(frozen=True, slots=True)
class ConsumptionForecast:
    """An ordered, non-overlapping household-consumption forecast."""

    created_at: datetime
    intervals: tuple[ConsumptionForecastInterval, ...]

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        if not isinstance(self.intervals, tuple):
            raise ValueError("intervals must be a tuple")
        _require_sorted_non_overlapping(self.intervals, ConsumptionForecastInterval)


@dataclass(frozen=True, slots=True)
class PlanningInput:
    """The pure, already-fetched snapshot given to an optimizer implementation."""

    now: datetime
    battery: BatteryState
    prices: SpotPriceSnapshot
    solar_forecast: SolarForecast
    consumption_forecast: ConsumptionForecast
    horizon: timedelta
    # Where the forecasts come from, echoed into the plan's assumptions.
    forecast_basis: str = "not stated by the caller"

    def __post_init__(self) -> None:
        _require_aware(self.now, "now")
        for name, expected_type in (
            ("battery", BatteryState),
            ("prices", SpotPriceSnapshot),
            ("solar_forecast", SolarForecast),
            ("consumption_forecast", ConsumptionForecast),
        ):
            if not isinstance(getattr(self, name), expected_type):
                raise ValueError(f"{name} must be a {expected_type.__name__}")
        if not isinstance(self.horizon, timedelta) or self.horizon <= timedelta():
            raise ValueError("horizon must be a positive timedelta")


@dataclass(frozen=True, slots=True)
class EnergyPlanSlot:
    """One proposed, non-executing battery action for an explicit interval."""

    start: datetime
    end: datetime
    action: EnergyAction
    target_soc_percent: float | None
    max_power_w: float | None
    reason: str
    energy: PlanEnergy | None = None

    def __post_init__(self) -> None:
        _require_aware(self.start, "start")
        _require_aware(self.end, "end")
        if self.end <= self.start:
            raise ValueError("end must be strictly after start")
        if self.energy is not None and not isinstance(self.energy, PlanEnergy):
            raise ValueError("energy must be a PlanEnergy")
        if not isinstance(self.action, EnergyAction):
            raise ValueError("action must be an EnergyAction")
        if self.action in {EnergyAction.CHARGE, EnergyAction.DISCHARGE}:
            if self.target_soc_percent is None:
                raise ValueError("charge and discharge actions require target_soc_percent")
            _require_finite(self.target_soc_percent, "target_soc_percent")
            if not TARGET_SOC_MIN_PERCENT <= self.target_soc_percent <= TARGET_SOC_MAX_PERCENT:
                raise ValueError("target_soc_percent must be between 7 and 97")
        elif self.target_soc_percent is not None:
            raise ValueError("this action forbids target_soc_percent")
        if self.action in {EnergyAction.IDLE, EnergyAction.HOLD, EnergyAction.AUTO} and self.max_power_w is not None:
            raise ValueError("this action forbids max_power_w")
        if self.max_power_w is not None:
            _require_finite(self.max_power_w, "max_power_w", positive=True)
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        check_printable_ascii(self.reason, "reason", REASON_MAX_LENGTH)


@dataclass(frozen=True, slots=True)
class EnergyPlan:
    """An ordered proposal which is valid only until its declared expiry instant."""

    created_at: datetime
    valid_until: datetime
    slots: tuple[EnergyPlanSlot, ...]
    # Reserve and target are different things. The reserve is a hard lower bound along the whole
    # trajectory; the target is an optional goal at ``target_time``. A plan can satisfy every reserve
    # and still report the target as unreachable, and the reverse.
    policy: PlanPolicy = PlanPolicy.GENTLE
    reserve_soc_percent: float | None = None
    reserve_satisfied: bool = True
    reserve_shortfall_wh: float = 0.0
    target_soc_percent: float | None = None
    target_time: datetime | None = None
    target_reachable: bool = True
    unserved_target_energy_wh: float = 0.0
    power: ChargePowerEnvelope | None = None
    energy: PlanEnergy = field(default_factory=PlanEnergy)
    assumptions: PlanAssumptions | None = None
    limiting_reasons: tuple[LimitingReason, ...] = ()
    basis: PlanBasis | None = None

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        _require_aware(self.valid_until, "valid_until")
        if self.valid_until <= self.created_at:
            raise ValueError("valid_until must be strictly after created_at")
        if not isinstance(self.slots, tuple):
            raise ValueError("slots must be a tuple")
        _require_sorted_non_overlapping(self.slots, EnergyPlanSlot, "slots")
