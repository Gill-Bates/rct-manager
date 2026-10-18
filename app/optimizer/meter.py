#!/usr/bin/env python3
#
# app/optimizer/meter.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Meter-based household-consumption capture: counter deltas attributed to 15-minute buckets.

The inverter's cumulative load counter (``energy_e_load_total``, Wh) keeps counting while this
application is stopped, so the difference between two readings is the consumption of that period
even across a stop. Integrating power samples alone loses that energy. This module turns counter
readings into per-bucket energy with an explicit quality:

* a pair of readings at most :data:`MAX_INTEGRATION_GAP` apart is a MEASURED span;
* a longer pair (an app stop, a polling outage) is spread uniformly over the buckets it spans and
  kept as an ESTIMATE with its own energy and seconds. It is never merged into measured coverage, so
  the forecast can refuse to learn from it or to detect absence/return from it.

Everything implausible is discarded instead of repaired: a decreasing counter (reset, replaced
meter), an implied mean power above :data:`MAX_PLAUSIBLE_POWER_W`, a stalled counter over a gap, and
gaps longer than :data:`MAX_ESTIMATE_GAP`. When the total counter is unusable for a pair, the day
counter delta is the fallback; when both are usable but disagree, the total wins and the mismatch is
counted. Counter semantics that need real hardware to confirm (day rollover instant and zone, wrap
behaviour) are listed as unverified in ``docs/reference/hardware-verification.md``; counter capture is
described in ``docs/energy/consumption.md`` ("Counter capture"). None is assumed here
beyond "non-decreasing between resets".
"""

import itertools
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.optimizer.consumption import BUCKET_WIDTH, MAX_INTEGRATION_GAP, floor_to_bucket

log = logging.getLogger(__name__)

TOTAL_COUNTER = "energy_e_load_total"
DAY_COUNTER = "energy_e_load_day"
COUNTER_METRICS = (TOTAL_COUNTER, DAY_COUNTER)

# A household connection tops out well below this (3 x 63 A is about 43 kW); a larger implied mean
# power means a counter jump (replacement, firmware reset), not consumption.
MAX_PLAUSIBLE_POWER_W = 50_000.0
# The longest stop whose counter difference is still spread as an estimate. Beyond a day the spread
# says nothing about the quarter-hour shape, so the energy is left unattributed rather than smeared.
MAX_ESTIMATE_GAP = timedelta(hours=24)
# Day-counter cross-check: the two deltas of one span may differ by this much (quantisation of a
# 32-bit float counter at several MWh is 1 to 4 Wh) before the disagreement is counted.
CROSS_CHECK_ABS_WH = 20.0
CROSS_CHECK_REL = 0.25
# A day reading counts for a pair when it lies this close to the pair's total readings.
DAY_MATCH_TOLERANCE = timedelta(seconds=90)


@dataclass(slots=True)
class MeterAccumulator:
    measured_wh: float = 0.0
    measured_seconds: float = 0.0
    estimated_wh: float = 0.0
    estimated_seconds: float = 0.0


@dataclass(slots=True)
class MeterStats:
    """What the attribution discarded or flagged; surfaced in logs, never silently dropped."""

    measured_pairs: int = 0
    estimated_pairs: int = 0
    discarded_negative: int = 0
    discarded_implausible: int = 0
    discarded_stalled: int = 0
    discarded_long_gap: int = 0
    day_fallbacks: int = 0
    cross_check_mismatches: int = 0
    unattributed_wh: float = 0.0

    def merge(self, other: "MeterStats") -> None:
        for name in self.__slots__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass(slots=True)
class MeterAttribution:
    buckets: dict[datetime, MeterAccumulator] = field(default_factory=dict)
    stats: MeterStats = field(default_factory=MeterStats)


Reading = tuple[datetime, float]


def _usable(value: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0.0
    )


def _day_delta(day: Sequence[Reading], start: datetime, end: datetime) -> float | None:
    """The day-counter increase across ``start``..``end``, or ``None`` when it cannot be trusted.

    Needs a reading near each end; a drop (midnight rollover) or a non-finite value gives ``None``.
    """

    def near(moment: datetime) -> float | None:
        best: Reading | None = None
        for reading in day:
            if abs(reading[0] - moment) <= DAY_MATCH_TOLERANCE and (
                best is None or abs(reading[0] - moment) < abs(best[0] - moment)
            ):
                best = reading
        return None if best is None or not _usable(best[1]) else best[1]

    first, last = near(start), near(end)
    if first is None or last is None or last < first:
        return None
    return last - first


def _spread(
    buckets: dict[datetime, MeterAccumulator],
    start: datetime,
    end: datetime,
    energy_wh: float,
    *,
    estimated: bool,
) -> None:
    """Spread ``energy_wh`` of the span ``start``..``end`` over the buckets it covers, by time."""
    total = (end - start).total_seconds()
    cursor = start
    while cursor < end:
        bucket_start = floor_to_bucket(cursor)
        segment_end = min(end, bucket_start + BUCKET_WIDTH)
        seconds = (segment_end - cursor).total_seconds()
        share = energy_wh * seconds / total
        accumulator = buckets.setdefault(bucket_start, MeterAccumulator())
        if estimated:
            accumulator.estimated_wh += share
            accumulator.estimated_seconds += seconds
        else:
            accumulator.measured_wh += share
            accumulator.measured_seconds += seconds
        cursor = segment_end


def attribute_counter(
    total: Sequence[Reading],
    day: Sequence[Reading] = (),
    *,
    anchor: Reading | None = None,
) -> MeterAttribution:
    """Attribute consecutive total-counter readings (after ``anchor``) to 15-minute buckets.

    ``anchor`` is the last reading of the previous batch, so a span across batches (or across an
    app stop, when it was restored from the store) is attributed exactly once. Pairs are judged
    independently; a rejected pair never poisons the next one.
    """
    result = MeterAttribution()
    ordered = sorted(
        ((t.astimezone(UTC), v) for t, v in total), key=lambda reading: reading[0]
    )
    if anchor is not None:
        ordered.insert(0, (anchor[0].astimezone(UTC), anchor[1]))
    day_sorted = sorted(
        ((t.astimezone(UTC), v) for t, v in day), key=lambda reading: reading[0]
    )
    stats = result.stats
    for (t0, v0), (t1, v1) in itertools.pairwise(ordered):
        seconds = (t1 - t0).total_seconds()
        if seconds <= 0:
            continue
        gap = t1 - t0 > MAX_INTEGRATION_GAP
        if gap and t1 - t0 > MAX_ESTIMATE_GAP:
            stats.discarded_long_gap += 1
            if _usable(v0) and _usable(v1) and v1 > v0:
                stats.unattributed_wh += v1 - v0
            continue
        delta = _total_delta(v0, v1, seconds, gap, stats)
        day_delta = _day_delta(day_sorted, t0, t1) if day_sorted else None
        if delta is None:
            # Total counter unusable for this pair: the day counter, if it is plausible, stands in.
            if (
                day_delta is not None
                and _plausible(day_delta, seconds)
                and (day_delta > 0 or not gap)
            ):
                delta = day_delta
                stats.day_fallbacks += 1
            else:
                continue
        elif day_delta is not None and abs(day_delta - delta) > max(
            CROSS_CHECK_ABS_WH, CROSS_CHECK_REL * delta
        ):
            stats.cross_check_mismatches += 1
            # The total counter wins (below); this records the raw day-vs-total divergence of the one
            # offending span so a human can see at DEBUG whether the day counter lags, quantises
            # coarser, or runs on different semantics. Off by default; it adds no persistent state.
            log.debug(
                "Load counter cross-check mismatch %s..%s: total %.3f->%.3f delta=%.3f Wh, "
                "day_delta=%.3f Wh, |day_delta-delta|=%.3f Wh > threshold %.3f Wh (total retained)",
                t0.isoformat(),
                t1.isoformat(),
                v0,
                v1,
                delta,
                day_delta,
                abs(day_delta - delta),
                max(CROSS_CHECK_ABS_WH, CROSS_CHECK_REL * delta),
            )
        _spread(result.buckets, t0, t1, delta, estimated=gap)
        if gap:
            stats.estimated_pairs += 1
        else:
            stats.measured_pairs += 1
    return result


def _plausible(delta_wh: float, seconds: float) -> bool:
    return delta_wh * 3600.0 / seconds <= MAX_PLAUSIBLE_POWER_W


def _total_delta(
    v0: float, v1: float, seconds: float, gap: bool, stats: MeterStats
) -> float | None:
    if not _usable(v0) or not _usable(v1) or v1 < v0:
        stats.discarded_negative += (
            1  # a decreasing counter is a reset or a replaced meter
        )
        return None
    delta = v1 - v0
    if not _plausible(delta, seconds):
        stats.discarded_implausible += 1
        return None
    if gap and delta <= 0.0:
        # A household never draws 0 Wh over minutes; a flat counter across a gap is a stalled one.
        stats.discarded_stalled += 1
        return None
    return delta


def meter_rows(buckets: dict[datetime, MeterAccumulator]) -> list[dict]:
    """Store rows for the meter table; seconds are clamped to the 900 s a bucket holds."""
    rows: list[dict] = []
    for start in sorted(buckets):
        accumulator = buckets[start]
        rows.append(
            {
                "interval_start": start.isoformat(),
                "interval_end": (start + BUCKET_WIDTH).isoformat(),
                "measured_wh": max(0.0, accumulator.measured_wh),
                "measured_seconds": min(900.0, accumulator.measured_seconds),
                "estimated_wh": max(0.0, accumulator.estimated_wh),
                "estimated_seconds": min(900.0, accumulator.estimated_seconds),
            }
        )
    return rows
