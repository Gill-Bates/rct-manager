#!/usr/bin/env python3
#
# app/optimizer/models.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Validated, vendor-neutral inputs and outputs for the future energy optimizer."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from app.dispatch.models import check_printable_ascii
from app.spotmarket.models import SpotPriceSnapshot

TARGET_SOC_MIN_PERCENT = 7.0
TARGET_SOC_MAX_PERCENT = 97.0
REASON_MAX_LENGTH = 128


class EnergyAction(StrEnum):
    """A planning action, intentionally distinct from the executor's action vocabulary."""

    IDLE = "idle"
    CHARGE = "charge"
    DISCHARGE = "discharge"
    HOLD = "hold"
    AUTO = "auto"


def _require_aware(moment: datetime, field: str) -> None:
    if not isinstance(moment, datetime):
        raise ValueError(f"{field} must be a datetime")
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"{field} must be timezone-aware")


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
class BatteryState:
    """A measured battery snapshot and the trustworthy planning limits supplied with it."""

    soc_percent: float
    usable_capacity_wh: float
    max_charge_power_w: float
    max_discharge_power_w: float
    measured_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.measured_at, "measured_at")
        _require_finite(self.soc_percent, "soc_percent")
        if not 0 <= self.soc_percent <= 100:
            raise ValueError("soc_percent must be between 0 and 100")
        _require_finite(self.usable_capacity_wh, "usable_capacity_wh", positive=True)
        _require_finite(self.max_charge_power_w, "max_charge_power_w", positive=True)
        _require_finite(self.max_discharge_power_w, "max_discharge_power_w", positive=True)


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

    def __post_init__(self) -> None:
        _require_aware(self.now, "now")
        for field, expected_type in (
            ("battery", BatteryState),
            ("prices", SpotPriceSnapshot),
            ("solar_forecast", SolarForecast),
            ("consumption_forecast", ConsumptionForecast),
        ):
            if not isinstance(getattr(self, field), expected_type):
                raise ValueError(f"{field} must be a {expected_type.__name__}")
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

    def __post_init__(self) -> None:
        _require_aware(self.start, "start")
        _require_aware(self.end, "end")
        if self.end <= self.start:
            raise ValueError("end must be strictly after start")
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

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        _require_aware(self.valid_until, "valid_until")
        if self.valid_until <= self.created_at:
            raise ValueError("valid_until must be strictly after created_at")
        if not isinstance(self.slots, tuple):
            raise ValueError("slots must be a tuple")
        _require_sorted_non_overlapping(self.slots, EnergyPlanSlot, "slots")
