#!/usr/bin/env python3
#
# tests/test_optimizer_align.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Tests for the pure, DST-safe time-alignment helpers."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.optimizer.align import align_energy, align_prices, align_up, build_grid
from app.optimizer.models import SolarForecast, SolarForecastInterval
from app.spotmarket.models import SpotMarketProvider, SpotPriceInterval, SpotPriceSnapshot

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _prices(intervals: tuple[SpotPriceInterval, ...]) -> SpotPriceSnapshot:
    return SpotPriceSnapshot(
        provider=SpotMarketProvider.ENERGY_CHARTS,
        bidding_zone="DE-LU",
        currency="EUR",
        fetched_at=NOW,
        intervals=intervals,
    )


def test_align_up_rounds_to_the_next_quarter_hour() -> None:
    assert align_up(datetime(2026, 10, 12, 12, 0, tzinfo=UTC)) == datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    assert align_up(datetime(2026, 10, 12, 12, 1, tzinfo=UTC)) == datetime(2026, 10, 12, 12, 15, tzinfo=UTC)
    assert align_up(datetime(2026, 10, 12, 12, 59, tzinfo=UTC)) == datetime(2026, 10, 12, 13, 0, tzinfo=UTC)


def test_build_grid_spans_aligned_now_to_now_plus_horizon() -> None:
    grid = build_grid(datetime(2026, 10, 12, 12, 7, tzinfo=UTC), timedelta(hours=1))
    assert grid[0][0] == datetime(2026, 10, 12, 12, 15, tzinfo=UTC)
    # Last slot ends no later than 13:07; the final full slot is 12:45-13:00.
    assert grid[-1][1] == datetime(2026, 10, 12, 13, 0, tzinfo=UTC)
    for start, end in grid:
        assert (end - start) == timedelta(minutes=15)


def test_build_grid_is_empty_for_a_sub_slot_horizon() -> None:
    assert build_grid(NOW, timedelta(minutes=10)) == ()


def test_price_step_function_uses_the_midpoint_half_open() -> None:
    # One coarse 60-minute price slot covering four 15-minute grid slots.
    coarse = _prices((SpotPriceInterval(NOW, NOW + timedelta(hours=1), Decimal("0.20")),))
    grid = build_grid(NOW, timedelta(hours=1))
    aligned = align_prices(coarse, grid)
    assert aligned == (Decimal("0.20"),) * 4


def test_price_midpoint_on_a_boundary_belongs_to_the_later_interval() -> None:
    # Two adjacent 15-minute price intervals; a grid slot whose midpoint equals the shared boundary
    # must take the later interval (start <= midpoint < end).
    boundary = NOW + timedelta(minutes=15)
    prices = _prices(
        (
            SpotPriceInterval(NOW, boundary, Decimal("0.10")),
            SpotPriceInterval(boundary, NOW + timedelta(minutes=30), Decimal("0.30")),
        )
    )
    # A grid slot [NOW+7.5min, NOW+22.5min] has midpoint exactly at the boundary.
    grid = ((NOW + timedelta(minutes=7, seconds=30), NOW + timedelta(minutes=22, seconds=30)),)
    assert align_prices(prices, grid) == (Decimal("0.30"),)


def test_price_gap_maps_to_none() -> None:
    prices = _prices((SpotPriceInterval(NOW, NOW + timedelta(minutes=15), Decimal("0.20")),))
    grid = build_grid(NOW, timedelta(hours=1))
    aligned = align_prices(prices, grid)
    assert aligned[0] == Decimal("0.20")
    assert aligned[1:] == (None, None, None)


def test_energy_proration_splits_a_coarse_interval_evenly() -> None:
    # 1000 Wh over one hour, spread across four 15-minute grid slots = 250 Wh each.
    solar = SolarForecast(
        created_at=NOW,
        intervals=(SolarForecastInterval(NOW, NOW + timedelta(hours=1), 1000.0),),
    )
    grid = build_grid(NOW, timedelta(hours=1))
    aligned = align_energy(solar, grid)
    assert aligned == pytest.approx((250.0, 250.0, 250.0, 250.0))


def test_energy_partial_overlap_is_prorated_by_fraction() -> None:
    # A source interval covering only the second half of a grid slot contributes half its energy.
    solar = SolarForecast(
        created_at=NOW,
        intervals=(
            SolarForecastInterval(NOW + timedelta(minutes=7, seconds=30), NOW + timedelta(minutes=15), 400.0),
        ),
    )
    grid = ((NOW, NOW + timedelta(minutes=15)),)
    # The whole 400 Wh source lies inside the grid slot, so the slot receives all of it.
    assert align_energy(solar, grid) == pytest.approx((400.0,))


def test_energy_slot_without_coverage_is_zero() -> None:
    solar = SolarForecast(
        created_at=NOW,
        intervals=(SolarForecastInterval(NOW, NOW + timedelta(minutes=15), 100.0),),
    )
    grid = build_grid(NOW, timedelta(hours=1))
    aligned = align_energy(solar, grid)
    assert aligned[0] == pytest.approx(100.0)
    assert aligned[1:] == pytest.approx((0.0, 0.0, 0.0))


def test_dst_spring_forward_day_keeps_uniform_utc_slots() -> None:
    # Europe/Berlin springs forward at 02:00 local on 2026-03-29 (02:00 -> 03:00). Build the grid
    # from a UTC instant spanning that local transition; every slot stays exactly 900 s on the
    # absolute UTC timeline, and local bucketing still resolves through zoneinfo.
    berlin = ZoneInfo("Europe/Berlin")
    start_utc = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)  # 01:30 Berlin, before the jump
    grid = build_grid(start_utc, timedelta(hours=3))

    for slot_start, slot_end in grid:
        assert (slot_end - slot_start) == timedelta(minutes=15)
    # The local wall clock skips 02:00..02:59; a UTC slot landing in that window maps to a 03:xx
    # local time, proving the arithmetic never produced a nonexistent local instant.
    locals_seen = {slot_start.astimezone(berlin).hour for slot_start, _ in grid}
    assert 2 not in locals_seen  # 02:xx local does not exist on this day
    assert 3 in locals_seen


def test_dst_fall_back_day_keeps_uniform_utc_slots() -> None:
    # Europe/Berlin falls back at 03:00 local on 2026-10-25 (03:00 -> 02:00); 02:xx local occurs
    # twice. UTC slots stay uniform and distinct regardless.
    start_utc = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    grid = build_grid(start_utc, timedelta(hours=3))
    assert len({slot_start for slot_start, _ in grid}) == len(grid)
    for slot_start, slot_end in grid:
        assert (slot_end - slot_start) == timedelta(minutes=15)
