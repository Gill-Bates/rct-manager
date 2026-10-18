#!/usr/bin/env python3
#
# tests/test_consumption_forecast.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The deterministic, median-based historical consumption forecaster and its data-quality rules."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.clock import is_aware
from app.optimizer.consumption import (
    MIN_BUCKET_COVERAGE_SECONDS,
    MIN_PRIMARY_SAMPLES,
    HistoricalConsumptionForecaster,
    HistoryBucket,
)

BUCKET = timedelta(minutes=15)


def _bucket(
    start: datetime, energy_wh: float, coverage: float = 900.0
) -> HistoryBucket:
    return HistoryBucket(
        start, start + BUCKET, energy_wh, sample_count=10, coverage_seconds=coverage
    )


def test_same_weekday_slot_is_preferred_and_uses_the_median() -> None:
    # Five Thursdays at 10:00 UTC; the 5000 Wh spike must not drag the estimate up (median wins).
    history = []
    for week in range(5):
        start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC) + timedelta(
            weeks=week
        )  # 2026-01-01 is Thursday
        history.append(_bucket(start, [200.0, 210.0, 5000.0, 220.0, 215.0][week]))
    forecaster = HistoricalConsumptionForecaster("UTC")
    now = datetime(2026, 2, 5, 9, 59, tzinfo=UTC)  # Thursday, floors to 09:45
    forecast = forecaster.build(history, now, timedelta(minutes=20))
    slot = next(
        iv for iv in forecast.intervals if iv.start.hour == 10 and iv.start.minute == 0
    )
    assert slot.energy_wh == pytest.approx(
        215.0
    )  # median of the five, not the 5000 outlier


def test_falls_back_to_same_time_of_day_when_the_weekday_history_is_thin() -> None:
    # Only two Thursdays (below MIN_PRIMARY_SAMPLES) but several non-Thursday days at the same time,
    # all inside the 14-day fallback window relative to ``now`` so the time-of-day path is exercised
    # (not the whole-history fallback). Older data would be dropped by the lookback window.
    now = datetime(2026, 1, 15, 9, 59, tzinfo=UTC)  # Thursday
    history = []
    for day_offset in (7, 14):  # the two preceding Thursdays
        history.append(
            _bucket(
                datetime(2026, 1, 15, 10, 0, tzinfo=UTC) - timedelta(days=day_offset),
                999.0,
            )
        )
    for day in range(5):
        start = datetime(2026, 1, 9, 10, 0, tzinfo=UTC) + timedelta(
            days=day
        )  # Fri..Tue, no Thursday
        history.append(_bucket(start, 300.0))
    forecaster = HistoricalConsumptionForecaster("UTC")
    forecast = forecaster.build(history, now, timedelta(minutes=16))
    slot = next(
        iv for iv in forecast.intervals if iv.start.hour == 10 and iv.start.minute == 0
    )
    # Two Thursdays is below the primary threshold, so the same-time-of-day median governs. That
    # slot pool is {999, 999, 300x5}; its median is 300.
    assert slot.energy_wh == pytest.approx(300.0)


def _mixed_bucket(
    start: datetime, energy_wh: float, *, measured: float, estimated: float
) -> HistoryBucket:
    """A bucket with measured AND estimated coverage; ``energy_wh`` covers both parts."""
    return HistoryBucket(
        start,
        start + BUCKET,
        energy_wh,
        sample_count=10,
        coverage_seconds=measured,
        estimated_seconds=estimated,
    )


