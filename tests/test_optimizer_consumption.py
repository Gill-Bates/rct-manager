#!/usr/bin/env python3
#
# tests/test_optimizer_consumption.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Tests for the deterministic baseline consumption-forecast provider."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from app.optimizer.base import ConsumptionForecastProvider
from app.optimizer.consumption import BaselineConsumptionForecastProvider
from app.optimizer.models import ConsumptionForecast

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def test_provider_satisfies_the_consumption_protocol() -> None:
    provider: ConsumptionForecastProvider = BaselineConsumptionForecastProvider(400.0)
    assert isinstance(provider.forecast(NOW, timedelta(hours=1)), ConsumptionForecast)


def test_flat_baseline_integrates_average_load_over_each_slot() -> None:
    provider = BaselineConsumptionForecastProvider(400.0)
    forecast = provider.forecast(NOW, timedelta(hours=2))

    # Two hours of 15-minute slots.
    assert len(forecast.intervals) == 8
    for interval in forecast.intervals:
        assert (interval.end - interval.start) == timedelta(minutes=15)
        # 400 W over a quarter hour = 100 Wh.
        assert interval.energy_wh == pytest.approx(100.0)


def test_intervals_are_tz_aware_ordered_and_non_overlapping() -> None:
    forecast = BaselineConsumptionForecastProvider(500.0).forecast(NOW, timedelta(hours=3))

    assert forecast.created_at.tzinfo is not None
    for interval in forecast.intervals:
        assert interval.start.tzinfo is not None
        assert interval.end.tzinfo is not None
    for previous, current in pairwise(forecast.intervals):
        assert current.start >= previous.end


def test_grid_is_anchored_up_to_the_next_quarter_hour() -> None:
    unaligned = datetime(2026, 10, 12, 12, 7, 30, tzinfo=UTC)
    forecast = BaselineConsumptionForecastProvider(400.0).forecast(unaligned, timedelta(hours=1))

    assert forecast.intervals[0].start == datetime(2026, 10, 12, 12, 15, tzinfo=UTC)


def test_day_profile_averages_to_the_configured_load_over_a_full_day() -> None:
    provider = BaselineConsumptionForecastProvider(400.0, apply_day_profile=True, timezone="UTC")
    forecast = provider.forecast(NOW.replace(hour=0, minute=0), timedelta(hours=24))

    total_wh = sum(interval.energy_wh for interval in forecast.intervals)
    # A full flat day at 400 W is 400 W * 24 h = 9600 Wh; the normalized profile preserves it.
    assert total_wh == pytest.approx(9600.0, rel=1e-9)


def test_day_profile_shapes_slots_by_local_hour() -> None:
    flat = BaselineConsumptionForecastProvider(400.0).forecast(NOW.replace(hour=3), timedelta(hours=1))
    shaped = BaselineConsumptionForecastProvider(400.0, apply_day_profile=True, timezone="UTC").forecast(
        NOW.replace(hour=3), timedelta(hours=1)
    )
    # 03:00 local is a night-trough hour (<1.0 weight), so the shaped load is below the flat load.
    assert shaped.intervals[0].energy_wh < flat.intervals[0].energy_wh


def test_zero_horizon_is_rejected_and_negative_load_is_rejected() -> None:
    provider = BaselineConsumptionForecastProvider(400.0)
    with pytest.raises(ValueError):
        provider.forecast(NOW, timedelta())
    with pytest.raises(ValueError):
        BaselineConsumptionForecastProvider(-1.0)


def test_invalid_timezone_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        BaselineConsumptionForecastProvider(400.0, apply_day_profile=True, timezone="Not/AZone")
