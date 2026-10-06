#!/usr/bin/env python3
#
# app/dispatch/strategy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pure setpoint strategies for battery dispatch (REQ-014, REQ-030)."""

from app.dispatch.models import (
    ControlTelemetry,
    DispatchConfig,
    DispatchMode,
    PowerDirection,
    PowerSetpoint,
)


def target_reached(mode: DispatchMode, soc_percent: float, target_soc_percent: float) -> bool:
    if mode is DispatchMode.CHARGE_FROM_GRID:
        return soc_percent >= target_soc_percent
    return soc_percent <= target_soc_percent


def calculate_setpoint(
    mode: DispatchMode,
    telemetry: ControlTelemetry,
    *,
    target_soc_percent: float,
    max_power_w: float,
    config: DispatchConfig,
    last: PowerSetpoint,
) -> PowerSetpoint:
    if target_reached(mode, telemetry.soc_percent, target_soc_percent):
        return PowerSetpoint()
    if mode is DispatchMode.CHARGE_FROM_GRID:
        return PowerSetpoint(PowerDirection.CHARGE, max_power_w)
    if mode is not DispatchMode.DISCHARGE_TO_LOAD:
        return PowerSetpoint()
    # Positive grid_import_w means import. Increase discharge by the excess over the reserve.
    base = last.watts if last.direction is PowerDirection.DISCHARGE else 0.0
    error = telemetry.grid_import_w - config.grid_import_reserve_w
    if telemetry.grid_import_w <= 0:
        # Export is a safety boundary, not a comfort deadband: reduce immediately.
        desired = base + error
    elif abs(error) <= config.grid_control_deadband_w:
        desired = base
    else:
        desired = base + error
    # A non-positive measured import means PV already covers the load. Never create export.
    if 0 < telemetry.grid_import_w <= config.grid_import_reserve_w:
        desired = min(desired, base)
    desired = max(0.0, min(max_power_w, desired))
    return PowerSetpoint(PowerDirection.DISCHARGE, desired) if desired else PowerSetpoint()