def test_estimated_buckets_do_not_enter_the_median_even_when_well_measured() -> None:
    # Item 2: a bucket with measured >= MIN_BUCKET_COVERAGE_SECONDS AND estimated_seconds > 0 is
    # quality ESTIMATED and its energy includes estimated Wh. The single shared usable_value rule
    # must exclude it from the median/index just as the adaptive replay does, so the spike never
    # inflates the estimate.
    from app.optimizer.consumption import MIN_PARTIAL_COVERAGE_SECONDS

    now = datetime(2026, 2, 5, 9, 59, tzinfo=UTC)  # Thursday, floors to 09:45
    history = []
    # Three same-weekday 10:00 observations that are clean (COMPLETE), all 200 Wh.
    for week in range(1, 4):
        history.append(
            _bucket(
                datetime(2026, 2, 5, 10, 0, tzinfo=UTC) - timedelta(weeks=week), 200.0
            )
        )
    # Two mixed buckets at the same slot: well measured but carrying estimated energy (ESTIMATED).
    for week in range(4, 6):
        history.append(
            _mixed_bucket(
                datetime(2026, 2, 5, 10, 0, tzinfo=UTC) - timedelta(weeks=week),
                9000.0,
                measured=MIN_PARTIAL_COVERAGE_SECONDS,
                estimated=100.0,
            )
        )
    forecaster = HistoricalConsumptionForecaster("UTC")
    forecast = forecaster.build(history, now, timedelta(minutes=20))
    slot = next(
        iv for iv in forecast.intervals if iv.start.hour == 10 and iv.start.minute == 0
    )
    # Only the three clean observations count: the median is 200, not dragged up by the 9000 spikes.
    assert slot.energy_wh == pytest.approx(200.0)


def test_estimated_buckets_do_not_count_toward_horizon_coverage() -> None:
    # Item 2: ESTIMATED buckets must not make a slot count as per-slot-backed. Five same-time-of-day
    # buckets that are all ESTIMATED leave the 10:00 slot uncovered.
    from app.optimizer.consumption import MIN_PARTIAL_COVERAGE_SECONDS

    now = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
    history = [
        _mixed_bucket(
            now - timedelta(days=day),
            300.0,
            measured=MIN_PARTIAL_COVERAGE_SECONDS,
            estimated=50.0,
        )
        for day in range(1, 6)
    ]
    forecaster = HistoricalConsumptionForecaster("UTC")
    # Not a single slot is backed by a real observation, so the 15-min horizon coverage is 0.
    assert forecaster.horizon_coverage_ratio(
        history, now, timedelta(minutes=15)
    ) == pytest.approx(0.0)
    assert forecaster.coverage_end(history, now, timedelta(minutes=15)) == now


def test_complete_and_sufficient_partial_buckets_still_contribute() -> None:
    # Item 2: a COMPLETE bucket still counts at full energy; a PARTIAL bucket with coverage at or
    # above MIN_PARTIAL_COVERAGE_SECONDS still contributes its normalized (full-width) energy.
    from app.optimizer.consumption import MIN_PARTIAL_COVERAGE_SECONDS

    now = datetime(2026, 2, 5, 9, 59, tzinfo=UTC)
    # One COMPLETE (900 s, 300 Wh) plus two PARTIAL buckets measured 750 s carrying 250 Wh each;
    # 250 Wh over 750 s normalizes to 300 Wh for the full 900 s width, so all three agree at 300.
    history = [
        _bucket(
            datetime(2026, 2, 5, 10, 0, tzinfo=UTC) - timedelta(weeks=1),
            300.0,
            coverage=900.0,
        )
    ]
    for week in range(2, 4):
        history.append(
            HistoryBucket(
                datetime(2026, 2, 5, 10, 0, tzinfo=UTC) - timedelta(weeks=week),
                datetime(2026, 2, 5, 10, 0, tzinfo=UTC)
                - timedelta(weeks=week)
                + BUCKET,
                250.0,
                sample_count=10,
                coverage_seconds=MIN_PARTIAL_COVERAGE_SECONDS,
            )
        )
    forecaster = HistoricalConsumptionForecaster("UTC")
    forecast = forecaster.build(history, now, timedelta(minutes=20))
    slot = next(
        iv for iv in forecast.intervals if iv.start.hour == 10 and iv.start.minute == 0
    )
    # Three observations {300, 300, 300}; the PARTIAL ones contribute normalized energy, not raw.
    assert slot.energy_wh == pytest.approx(300.0)


