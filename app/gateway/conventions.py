#!/usr/bin/env python3
#
# app/gateway/conventions.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The only mapping between RCT signed watts and dispatch directions (REQ-054/055)."""

import math
from dataclasses import dataclass
from typing import Literal

from app.dispatch.models import DispatchMode, PowerDirection, PowerSetpoint
from app.dispatch.soc_policy import SocTargetMode


@dataclass(frozen=True, slots=True)
class RctBatteryPowerConvention:
    discharge_positive: bool = True

    def target(self, setpoint: PowerSetpoint) -> float:
        if setpoint.direction is PowerDirection.NONE:
            return 0.0
        positive = setpoint.direction is PowerDirection.DISCHARGE
        sign = 1.0 if positive == self.discharge_positive else -1.0
        return sign * setpoint.watts

    def setpoint(self, watts: float) -> PowerSetpoint:
        if watts == 0:
            return PowerSetpoint()
        discharge = (watts > 0) == self.discharge_positive
        return PowerSetpoint(PowerDirection.DISCHARGE if discharge else PowerDirection.CHARGE, abs(watts))


@dataclass(frozen=True, slots=True)
class RctGridPowerConvention:
    import_positive: bool = True

    def import_watts(self, watts: float) -> float:
        return watts if self.import_positive else -watts


SocUnit = Literal["ratio", "percent"]


def soc_percent(value: float, unit: SocUnit) -> float:
    """Convert a battery-SoC reading to percent using the catalog-declared unit, never magnitude.

    ``unit`` is the catalog's declared unit for ``battery_soc`` (``MetricReading.unit`` /
    ``RegistryEntry.unit``), so ratio-vs-percent is an explicit metadata fact, not a guess from the
    number. The old magnitude heuristic turned a genuine 0.8 % reading into 80 %, which in the
    discharge path reads as "well above the stop target" and keeps discharging (safety bug).
    """
    if unit == "ratio":
        return value * 100.0
    if unit == "percent":
        return value
    raise ValueError(f"battery_soc has unexpected unit {unit!r}; cannot convert to percent")


def _finite_percent(value: object, name: str) -> float:
    """A percentage this derivation may compute with. NaN would survive every clamp below."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


@dataclass(frozen=True, slots=True)
class RctSocTargetConvention:
    """The raw value this device's SoC-target register wants — never a stop threshold.

    The business stop goal lives one layer up (``DispatchIntent.target_soc_percent``, enforced by
    ``target_reached``). Which rule turns it into a register value is a per-device,
    operator-settable policy (``app.dispatch.soc_policy``), because it is a device/firmware quirk:

    ``BUSINESS_TARGET`` writes the stop goal as-is, which is what the service has always done.
    ``BELOW_CURRENT_SOC`` accommodates the **unverified** hypothesis that a charge under external
    power control only delivers full power while the register sits below the measured SoC; no claim
    exists for the discharge direction, so discharge keeps the stop goal there too.
    """

    mode: SocTargetMode = SocTargetMode.BUSINESS_TARGET
    below_margin_percent: float = 5.0
    floor_percent: float = 0.0

    def register_ratio(
        self, dispatch_mode: DispatchMode, *, stop_target_percent: float, soc_percent: float
    ) -> float:
        if dispatch_mode not in (DispatchMode.CHARGE_FROM_GRID, DispatchMode.DISCHARGE_TO_LOAD):
            # HOLD omits the step entirely and EXPORT_TO_GRID is refused long before any adapter
            # call. Raising means a future caller cannot silently get the business-target value.
            raise ValueError(f"{dispatch_mode.value} has no SoC target register value")
        target = _finite_percent(stop_target_percent, "stop_target_percent")
        if self.mode is SocTargetMode.BELOW_CURRENT_SOC and dispatch_mode is DispatchMode.CHARGE_FROM_GRID:
            measured = _finite_percent(soc_percent, "soc_percent")
            margin = _finite_percent(self.below_margin_percent, "below_margin_percent")
            floor = _finite_percent(self.floor_percent, "floor_percent")
            percent = min(max(measured - margin, floor), 100.0)
        else:
            percent = target
        return min(max(percent / 100.0, 0.0), 1.0)

    def register_value(
        self, dispatch_mode: DispatchMode, *, stop_target_percent: float, soc_percent: float, unit: SocUnit
    ) -> float:
        """The value to write to the SoC-target register in the attested wire ``unit``.

        ``unit`` is the device's verified ``soc_target_unit`` (WRITE_PATH evidence, V-17). The
        derivation is always done in the canonical 0..1 ratio and then rendered: ``"ratio"`` writes
        0..1, ``"percent"`` writes 0..100. Without this a ``percent``-attested device was still sent
        the raw ratio (0.8 instead of 80), so the verification attested a unit the write ignored.
        """
        ratio = self.register_ratio(
            dispatch_mode, stop_target_percent=stop_target_percent, soc_percent=soc_percent
        )
        if unit == "ratio":
            return ratio
        if unit == "percent":
            return ratio * 100.0
        raise ValueError(f"unexpected soc_target_unit {unit!r}")
