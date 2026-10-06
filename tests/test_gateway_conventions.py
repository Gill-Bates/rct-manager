#!/usr/bin/env python3
#
# tests/test_gateway_conventions.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""RCT sign conventions are isolated and invertible."""

from app.dispatch.models import PowerDirection, PowerSetpoint
from app.gateway.conventions import RctBatteryPowerConvention, RctGridPowerConvention


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