def test_buckets_with_poor_coverage_are_discarded() -> None:
    poor = MIN_BUCKET_COVERAGE_SECONDS - 1.0
    history = [
        _bucket(
            datetime(2026, 1, 1, 10, 0, tzinfo=UTC) + timedelta(weeks=week),
            5000.0,
            coverage=poor,
        )
        for week in range(MIN_PRIMARY_SAMPLES + 2)
    ]
    history.append(
        _bucket(datetime(2026, 1, 8, 11, 0, tzinfo=UTC), 100.0)
    )  # the only well-covered bucket
    forecaster = HistoricalConsumptionForecaster("UTC")
    now = datetime(2026, 2, 5, 9, 59, tzinfo=UTC)
    forecast = forecaster.build(history, now, timedelta(minutes=16))
    # The poorly-covered 10:00 buckets are not trusted; the whole-history fallback uses the one
    # well-covered bucket (100 Wh), never the discarded 5000 Wh spikes.
    assert all(iv.energy_wh == pytest.approx(100.0) for iv in forecast.intervals)


def test_no_history_yields_no_intervals() -> None:
    forecaster = HistoricalConsumptionForecaster("UTC")
    forecast = forecaster.build(
        [], datetime(2026, 2, 5, 10, 0, tzinfo=UTC), timedelta(hours=2)
    )
    assert forecast.intervals == ()


def test_results_are_sorted_non_overlapping_and_timezone_aware() -> None:
    history = [
        _bucket(datetime(2026, 1, 1, hour, 0, tzinfo=UTC), 100.0)
        for hour in range(8, 20)
    ]
    forecaster = HistoricalConsumptionForecaster("UTC")
    forecast = forecaster.build(
        history, datetime(2026, 2, 5, 10, 0, tzinfo=UTC), timedelta(hours=3)
    )
    starts = [iv.start for iv in forecast.intervals]
    assert starts == sorted(starts)
    assert all(
        forecast.intervals[i].end <= forecast.intervals[i + 1].start
        for i in range(len(forecast.intervals) - 1)
    )
    assert all(is_aware(iv.start) and is_aware(iv.end) for iv in forecast.intervals)


@pytest.mark.parametrize(
    ("local_midnight", "expected_slots"),
    [
        (
            datetime(2026, 3, 29, 0, 0, tzinfo=ZoneInfo("Europe/Berlin")),
            92,
        ),  # spring forward: 23 h day
        (
            datetime(2026, 10, 25, 0, 0, tzinfo=ZoneInfo("Europe/Berlin")),
            100,
        ),  # fall back: 25 h day
        (
            datetime(2026, 6, 1, 0, 0, tzinfo=ZoneInfo("Europe/Berlin")),
            96,
        ),  # ordinary day
    ],
)
def test_dst_days_do_not_assume_96_slots(
    local_midnight: datetime, expected_slots: int
) -> None:
    # A dense same-time-of-day history for every slot, so every slot of the local day is estimated.
    history = []
    for offset_days in range(1, 8):
        day_start = (local_midnight - timedelta(days=offset_days)).astimezone(UTC)
        cursor = day_start
        for _ in range(110):  # cover a full local day in any season
            history.append(_bucket(cursor, 100.0))
            cursor += BUCKET
    forecaster = HistoricalConsumptionForecaster("Europe/Berlin")
    now = local_midnight.astimezone(UTC)
    # A horizon of exactly the local calendar day (23/24/25 h) must yield that many 15-min slots.
    next_midnight = (local_midnight + timedelta(days=1)).astimezone(UTC)
    horizon = next_midnight - now
    forecast = forecaster.build(history, now, horizon)
    assert len(forecast.intervals) == expected_slots


# --- lookback windows and horizon-coverage release (review finding 6) --------------------------


