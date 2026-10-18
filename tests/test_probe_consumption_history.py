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
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.protocol.timeseries import TimeSeriesPoint
from app.protocol.types import Command
from tools.probe_consumption_history import (
    DAY_LOAD_LOG_METRIC,
    ReadOnlyViolation,
    _ab_before_datetimes,
    _ab_conclusion,
    _boundary_instant,
    _point_present,
    _probe_before_ab,
    _probe_days_around,
    _probe_metric,
    _ReadOnlyGuard,
    _wallclock_local_date,
    _wire_int,
    deltas,
    duplicate_timestamps,
    infer_grid,
)

BASE = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
BERLIN = ZoneInfo("Europe/Berlin")


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


# --- before= A/B wire-integer construction -----------------------------------------------------


def test_ab_before_datetimes_produce_the_documented_wire_integers() -> None:
    before_a, before_b = _ab_before_datetimes(date(2026, 10, 16), BERLIN)
    # A = true instant of Berlin local midnight (October = UTC+2) -> 2026-10-15T22:00:00Z.
    assert _wire_int(before_a) == 1792101600
    # B = the same wall-clock numbers relabelled as UTC -> 2026-10-16T00:00:00Z.
    assert _wire_int(before_b) == 1792108800
    # They differ by exactly the October Berlin offset: 2 h = 7200 s.
    assert _wire_int(before_b) - _wire_int(before_a) == 7200


def test_boundary_instant_is_the_prior_day_last_second() -> None:
    assert _boundary_instant(date(2026, 10, 16)) == datetime(
        2026, 10, 15, 23, 59, 59, tzinfo=UTC
    )


# --- A/B presence + conclusion logic -----------------------------------------------------------


class _AbFakeGateway:
    """Returns the 2026-10-15T23:59:59Z boundary point only when the wire int >= B's integer.

    This mimics the hypothesised device: query A (true-UTC instant, earlier wire int) drops the
    boundary point, query B (wall-clock instant, 2 h later) includes it.
    """

    def __init__(self, *, cutoff_wire_int: int) -> None:
        self._cutoff = cutoff_wire_int
        self.wire_ints: list[int] = []
        self.inspected: list[tuple[Command, int]] = []

    async def query_history_series(self, _device_id, _name, before):
        wire = _wire_int(before)
        self.wire_ints.append(wire)
        boundary = _boundary_instant(date(2026, 10, 16))
        if wire >= self._cutoff:
            return (_pt(boundary, 18018.7),)
        return (_pt(boundary - timedelta(days=1), 20856.4),)


def test_probe_before_ab_flags_present_only_in_b_as_write_side_fix() -> None:
    gateway = _AbFakeGateway(cutoff_wire_int=1792108800)  # B's wire int
    report = asyncio.run(
        _probe_before_ab(
            gateway, DAY_LOAD_LOG_METRIC, date(2026, 10, 16), BERLIN, _FakeClock()
        )
    )
    assert report.branch_a.present is False
    assert report.branch_b.present is True
    assert report.conclusion == (
        "QUERY timestamp must be normalized to device wall-clock (write-side fix needed)"
    )
    # Exactly two read-only queries were issued, A before B.
    assert gateway.wire_ints == [1792101600, 1792108800]


def test_ab_conclusion_covers_all_three_branches() -> None:
    assert _ab_conclusion(present_a=False, present_b=True) == (
        "QUERY timestamp must be normalized to device wall-clock (write-side fix needed)"
    )
    assert _ab_conclusion(present_a=True, present_b=True) == (
        "query-side normalization not required for this boundary"
    )
    assert (
        _ab_conclusion(present_a=True, present_b=False)
        == "inconclusive / need another boundary"
    )
    assert (
        _ab_conclusion(present_a=False, present_b=False)
        == "inconclusive / need another boundary"
    )


