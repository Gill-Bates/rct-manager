#!/usr/bin/env python3
#
# app/optimizer/consumption.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Household-consumption forecasts: measured history plus an optional deterministic baseline.

This module turns the already-received ``household_load_power`` samples (power in watts, with a
measurement instant) into fixed 15-minute energy buckets, and reads those buckets back into a
:class:`~app.optimizer.models.ConsumptionForecast`. It depends only on the pure optimizer models,
``app.clock`` and the standard library; it never imports the gateway, dispatch, or admin store, so
the pure planning boundary stays intact. The concrete persistence adapter that owns a store lives
in ``app.optimizer.adapters``.

Two deliberate rules protect data quality:

* Energy is integrated time-weighted between consecutive samples (``Wh = W x hours``), split across
  the 15-minute buckets a sample interval spans; a gap longer than ``MAX_INTEGRATION_GAP`` leaves
  the period uncovered rather than inventing energy across it. Missing samples are never read as
  ``0 W``.
* ``coverage_seconds`` (at most 900 per bucket) and ``sample_count`` record how well a bucket is
  backed, so the forecast can discard an under-covered bucket instead of trusting it.
"""

import itertools
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.optimizer.align import GRID_STEP_SECONDS, build_grid
from app.optimizer.models import (
    ConsumptionForecast,
    ConsumptionForecastInterval,
    _require_aware,
    _require_finite,
)

# The fixed bucket width; the whole energy domain works on 15-minute intervals.
BUCKET_SECONDS = 900
BUCKET_WIDTH = timedelta(seconds=BUCKET_SECONDS)
# Longest span between two consecutive samples that is still integrated. A longer gap is a data
# hole: the integration simply skips it, so a dropout is never smeared into fabricated energy.
MAX_INTEGRATION_GAP = timedelta(minutes=5)

# Forecast data-quality constants (all tested). A historical bucket must be covered this well to be
# trusted as an observation of its slot; a slot needs at least this many good buckets for the
# primary (same-weekday) estimate, and at least this many for the time-of-day fallback.
MIN_BUCKET_COVERAGE_SECONDS = 600.0  # 2/3 of a 15-minute bucket
MIN_PRIMARY_SAMPLES = 3  # same weekday + same slot: at least three recent weeks
MIN_FALLBACK_SAMPLES = 3  # same time of day across recent days
# How far back the two historical lookups reach. The primary looks at the same weekday over several
# weeks; the fallback at the same time of day over the most recent days.
PRIMARY_LOOKBACK_WEEKS = 8
FALLBACK_LOOKBACK_DAYS = 14
# The share of a planning horizon's quarter-hour slots that must be backed by an actual per-slot
# historical estimate (same-weekday or same-time-of-day), rather than the blunt whole-history
# median, before a Dry-Run plan may be produced. Below this the history is reported insufficient:
# a handful of well-covered buckets must not license filling the entire horizon from one median.
MIN_HORIZON_COVERAGE_RATIO = 0.5
# The conservative documented fallback when neither historical method has enough data. It is the
# median of every sufficiently-covered bucket in the whole history, scaled to one bucket. This is
# intentionally blunt: with almost no history the plan should lean on a plausible average rather
# than a confident per-slot number. ``None`` (no usable history at all) is reported as unavailable.


def floor_to_bucket(moment: datetime) -> datetime:
    """The UTC 15-minute bucket start that contains ``moment`` (converted to UTC first)."""
    moment = moment.astimezone(UTC)
    return moment.replace(
        minute=moment.minute - moment.minute % 15, second=0, microsecond=0
    )


@dataclass(frozen=True, slots=True)
class PowerSample:
    """One decoded house-load power reading: watts at an instant. Non-finite/negative is rejected
    by the aggregator, not here, so the caller can pass raw values straight through.
    """

    measured_at: datetime
    watts: float


@dataclass(slots=True)
class _BucketAccumulator:
    energy_wh: float = 0.0
    sample_count: int = 0
    coverage_seconds: float = 0.0


def _usable(watts: float) -> bool:
    """A house-load value that may enter the integration: finite and non-negative."""
    return (
        isinstance(watts, (int, float))
        and not isinstance(watts, bool)
        and math.isfinite(watts)
        and watts >= 0.0
    )


def aggregate_samples(
    samples: Iterable[PowerSample], *, carry: PowerSample | None = None
) -> dict[datetime, _BucketAccumulator]:
    """Integrate consecutive power samples into time-weighted 15-minute energy buckets.

    Energy between two samples is the trapezoidal area (average of the two powers x elapsed time),
    distributed to every bucket the interval covers in proportion to the time spent in each. A pair
    whose spacing exceeds :data:`MAX_INTEGRATION_GAP`, or that holds a non-finite or negative value,
    contributes no energy and no coverage; its endpoints still count as observed samples. The result
    maps each UTC bucket start to its accumulated energy, sample count and covered seconds.

    ``carry`` is the previous batch's last sample: it takes part in the integration so a segment
    straddling two batches is covered, but it is not counted again in ``sample_count``.
    """
    counted = sorted(samples, key=lambda sample: sample.measured_at)
    ordered = (
        counted
        if carry is None
        else sorted([carry, *counted], key=lambda sample: sample.measured_at)
    )
    buckets: dict[datetime, _BucketAccumulator] = defaultdict(_BucketAccumulator)
    for sample in counted:
        if _usable(sample.watts):
            buckets[floor_to_bucket(sample.measured_at)].sample_count += 1
    # Integrate over the ORIGINAL consecutive samples: a segment contributes energy and coverage
    # only when BOTH its endpoints are valid. An invalid sample between two valid ones breaks the
    # segment on each side, so no energy is invented across a known-bad measurement and that span is
    # left uncovered (the same way a gap longer than MAX_INTEGRATION_GAP is left uncovered).
    for previous, current in itertools.pairwise(ordered):
        if not _usable(previous.watts) or not _usable(current.watts):
            continue
        start = previous.measured_at.astimezone(UTC)
        end = current.measured_at.astimezone(UTC)
        span = end - start
        if span <= timedelta(0) or span > MAX_INTEGRATION_GAP:
            continue
        _integrate_pair(buckets, start, end, previous.watts, current.watts)
    return dict(buckets)


def _integrate_pair(
    buckets: dict[datetime, _BucketAccumulator],
    start: datetime,
    end: datetime,
    w_start: float,
    w_end: float,
) -> None:
    """Spread the trapezoidal energy of one sample pair across the buckets it spans."""
    total_seconds = (end - start).total_seconds()
    cursor = start
    while cursor < end:
        bucket_start = floor_to_bucket(cursor)
        bucket_end = bucket_start + BUCKET_WIDTH
        segment_end = min(end, bucket_end)
        segment_seconds = (segment_end - cursor).total_seconds()
        # Linear interpolation of the power at the two segment edges, so a boundary-crossing pair
        # keeps the trapezoidal area physically correct within each bucket.
        frac_start = (cursor - start).total_seconds() / total_seconds
        frac_end = (segment_end - start).total_seconds() / total_seconds
        w_seg_start = w_start + (w_end - w_start) * frac_start
        w_seg_end = w_start + (w_end - w_start) * frac_end
        average_w = (w_seg_start + w_seg_end) / 2.0
        buckets[bucket_start].energy_wh += average_w * (segment_seconds / 3600.0)
        buckets[bucket_start].coverage_seconds += segment_seconds
        cursor = segment_end


def buckets_to_rows(buckets: dict[datetime, _BucketAccumulator]) -> list[dict]:
    """Convert accumulated buckets into store rows, clamping coverage to the 900 s a bucket holds."""
    rows: list[dict] = []
    for bucket_start in sorted(buckets):
        accumulator = buckets[bucket_start]
        rows.append(
            {
                "interval_start": bucket_start.isoformat(),
                "interval_end": (bucket_start + BUCKET_WIDTH).isoformat(),
                "energy_wh": max(0.0, accumulator.energy_wh),
                "sample_count": accumulator.sample_count,
                "coverage_seconds": min(
                    float(BUCKET_SECONDS), accumulator.coverage_seconds
                ),
            }
        )
    return rows


# Data-quality classes of a stored bucket. A COMPLETE bucket was measured for (almost) its whole
# width; a PARTIAL one for most of it (its energy is only usable scaled to the full width and only for
# state detection); an ESTIMATED one contains energy spread across an app stop from a counter
# difference; MISSING is everything else. Only COMPLETE buckets are learning data.
COMPLETE_COVERAGE_SECONDS = 810.0  # 90 % of a bucket


class BucketQuality(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    ESTIMATED = "estimated"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class HistoryBucket:
    """One stored consumption bucket read back for the forecast.

    ``coverage_seconds`` is MEASURED seconds only; ``estimated_seconds`` is the part of the bucket
    whose energy was spread from a counter difference. ``energy_wh`` covers both parts.
    """

    start: datetime
    end: datetime
    energy_wh: float
    sample_count: int
    coverage_seconds: float
    estimated_seconds: float = 0.0

    @property
    def well_covered(self) -> bool:
        return self.coverage_seconds >= MIN_BUCKET_COVERAGE_SECONDS

    @property
    def quality(self) -> BucketQuality:
        if self.estimated_seconds > 0.0:
            if (
                self.coverage_seconds + self.estimated_seconds
                >= MIN_BUCKET_COVERAGE_SECONDS
            ):
                return BucketQuality.ESTIMATED
            return BucketQuality.MISSING
        if self.coverage_seconds >= COMPLETE_COVERAGE_SECONDS:
            return BucketQuality.COMPLETE
        if self.coverage_seconds >= MIN_BUCKET_COVERAGE_SECONDS:
            return BucketQuality.PARTIAL
        return BucketQuality.MISSING

    @property
    def normalized_wh(self) -> float:
        """Energy scaled to the full bucket width (exact for COMPLETE, an extrapolation otherwise)."""
        seconds = self.coverage_seconds + self.estimated_seconds
        if seconds <= 0.0:
            return 0.0
        return self.energy_wh * BUCKET_SECONDS / min(float(BUCKET_SECONDS), seconds)


# A PARTIAL bucket (measured 600..810 s) is extrapolated to the full width, so it is only trusted
# from 750 s (83 %) on: the extrapolation factor then stays below 1.2. This is the single shared
# threshold for what counts as a forecast-usable observation; the adaptive module re-exports it.
MIN_PARTIAL_COVERAGE_SECONDS = 750.0


def usable_value(bucket: HistoryBucket) -> float | None:
    """The bucket's full-width energy when it is a clean observation, else ``None``.

    This is the ONE shared "forecast-usable observation" rule for both forecasters. COMPLETE buckets
    count as measured; PARTIAL ones only from :data:`MIN_PARTIAL_COVERAGE_SECONDS` on and scaled to
    the full width. ESTIMATED, MISSING and thinly covered buckets are not observations and never
    enter training, the median or coverage.
    """
    quality = bucket.quality
    if quality is BucketQuality.COMPLETE:
        return bucket.energy_wh
    if (
        quality is BucketQuality.PARTIAL
        and bucket.coverage_seconds >= MIN_PARTIAL_COVERAGE_SECONDS
    ):
        return bucket.normalized_wh
    return None


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def rows_to_history(
    rows: Sequence[dict], meter: Sequence[dict] = ()
) -> list[HistoryBucket]:
    """Parse store rows into timezone-aware :class:`HistoryBucket` values, dropping unreadable ones.

    ``rows`` are the power-integrated buckets; ``meter`` are the counter-based ones. Per bucket the
    better MEASURED source wins (the counter when it covers at least as many seconds as the power
    integration; never both, so energy is not counted twice), and the counter's ESTIMATED energy
    is added on top and flagged through ``estimated_seconds``.
    """
    power: dict[datetime, tuple[datetime, float, int, float]] = {}
    for row in rows:
        try:
            start = _parse_instant(row["interval_start"])
            end = _parse_instant(row["interval_end"])
            energy_wh = float(row["energy_wh"])
            sample_count = int(row["sample_count"])
            coverage_seconds = float(row["coverage_seconds"])
        except (KeyError, ValueError, TypeError):
            continue
        if end <= start or not math.isfinite(energy_wh) or energy_wh < 0:
            continue
        power[start] = (end, energy_wh, sample_count, coverage_seconds)
    counted: dict[datetime, tuple[datetime, float, float, float, float]] = {}
    for row in meter:
        try:
            start = _parse_instant(row["interval_start"])
            end = _parse_instant(row["interval_end"])
            values = (
                float(row["measured_wh"]),
                float(row["measured_seconds"]),
                float(row["estimated_wh"]),
                float(row["estimated_seconds"]),
            )
        except (KeyError, ValueError, TypeError):
            continue
        if end <= start or not all(math.isfinite(v) and v >= 0 for v in values):
            continue
        counted[start] = (end, *values)
    history: list[HistoryBucket] = []
    for start in power.keys() | counted.keys():
        end_p, energy_p, samples, seconds_p = power.get(start, (None, 0.0, 0, 0.0))
        end_m, wh_m, seconds_m, est_wh, est_seconds = counted.get(
            start, (None, 0.0, 0.0, 0.0, 0.0)
        )
        end = end_p or end_m
        if end is None:
            continue
        if seconds_m >= seconds_p:
            measured_wh, measured_seconds = wh_m, seconds_m
        else:
            measured_wh, measured_seconds = energy_p, seconds_p
        # The other source may already cover part of what the estimate spread; keep only the remainder.
        room = max(0.0, BUCKET_SECONDS - measured_seconds)
        if est_seconds > room:
            est_wh, est_seconds = (
                est_wh * room / est_seconds if est_seconds else 0.0
            ), room
        history.append(
            HistoryBucket(
                start, end, measured_wh + est_wh, samples, measured_seconds, est_seconds
            )
        )
    history.sort(key=lambda bucket: bucket.start)
    return history


class HistoricalConsumptionForecaster:
    """Deterministic, median-based household-consumption forecast from stored history.

    No machine learning. For each 15-minute slot of the requested horizon it estimates energy in a
    fixed priority: (1) the median of well-covered buckets at the same local weekday and same
    time-of-day over recent weeks; (2) if too few, the median at the same time-of-day over the most
    recent days; (3) a conservative whole-history median. The median resists outliers such as a
    stove, wallbox or heating element skewing a single window. All timestamps are timezone-aware;
    the local weekday/time-of-day are taken in ``timezone`` so DST days with 92/96/100 slots are
    handled by construction (the slots are generated from the horizon, never assumed to be 96).
    """

    def __init__(self, timezone: str = "UTC") -> None:
        self._zone = ZoneInfo(timezone)

    def build(
        self, history: Sequence[HistoryBucket], now: datetime, horizon: timedelta
    ) -> ConsumptionForecast:
        now = now.astimezone(UTC)
        observations = self._observations(history)
        fallback_energy = self._whole_history_median(observations)
        by_week_slot, by_slot = self._index(observations, now)
        intervals: list[ConsumptionForecastInterval] = []
        start = floor_to_bucket(now)
        horizon_end = now + horizon
        while start < horizon_end:
            end = start + BUCKET_WIDTH
            energy = self._estimate(start, by_week_slot, by_slot, fallback_energy)
            if energy is not None:
                intervals.append(
                    ConsumptionForecastInterval(start, end, max(0.0, energy))
                )
            start = end
        return ConsumptionForecast(created_at=now, intervals=tuple(intervals))

    def horizon_coverage_ratio(
        self, history: Sequence[HistoryBucket], now: datetime, horizon: timedelta
    ) -> float:
        """Fraction of the horizon's slots backed by a genuine per-slot estimate (not the fallback).

        A slot counts as covered when the same-weekday index or the time-of-day index has at least
        the required number of samples for it. The blunt whole-history median does not count: it is
        the signal that a slot is *not* backed by representative history. The release decision uses
        this instead of a flat bucket count so a few well-covered buckets cannot license filling an
        entire ~30 h horizon from one median.
        """
        now = now.astimezone(UTC)
        observations = self._observations(history)
        by_week_slot, by_slot = self._index(observations, now)
        total = 0
        covered = 0
        start = floor_to_bucket(now)
        horizon_end = now + horizon
        while start < horizon_end:
            total += 1
            week_key, slot_key = self._local_keys(start)
            if (
                len(by_week_slot.get(week_key, [])) >= MIN_PRIMARY_SAMPLES
                or len(by_slot.get(slot_key, [])) >= MIN_FALLBACK_SAMPLES
            ):
                covered += 1
            start += BUCKET_WIDTH
        if total == 0:
            return 0.0
        return covered / total

    def coverage_end(
        self, history: Sequence[HistoryBucket], now: datetime, horizon: timedelta
    ) -> datetime:
        """How far from ``now`` the horizon's slots are contiguously backed by per-slot history.

        Walks the horizon's quarter-hour slots from ``now`` and stops at the first slot that is not
        backed by a genuine per-slot estimate (same-weekday or same-time-of-day), using the exact
        same test as :meth:`horizon_coverage_ratio`. The returned instant is the start of that first
        unbacked slot (or ``now + horizon`` if every slot is backed). It is the honest consumption
        reach for the common-horizon trim; the blunt whole-history median never extends it.
        """
        now = now.astimezone(UTC)
        observations = self._observations(history)
        by_week_slot, by_slot = self._index(observations, now)
        start = floor_to_bucket(now)
        horizon_end = now + horizon
        reached = now
        while start < horizon_end:
            week_key, slot_key = self._local_keys(start)
            if (
                len(by_week_slot.get(week_key, [])) >= MIN_PRIMARY_SAMPLES
                or len(by_slot.get(slot_key, [])) >= MIN_FALLBACK_SAMPLES
            ):
                reached = start + BUCKET_WIDTH
                start += BUCKET_WIDTH
            else:
                break
        return reached

    def _local_keys(
        self, start: datetime
    ) -> tuple[tuple[int, int, int], tuple[int, int]]:
        """The ``(weekday, hour, minute)`` and ``(hour, minute)`` local keys of a bucket start."""
        local = start.astimezone(self._zone)
        return (local.weekday(), local.hour, local.minute), (local.hour, local.minute)

    @staticmethod
    def _observations(
        history: Sequence[HistoryBucket],
    ) -> list[tuple[HistoryBucket, float]]:
        """The forecast-usable observations: each kept bucket paired with its full-width energy.

        Uses the single shared :func:`usable_value` rule, so ESTIMATED and MISSING buckets (and the
        estimated energy they carry) never enter training, the median or coverage.
        """
        observations: list[tuple[HistoryBucket, float]] = []
        for bucket in history:
            value = usable_value(bucket)
            if value is not None:
                observations.append((bucket, value))
        return observations

    def _index(
        self, observations: Sequence[tuple[HistoryBucket, float]], now: datetime
    ) -> tuple[
        dict[tuple[int, int, int], list[float]], dict[tuple[int, int], list[float]]
    ]:
        """Build the two historical indices, each limited to its documented lookback window.

        The same-weekday (primary) index only sees buckets within the last
        :data:`PRIMARY_LOOKBACK_WEEKS` weeks; the time-of-day (fallback) index only the last
        :data:`FALLBACK_LOOKBACK_DAYS` days. Without these windows every stored bucket would feed
        both indices, so old data could satisfy a 'recent weeks' estimate it does not represent.
        """
        primary_cutoff = now - timedelta(weeks=PRIMARY_LOOKBACK_WEEKS)
        fallback_cutoff = now - timedelta(days=FALLBACK_LOOKBACK_DAYS)
        by_week_slot: dict[tuple[int, int, int], list[float]] = defaultdict(list)
        by_slot: dict[tuple[int, int], list[float]] = defaultdict(list)
        for bucket, value in observations:
            week_key, slot_key = self._local_keys(bucket.start)
            if bucket.start >= primary_cutoff:
                by_week_slot[week_key].append(value)
            if bucket.start >= fallback_cutoff:
                by_slot[slot_key].append(value)
        return by_week_slot, by_slot

    def _estimate(
        self,
        start: datetime,
        by_week_slot: dict[tuple[int, int, int], list[float]],
        by_slot: dict[tuple[int, int], list[float]],
        fallback_energy: float | None,
    ) -> float | None:
        week_key, slot_key = self._local_keys(start)
        primary = by_week_slot.get(week_key, [])
        if len(primary) >= MIN_PRIMARY_SAMPLES:
            return statistics.median(primary)
        secondary = by_slot.get(slot_key, [])
        if len(secondary) >= MIN_FALLBACK_SAMPLES:
            return statistics.median(secondary)
        return fallback_energy

    @staticmethod
    def _whole_history_median(
        observations: Sequence[tuple[HistoryBucket, float]],
    ) -> float | None:
        energies = [value for _bucket, value in observations]
        if not energies:
            return None
        return statistics.median(energies)


_DAY_PROFILE: tuple[float, ...] = (
    0.6, 0.5, 0.5, 0.5, 0.5, 0.6,  # 00:00-05:59
    0.9, 1.2, 1.3, 1.1, 1.0, 1.0,  # 06:00-11:59
    1.1, 1.0, 0.9, 0.9, 1.1, 1.4,  # 12:00-17:59
    1.7, 1.9, 1.5, 1.2, 0.9, 0.7,  # 18:00-23:59
)  # fmt: skip

# The weights must average to exactly 1.0 so applying the profile preserves the configured average
# load over a full day (sum == 24.0).
assert len(_DAY_PROFILE) == 24  # module-load invariant on the fixed profile
assert abs(sum(_DAY_PROFILE) - 24.0) < 1e-9  # the profile must average to 1.0


class BaselineConsumptionForecastProvider:
    """A pure baseline-profile :class:`ConsumptionForecastProvider`.

    ``average_load_w`` is the configured constant average household load (``baseline_household_load_w``
    from Settings). When ``apply_day_profile`` is ``True`` the constant is shaped by ``_DAY_PROFILE``
    keyed on the slot's wall-clock hour in ``timezone`` (an IANA zone, UTC when ``None``); otherwise
    every slot carries the flat average. The provider is deterministic and performs no I/O.
    """

    def __init__(
        self,
        average_load_w: float,
        *,
        apply_day_profile: bool = False,
        timezone: str | None = None,
    ) -> None:
        _require_finite(average_load_w, "average_load_w", non_negative=True)
        self._average_load_w = float(average_load_w)
        self._apply_day_profile = apply_day_profile
        # Resolve the zone eagerly so an invalid IANA name fails at construction, not per forecast.
        self._zone = _resolve_zone(timezone)

    def forecast(self, now: datetime, horizon: timedelta) -> ConsumptionForecast:
        """Return a baseline consumption forecast over ``[align_up(now), now + horizon]``.

        The forecast is created at ``now`` (its ``created_at``) and carries one 15-minute interval
        per grid slot, each with ``energy_wh`` equal to the (optionally profile-shaped) average load
        integrated over the slot.
        """
        _require_aware(now, "now")
        if not isinstance(horizon, timedelta) or horizon <= timedelta():
            raise ValueError("horizon must be a positive timedelta")
        grid = build_grid(now, horizon)
        slot_hours = GRID_STEP_SECONDS / 3600.0
        intervals = tuple(
            ConsumptionForecastInterval(
                start=slot_start,
                end=slot_end,
                energy_wh=self._load_w_for(slot_start) * slot_hours,
            )
            for slot_start, slot_end in grid
        )
        return ConsumptionForecast(created_at=now.astimezone(UTC), intervals=intervals)

    def _load_w_for(self, slot_start: datetime) -> float:
        if not self._apply_day_profile:
            return self._average_load_w
        local_hour = slot_start.astimezone(self._zone).hour
        return self._average_load_w * _DAY_PROFILE[local_hour]


def _resolve_zone(timezone: str | None):
    if timezone is None:
        return UTC
    # Imported lazily so the module stays stdlib-only and the import cost is paid once per provider.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone must be an IANA zone") from exc
