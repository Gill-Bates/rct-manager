#!/usr/bin/env python3
#
# app/optimizer/daily.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Resolved daily household consumption: one answer per local calendar day.

A day is a local calendar date of the site, not a UTC day, so a DST day is 23 or 25 hours long and
still exactly one date. Priority per completed day: (1) a sufficiently complete local 15-minute
measurement; (2) otherwise the inverter's own day total, if one is stored; (3) otherwise unavailable.
The sources are alternatives and are never summed. A day total carries no time profile
(``profile_available`` is false) and is not part of the forecast's learning data: nothing here
produces a :class:`HistoryBucket`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum

from app.optimizer.consumption import HistoryBucket

# Share of the local day that must be covered by measured seconds to call the day locally measured.
MIN_MEASURED_DAY_COVERAGE = 0.9
SOURCE_INVERTER_HISTORY = "inverter_history"


class DailySource(StrEnum):
    LOCAL_MEASURED = "local_measured"
    INVERTER_HISTORY = SOURCE_INVERTER_HISTORY
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DailyConsumption:
    date: date
    energy_wh: float | None
    source: DailySource
    profile_available: bool  # False: the day quantity is known, its distribution over the day is not
    measured_coverage: float  # share of the local day covered by locally measured seconds, 0..1


def local_day_bounds(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """UTC start and end of a local calendar day (23, 24 or 25 hours)."""
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return start, end


def _measured_by_day(
    history: Sequence[HistoryBucket], zone: tzinfo
) -> dict[date, tuple[float, float]]:
    """Local date -> (energy_wh, measured_seconds) summed over the buckets that start in that day."""
    totals: dict[date, tuple[float, float]] = {}
    for bucket in history:
        day = bucket.start.astimezone(zone).date()
        energy, seconds = totals.get(day, (0.0, 0.0))
        totals[day] = (energy + bucket.energy_wh, seconds + bucket.coverage_seconds)
    return totals


def resolve_daily_consumption(
    first: date,
    last: date,
    *,
    today: date,
    zone: tzinfo,
    history: Sequence[HistoryBucket],
    inverter_days: Mapping[date, float],
) -> list[DailyConsumption]:
    """Resolve ``first``..``last`` inclusive. The running local day and later days are unavailable.

    ``history`` holds the local 15-minute buckets (power and meter merged, estimated parts included
    in their energy), ``inverter_days`` the stored inverter day totals in Wh.
    """
    measured = _measured_by_day(history, zone)
    result: list[DailyConsumption] = []
    day = first
    while day <= last:
        result.append(_resolve_day(day, today, zone, measured, inverter_days))
        day += timedelta(days=1)
    return result


def _resolve_day(
    day: date,
    today: date,
    zone: tzinfo,
    measured: Mapping[date, tuple[float, float]],
    inverter_days: Mapping[date, float],
) -> DailyConsumption:
    if day >= today:  # a running day is no completed day yet; it is not reconciled from any source
        return DailyConsumption(day, None, DailySource.UNAVAILABLE, False, 0.0)
    start, end = local_day_bounds(day, zone)
    energy, seconds = measured.get(day, (0.0, 0.0))
    coverage = min(1.0, seconds / (end - start).total_seconds())
    if coverage >= MIN_MEASURED_DAY_COVERAGE:
        return DailyConsumption(day, energy, DailySource.LOCAL_MEASURED, True, coverage)
    inverter = inverter_days.get(day)
    if inverter is not None:
        return DailyConsumption(day, inverter, DailySource.INVERTER_HISTORY, False, coverage)
    return DailyConsumption(day, None, DailySource.UNAVAILABLE, False, coverage)
