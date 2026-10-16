#!/usr/bin/env python3
#
# tests/test_probe_consumption_history.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Unit tests for the read-only consumption-history probe tool (no hardware, no socket).

These cover the pure analysis helpers, the read-only guard, and the backwards-pagination loop
driven by a fake gateway. Nothing here opens a socket or needs a device.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.protocol.timeseries import TimeSeriesPoint
from app.protocol.types import Command
from tools.probe_consumption_history import (
    DAY_LOAD_LOG_METRIC,
    ReadOnlyViolation,
    _probe_metric,
    _ReadOnlyGuard,
    deltas,
    duplicate_timestamps,
    infer_grid,
)

BASE = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)


def _pt(stamp: datetime, value: float) -> TimeSeriesPoint:
    return TimeSeriesPoint(stamp, value)


# --- pure helpers ------------------------------------------------------------------------------


def test_deltas_and_grid_for_daily_points() -> None:
    points = [_pt(BASE - timedelta(days=i), 10000.0 + i) for i in range(4)]
    assert all(delta == timedelta(days=1) for delta in deltas(points))
    assert "daily" in infer_grid(points)


def test_grid_for_minute_points() -> None:
    points = [_pt(BASE - timedelta(minutes=15 * i), float(i)) for i in range(5)]
    assert "min spacing" in infer_grid(points)


def test_duplicate_timestamps_are_detected_across_the_set() -> None:
    stamp = BASE - timedelta(days=1)
    points = [_pt(stamp, 1.0), _pt(stamp, 2.0), _pt(BASE - timedelta(days=2), 3.0)]
    assert duplicate_timestamps(points) == [stamp]


def test_infer_grid_without_two_timestamps() -> None:
    assert "unknown" in infer_grid([_pt(BASE, 1.0)])


# --- read-only guard ---------------------------------------------------------------------------


def test_guard_accepts_only_the_allowed_history_write() -> None:
    guard = _ReadOnlyGuard(frozenset({0xCA6D6472}))
    guard.inspect(Command.WRITE, 0xCA6D6472)
    assert guard.frames_sent == 1


def test_guard_rejects_a_read_or_a_foreign_object() -> None:
    guard = _ReadOnlyGuard(frozenset({0xCA6D6472}))
    with pytest.raises(ReadOnlyViolation):
        guard.inspect(Command.READ, 0xCA6D6472)
    with pytest.raises(ReadOnlyViolation):
        guard.inspect(
            Command.WRITE, 0xDEADBEEF
        )  # e.g. logger_log_rate must never be touched


# --- pagination loop with a fake gateway -------------------------------------------------------


class _FakeClock:
    """now() ticks forward; sleep() is a no-op so the test never waits the real pause."""

    def __init__(self) -> None:
        self._now = BASE

    def now(self) -> datetime:
        return self._now

    async def sleep(self, _seconds: float) -> None:
        return None


class _FakeGateway:
    """Returns one daily point per page walking backwards, then an empty page."""

    def __init__(self, days: int) -> None:
        self._days = days
        self.queries: list[datetime] = []

    async def query_history_series(self, _device_id, _name, before):
        self.queries.append(before)
        cutoff = before.astimezone(UTC).date()
        older = [BASE - timedelta(days=i) for i in range(1, self._days + 1)]
        older = [stamp for stamp in older if stamp.date() < cutoff]
        if not older:
            return ()
        newest = max(older)
        return (_pt(newest, 12000.0),)  # one point per page


def test_probe_walks_backwards_and_records_oldest_newest() -> None:
    gateway = _FakeGateway(days=5)
    report = asyncio.run(
        _probe_metric(gateway, DAY_LOAD_LOG_METRIC, pages=3, clock=_FakeClock())
    )
    unique = report.unique_by_timestamp()
    assert len(unique) == 3  # three pages, one point each
    assert max(unique) == BASE - timedelta(days=1)
    assert min(unique) == BASE - timedelta(days=3)
    # The before= cursor moved strictly backwards on every page.
    assert gateway.queries == sorted(gateway.queries, reverse=True)
    assert len(set(gateway.queries)) == len(gateway.queries)


def test_probe_marks_before_as_exhausted_when_a_page_is_empty() -> None:
    gateway = _FakeGateway(days=2)
    report = asyncio.run(
        _probe_metric(gateway, DAY_LOAD_LOG_METRIC, pages=5, clock=_FakeClock())
    )
    assert report.before_empty_after_oldest is True