def test_primary_index_ignores_buckets_older_than_the_lookback_window() -> None:
    # Four same-weekday 10:00 buckets older than PRIMARY_LOOKBACK_WEEKS (all 50 Wh) must NOT satisfy
    # the primary estimate; a single recent same-time-of-day pool (300 Wh) must not be overridden by
    # that stale data. Without the window, the stale buckets would form a confident 50 Wh primary.
    from app.optimizer.consumption import FALLBACK_LOOKBACK_DAYS, PRIMARY_LOOKBACK_WEEKS

    now = datetime(2026, 3, 5, 9, 59, tzinfo=UTC)  # Thursday
    history = []
    for week in range(1, 5):  # four Thursdays, each beyond the 8-week primary window
        start = now.replace(hour=10, minute=0) - timedelta(
            weeks=PRIMARY_LOOKBACK_WEEKS + week
        )
        history.append(_bucket(start, 50.0))
    # A recent same-time-of-day pool inside the fallback window.
    for day in range(1, 4):
        history.append(
            _bucket(now.replace(hour=10, minute=0) - timedelta(days=day), 300.0)
        )
    assert all(d <= FALLBACK_LOOKBACK_DAYS for d in (1, 2, 3))
    forecaster = HistoricalConsumptionForecaster("UTC")
    forecast = forecaster.build(history, now, timedelta(minutes=16))
    slot = next(
        iv for iv in forecast.intervals if iv.start.hour == 10 and iv.start.minute == 0
    )
    # The stale primary buckets are excluded, so the recent fallback median (300) governs, not 50.
    assert slot.energy_wh == pytest.approx(300.0)


def test_fallback_index_ignores_buckets_older_than_the_lookback_window() -> None:
    # Same-time-of-day buckets older than FALLBACK_LOOKBACK_DAYS must not feed the fallback index.
    # With no recent per-slot data the estimate drops to the blunt whole-history median instead.
    from app.optimizer.consumption import FALLBACK_LOOKBACK_DAYS

    now = datetime(2026, 3, 5, 9, 59, tzinfo=UTC)
    history = [
        _bucket(
            now.replace(hour=10, minute=0)
            - timedelta(days=FALLBACK_LOOKBACK_DAYS + 1 + day),
            300.0,
        )
        for day in range(3)
    ]
    # Several well-covered buckets at a different time of day dominate the whole-history median (777)
    # so the fallback result is clearly distinct from the stale 10:00 pool's 300.
    for day in range(1, 6):
        history.append(
            _bucket(now.replace(hour=3, minute=0) - timedelta(days=day), 777.0)
        )
    forecaster = HistoricalConsumptionForecaster("UTC")
    forecast = forecaster.build(history, now, timedelta(minutes=16))
    slot = next(
        iv for iv in forecast.intervals if iv.start.hour == 10 and iv.start.minute == 0
    )
    # The stale 10:00 pool is outside the fallback window, so the per-slot path yields nothing and
    # the blunt whole-history median (777, dominated by the recent 03:00 pool) is used, not 300.
    assert slot.energy_wh == pytest.approx(777.0)


def test_horizon_coverage_ratio_counts_only_slots_backed_by_real_history() -> None:
    # A dense same-time-of-day pool for one slot only backs that single quarter hour of the horizon,
    # so a multi-slot horizon is only partially covered; the ratio must reflect that, not report 1.0
    # off a handful of buckets.
    now = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
    history = [
        _bucket(now - timedelta(days=day), 300.0)
        for day in range(1, 6)  # five recent 10:00 buckets
    ]
    forecaster = HistoricalConsumptionForecaster("UTC")
    # Horizon of 4 slots (10:00..11:00); only the 10:00 slot is backed by the pool.
    ratio = forecaster.horizon_coverage_ratio(history, now, timedelta(hours=1))
    assert ratio == pytest.approx(0.25)
    # A horizon of just the backed slot is fully covered.
    assert forecaster.horizon_coverage_ratio(
        history, now, timedelta(minutes=15)
    ) == pytest.approx(1.0)


# --- the real coverage_end and the coverage-ratio-based trim path (P2-1, P2-2, P3-1, P3-2) ------


def _dense_time_of_day_history(
    anchor: datetime,
    span_hours: float,
    *,
    days: int = 5,
    skip: set[datetime] | None = None,
    zone: str = "UTC",
) -> list[HistoryBucket]:
    """Well-covered same-time-of-day buckets so every slot of ``[anchor, anchor+span)`` is backed.

    For each quarter-hour slot in the window, ``days`` recent days carry a bucket at that same local
    time of day, which clears MIN_FALLBACK_SAMPLES and makes the slot count as per-slot-backed. A
    slot start listed in ``skip`` gets no history, leaving exactly that quarter hour unbacked.
    """
    skip = skip or set()
    zinfo = ZoneInfo(zone)
    history: list[HistoryBucket] = []
    slot_count = int(span_hours * 4)
    for i in range(slot_count):
        slot_start = anchor + BUCKET * i
        if slot_start in skip:
            continue
        local = slot_start.astimezone(zinfo)
        for day in range(1, days + 1):
            # Shift by whole local days so the time-of-day key matches across DST boundaries.
            past_local = local - timedelta(days=day)
            history.append(_bucket(past_local.astimezone(UTC), 250.0))
    return history


