#!/usr/bin/env python3
#
# app/gateway/conventions.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The only mapping between RCT signed watts and dispatch directions (REQ-054/055)."""

from dataclasses import dataclass

from app.dispatch.models import PowerDirection, PowerSetpoint


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
