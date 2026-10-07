#!/usr/bin/env python3
#
# app/energy/readings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The four business figures the Energy Manager publishes with its status (design 2.3.2).

Vendor-neutral by construction: no register name appears here. Which RCT objects feed these values,
and which sign convention applies, is the adapter's business (``app.gateway.energy_readings``).

The figures are advisory — they are displayed, they never gate an input and never decide a write —
so a reading is never allowed to raise: a stale figure must not be able to block a stop command.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DeviceReading:
    """One figure, its age, and whether it is good enough to act on.

    ``stale`` is True whenever the value is absent or the cache entry is no longer fresh. The age of
    a value we refuse to show is not published: an absent or expired entry carries ``None`` for both.
    """

    value: float | None
    age_seconds: float | None
    stale: bool


@dataclass(frozen=True, slots=True)
class EnergyReadings:
    battery_soc_percent: DeviceReading
    grid_power_w: DeviceReading  # positive = import (business convention)
    pv_power_w: DeviceReading
    house_load_w: DeviceReading


ABSENT = DeviceReading(None, None, True)


def absent_readings() -> EnergyReadings:
    """Every figure absent — what a device without cached values reads as."""
    return EnergyReadings(ABSENT, ABSENT, ABSENT, ABSENT)


class EnergyReadingsPort(Protocol):
    def readings(self, device_id: str) -> EnergyReadings: ...