def test_coverage_end_stops_at_the_first_unbacked_slot() -> None:
    # Every slot of the first 2 h is backed except the one at +1 h; the honest per-slot reach ends
    # there. (coverage_end is retained for direct inspection even though the adapter trim now leans
    # on horizon_coverage_ratio for consumption.)
    now = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)  # on a boundary
    gap = now + timedelta(hours=1)
    history = _dense_time_of_day_history(now, 2.0, skip={gap})
    forecaster = HistoricalConsumptionForecaster("UTC")
    assert forecaster.coverage_end(history, now, timedelta(hours=2)) == gap


def test_coverage_ratio_tolerates_a_single_early_gap_via_the_fallback() -> None:
    # P2-1: a 6 h horizon fully backed except for one quarter hour early in it (hour 1) must stay
    # far above the 50 % fallback ratio, so the forecaster's whole-history median fills that one slot
    # instead of the plan being refused for want of 100 % contiguous per-slot history.
    now = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
    gap = now + timedelta(hours=1)
    history = _dense_time_of_day_history(now, 6.0, skip={gap})
    forecaster = HistoricalConsumptionForecaster("UTC")
    ratio = forecaster.horizon_coverage_ratio(history, now, timedelta(hours=6))
    assert ratio > 0.5
    # 23 of 24 slots are per-slot-backed.
    assert ratio == pytest.approx(23 / 24)
    # And the uncovered slot is still produced (filled by the conservative whole-history median),
    # never silently dropped or zero-invented.
    forecast = forecaster.build(history, now, timedelta(hours=6))
    gap_slot = next(iv for iv in forecast.intervals if iv.start == gap)
    assert gap_slot.energy_wh == pytest.approx(250.0)


def test_coverage_anchoring_ignores_the_unused_partial_slot() -> None:
    # P2-2: now is mid-slot (12:07); the planner starts at 12:15, so the 12:00 slot is never used.
    # When only 12:15 onward is backed, a ratio/coverage check anchored at the next boundary sees a
    # fully-backed horizon, whereas one anchored at now would be dragged down by the unused 12:00.
    from app.optimizer.planner import planning_start

    now = datetime(2026, 3, 5, 12, 7, tzinfo=UTC)
    start = planning_start(now)
    assert start == datetime(2026, 3, 5, 12, 15, tzinfo=UTC)
    span = timedelta(hours=6)
    history = _dense_time_of_day_history(
        start, 6.0
    )  # backed only from 12:15 on, not 12:00
    forecaster = HistoricalConsumptionForecaster("UTC")
    # Anchored at the planner's start boundary: the whole plannable span is backed.
    assert forecaster.horizon_coverage_ratio(history, start, span) == pytest.approx(1.0)
    assert forecaster.coverage_end(history, start, span) == start + span
    # Anchored at now the leading unused 12:00 slot is unbacked and would stop the reach immediately.
    assert forecaster.coverage_end(history, now, span) < start


def test_coverage_end_handles_a_dst_transition_day() -> None:
    # P3-2: a fall-back DST day (25 h) must still have every plannable slot backed and the reach walk
    # the real local quarter hours without assuming 96 slots. Anchor at local midnight before the
    # 02:00->03:00 fall-back; a 6 h span spans the repeated hour.
    zone = "Europe/Berlin"
    local_midnight = datetime(2026, 10, 25, 0, 0, tzinfo=ZoneInfo(zone))
    now = local_midnight.astimezone(UTC)
    span = timedelta(hours=6)
    history = _dense_time_of_day_history(now, 6.0, zone=zone)
    forecaster = HistoricalConsumptionForecaster(zone)
    assert forecaster.horizon_coverage_ratio(history, now, span) == pytest.approx(1.0)
    assert forecaster.coverage_end(history, now, span) == now + span