def test_point_present_matches_exact_instant() -> None:
    boundary = _boundary_instant(date(2026, 10, 16))
    assert _point_present([_pt(boundary, 1.0)], boundary) is True
    assert (
        _point_present([_pt(boundary - timedelta(seconds=1), 1.0)], boundary) is False
    )


# --- three-day re-dating helper ----------------------------------------------------------------


def test_wallclock_local_date_shifts_a_2359_utc_stamp_to_the_next_berlin_day() -> None:
    # 2026-10-15T23:59:59Z, read as Berlin wall clock, is 2026-10-15 locally; but under the
    # wall-clock hypothesis the device meant 2026-10-15 23:59:59 LOCAL, which the decoder mislabels
    # as UTC. Re-dating treats the stamp numbers as local -> stays 2026-10-15. The off-by-one shows
    # up because production instead does stamp.astimezone(Berlin) -> 2026-10-16 01:59:59.
    stamp = datetime(2026, 10, 15, 23, 59, 59, tzinfo=UTC)
    assert _wallclock_local_date(stamp, BERLIN) == date(2026, 10, 15)
    # Production's (buggy) astimezone view rolls the same stamp onto the next calendar day:
    assert stamp.astimezone(BERLIN).date() == date(2026, 10, 16)


class _DaysFakeGateway:
    """Returns three day points stamped ...T23:59:59Z for 2026-10-14/15/16."""

    def __init__(self) -> None:
        self.wire_ints: list[int] = []

    async def query_history_series(self, _device_id, _name, before):
        self.wire_ints.append(_wire_int(before))
        return (
            _pt(datetime(2026, 10, 14, 23, 59, 59, tzinfo=UTC), 20856.4),
            _pt(datetime(2026, 10, 15, 23, 59, 59, tzinfo=UTC), 18018.7),
            _pt(datetime(2026, 10, 16, 23, 59, 59, tzinfo=UTC), 17989.4),
        )


def test_probe_days_around_lines_up_the_three_target_dates() -> None:
    gateway = _DaysFakeGateway()
    report = asyncio.run(
        _probe_days_around(
            gateway, DAY_LOAD_LOG_METRIC, date(2026, 10, 15), BERLIN, _FakeClock()
        )
    )
    assert report.dates == [date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]
    assert set(report.decoded) == {
        date(2026, 10, 14),
        date(2026, 10, 15),
        date(2026, 10, 16),
    }
    stamp, value = report.decoded[date(2026, 10, 15)]
    assert stamp == datetime(2026, 10, 15, 23, 59, 59, tzinfo=UTC)
    assert value == 18018.7
    # A single read-only query covered all three days.
    assert len(gateway.wire_ints) == 1


# --- read-only guard holds for the new A/B path ------------------------------------------------


class _GuardedAbFakeGateway(_AbFakeGateway):
    """A/B fake that routes every query through a read-only guard, like _GuardedGateway does."""

    def __init__(
        self, guard: _ReadOnlyGuard, object_id: int, *, cutoff_wire_int: int
    ) -> None:
        super().__init__(cutoff_wire_int=cutoff_wire_int)
        self._guard = guard
        self._object_id = object_id

    async def query_history_series(self, device_id, name, before):
        self._guard.inspect(Command.WRITE, self._object_id)
        self.inspected.append((Command.WRITE, self._object_id))
        return await super().query_history_series(device_id, name, before)


def test_before_ab_emits_only_the_day_metric_history_write() -> None:
    object_id = 0xCA6D6472  # logger_day_eload_log_ts
    guard = _ReadOnlyGuard(frozenset({object_id}))
    gateway = _GuardedAbFakeGateway(guard, object_id, cutoff_wire_int=1792108800)
    asyncio.run(
        _probe_before_ab(
            gateway, DAY_LOAD_LOG_METRIC, date(2026, 10, 16), BERLIN, _FakeClock()
        )
    )
    # Only two WRITE frames, both for the allowed day-metric object id; nothing else touched.
    assert guard.frames_sent == 2
    assert gateway.inspected == [(Command.WRITE, object_id), (Command.WRITE, object_id)]
