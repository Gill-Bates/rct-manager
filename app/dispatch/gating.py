#!/usr/bin/env python3
#
# app/dispatch/gating.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Write suppression for the battery control loop (REQ-050..REQ-052)."""

from datetime import datetime

from app.dispatch.models import DispatchConfig, PowerSetpoint


def should_write(
    desired: PowerSetpoint,
    current: PowerSetpoint,
    *,
    now: datetime,
    last_write_at: datetime | None,
    config: DispatchConfig,
    safety_stop: bool = False,
) -> bool:
    if safety_stop:
        return current.watts != 0 or current.direction != desired.direction
    if desired.direction != current.direction:
        return True
    if abs(desired.watts - current.watts) < config.power_write_deadband_w:
        return False
    if last_write_at is None:
        return True
    return (now - last_write_at).total_seconds() >= config.min_write_interval_seconds

