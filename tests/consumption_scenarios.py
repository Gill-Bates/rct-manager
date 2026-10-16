#!/usr/bin/env python3
#
# tests/consumption_scenarios.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Synthetic household histories for the adaptive consumption forecast scenarios.

Not a test module. ``Household`` generates the *truth* (what the meter would have measured) per
quarter hour from a mode function; a forecast made at time T may only see ``history_until(T)``, so
observation and forecast data stay separated in time.
"""

import math
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.optimizer.consumption import BUCKET_WIDTH, HistoryBucket

# A mode is (kind, factor): "normal" x factor, "low" standby, "outage" (no measurement) or
# "estimated" (an app stop whose energy was spread from a counter difference).
Mode = tuple[str, float]
NORMAL: Mode = ("normal", 1.0)
LOW: Mode = ("low", 1.0)
OUTAGE: Mode = ("outage", 1.0)


def everyday_wh(local: datetime) -> float:
    """The noise-free everyday energy of one quarter hour (about 9 kWh per day, peaks morning/evening)."""
    hour = local.hour + local.minute / 60.0
    weekend = local.weekday() >= 5
    base = 26.0 + 8.0 * math.sin((hour - 6.0) / 24.0 * 2.0 * math.pi)
    morning = (8.0 if not weekend else 5.0) * 4.0
    evening = 6.0 * 5.0 if not weekend else 7.0 * 5.0
    if (7.0 if not weekend else 9.0) <= hour < (9.0 if not weekend else 11.0):
        base += morning * 4.0
    if 17.5 <= hour < 22.0:
        base += evening * 4.0
    if local.weekday() == 5 and 12.0 <= hour < 14.0:  # Saturday cooking
        base += 70.0
    return base


class Household:
    def __init__(self, start: datetime, mode: Callable[[datetime], Mode], *, seed: int = 7,
                 timezone: str = "Europe/Berlin", spikes: dict[datetime, float] | None = None) -> None:
        self.zone = ZoneInfo(timezone)
        self.start = start.astimezone(UTC)
        self.mode = mode
        self._rng = random.Random(seed)  # noqa: S311 - deterministic test data
        self._spikes = spikes or {}
        self._buckets: list[HistoryBucket] = []
        self._truth: dict[datetime, float] = {}
        self._generated_until = self.start

    def _bucket(self, start: datetime) -> HistoryBucket | None:
        local = start.astimezone(self.zone)
        kind, factor = self.mode(local)
        noise = self._rng.gauss(1.0, 0.12)
        random_load = 0.0
        if self._rng.random() < 0.03:  # a dishwasher, oven or kettle now and then
            random_load = self._rng.uniform(150.0, 600.0)
        random_load += self._spikes.get(start, 0.0)
        if kind == "low":
            energy = max(0.0, 12.0 * self._rng.gauss(1.0, 0.15)) + self._spikes.get(start, 0.0)
        else:
            energy = max(0.0, everyday_wh(local) * factor * noise) + random_load
        self._truth[start] = energy
        end = start + BUCKET_WIDTH
        if kind == "outage":
            return None
        if kind == "estimated":
            return HistoryBucket(start, end, energy, 0, 0.0, estimated_seconds=900.0)
        return HistoryBucket(start, end, energy, 30, 900.0)

    def generate_until(self, until: datetime) -> None:
        until = until.astimezone(UTC)
        while self._generated_until + BUCKET_WIDTH <= until:
            bucket = self._bucket(self._generated_until)
            if bucket is not None:
                self._buckets.append(bucket)
            self._generated_until += BUCKET_WIDTH

    def history_until(self, moment: datetime) -> list[HistoryBucket]:
        """Only buckets that had ended by ``moment`` (what a forecast made then can know)."""
        self.generate_until(moment)
        return [b for b in self._buckets if b.end <= moment.astimezone(UTC)]

    def truth_day_wh(self, local_day) -> float:
        """The true energy of one local calendar day (generated on demand)."""
        midnight = datetime.combine(local_day, datetime.min.time(), tzinfo=self.zone).astimezone(UTC)
        next_midnight = datetime.combine(local_day + timedelta(days=1), datetime.min.time(), tzinfo=self.zone).astimezone(UTC)
        self.generate_until(next_midnight)
        return sum(v for k, v in self._truth.items() if midnight <= k < next_midnight)

    def local(self, year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
        return datetime(year, month, day, hour, minute, tzinfo=self.zone).astimezone(UTC)
