#!/usr/bin/env python3
#
# tests/test_consumption_history.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Local household-consumption historization: SQLite store, aggregation, and the sample sink."""

import asyncio
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.admin.consumption import ConsumptionHistorian, ConsumptionSampleSink
from app.admin.store import AdminStore
from app.optimizer.consumption import PowerSample, aggregate_samples, floor_to_bucket

SECRET = "x" * 32
BASE = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> AdminStore:
    store = AdminStore(tmp_path / "rct.db", SECRET)
    store.initialize()
    return store


# --- store -------------------------------------------------------------------------------------


def test_store_upserts_accumulate_and_read_back_per_device(tmp_path: Path) -> None:
    store = _store(tmp_path)
    bucket = {
        "interval_start": BASE.isoformat(),
        "interval_end": (BASE + timedelta(minutes=15)).isoformat(),
        "energy_wh": 100.0,
        "sample_count": 5,
        "coverage_seconds": 300.0,
    }
    store.upsert_consumption_intervals("main", [bucket])
    store.upsert_consumption_intervals("main", [{**bucket, "energy_wh": 50.0, "sample_count": 2, "coverage_seconds": 300.0}])
    store.upsert_consumption_intervals("slave1", [bucket])

    main = store.get_consumption_intervals("main")
    assert len(main) == 1
    assert main[0]["energy_wh"] == pytest.approx(150.0)
    assert main[0]["sample_count"] == 7
    assert main[0]["coverage_seconds"] == pytest.approx(600.0)
    # Devices are separated.
    assert len(store.get_consumption_intervals("slave1")) == 1


def test_store_clamps_accumulated_coverage_to_900(tmp_path: Path) -> None:
    store = _store(tmp_path)
    bucket = {
        "interval_start": BASE.isoformat(),
        "interval_end": (BASE + timedelta(minutes=15)).isoformat(),
        "energy_wh": 1.0,
        "sample_count": 1,
        "coverage_seconds": 800.0,
    }
    store.upsert_consumption_intervals("main", [bucket])
    store.upsert_consumption_intervals("main", [bucket])  # 1600 -> clamped to 900
    assert store.get_consumption_intervals("main")[0]["coverage_seconds"] == pytest.approx(900.0)


def test_retention_prune_drops_old_buckets_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = BASE - timedelta(days=90)
    recent = BASE
    for start in (old, recent):
        store.upsert_consumption_intervals("main", [{
            "interval_start": start.isoformat(),
            "interval_end": (start + timedelta(minutes=15)).isoformat(),
            "energy_wh": 10.0, "sample_count": 1, "coverage_seconds": 900.0,
        }])
    store.prune_consumption_intervals((BASE - timedelta(days=60)).isoformat())
    remaining = store.get_consumption_intervals("main")
    assert [row["interval_start"] for row in remaining] == [recent.isoformat()]


def test_prune_does_not_touch_price_or_solar_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.replace_spot_market_intervals([{
        "config_key": "k", "local_date": "2026-01-15",
        "interval_start": BASE.isoformat(), "interval_end": (BASE + timedelta(minutes=15)).isoformat(),
        "price_eur_per_kwh": 0.2, "timezone": "UTC", "fetched_at": BASE.isoformat(),
    }])
    store.replace_solar_forecast_intervals([{
        "config_key": "k", "local_date": "2026-01-15",
        "interval_start": BASE.isoformat(), "interval_end": (BASE + timedelta(minutes=15)).isoformat(),
        "energy_wh": 5.0, "timezone": "UTC", "fetched_at": BASE.isoformat(),
    }])
    store.prune_consumption_intervals((BASE + timedelta(days=1)).isoformat())
    assert store.get_spot_market_intervals("k")
    assert store.get_solar_forecast_intervals("k", "2026-01-15", "2026-01-15")


def test_history_survives_a_store_reopen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_consumption_intervals("main", [{
        "interval_start": BASE.isoformat(),
        "interval_end": (BASE + timedelta(minutes=15)).isoformat(),
        "energy_wh": 42.0, "sample_count": 3, "coverage_seconds": 900.0,
    }])
    store.close()
    reopened = AdminStore(tmp_path / "rct.db", SECRET)
    reopened.initialize()
    rows = reopened.get_consumption_intervals("main")
    assert rows and rows[0]["energy_wh"] == pytest.approx(42.0)


# --- sample sink -------------------------------------------------------------------------------


def test_sink_keeps_only_household_load_power() -> None:
    sink = ConsumptionSampleSink()
    sink.sample("main", "household_load_power", 500.0, BASE)
    sink.sample("main", "battery_soc", 80.0, BASE)  # ignored
    sink.sample("main", "grid_power", 100.0, BASE)  # ignored
    grouped = sink.drain()
    assert set(grouped) == {"main"}
    assert len(grouped["main"]) == 1


def test_sink_turns_negative_and_non_finite_values_into_gap_markers() -> None:
    sink = ConsumptionSampleSink()
    sink.sample("main", "household_load_power", -5.0, BASE)
    sink.sample("main", "household_load_power", float("nan"), BASE)
    sink.sample("main", "household_load_power", float("inf"), BASE)
    sink.sample("main", "household_load_power", True, BASE)  # bool is not a power value
    samples = sink.drain()["main"]
    assert len(samples) == 4
    assert all(not math.isfinite(sample.watts) for sample in samples)


def test_sink_is_bounded_and_drops_oldest_on_overflow() -> None:
    sink = ConsumptionSampleSink(max_buffered=3)
    for minute in range(5):
        sink.sample("main", "household_load_power", float(minute), BASE + timedelta(minutes=minute))
    grouped = sink.drain()
    # Only the three newest survive; the oldest two were dropped fail-safe.
    assert [sample.watts for sample in grouped["main"]] == [2.0, 3.0, 4.0]
    assert sink.dropped == 2


def test_sink_separates_devices() -> None:
    sink = ConsumptionSampleSink()
    sink.sample("main", "household_load_power", 100.0, BASE)
    sink.sample("slave1", "household_load_power", 200.0, BASE)
    grouped = sink.drain()
    assert set(grouped) == {"main", "slave1"}


# --- historian drain ---------------------------------------------------------------------------


def test_historian_drain_writes_buckets_and_carries_the_last_sample(tmp_path: Path) -> None:
    from tests.conftest import ManualClock

    store = _store(tmp_path)
    clock = ManualClock()
    historian = ConsumptionHistorian(store, clock)
    # First drain batch: four samples one minute apart, constant 1200 W.
    for minute in range(4):
        historian.sink.sample("main", "household_load_power", 1200.0, BASE + timedelta(minutes=minute))
    asyncio.run(historian.drain_once())
    # Second batch continues one minute later; the carryover bridges the gap so energy is continuous.
    for minute in range(4, 8):
        historian.sink.sample("main", "household_load_power", 1200.0, BASE + timedelta(minutes=minute))
    asyncio.run(historian.drain_once())

    rows = store.get_consumption_intervals("main")
    assert rows
    # 1200 W over 7 minutes total = 140 Wh, all inside the 10:00 bucket.
    assert rows[0]["energy_wh"] == pytest.approx(140.0, abs=1.0)


# --- aggregation: invalid samples break integration (review finding 5) -------------------------


def test_aggregation_does_not_bridge_energy_across_an_invalid_sample() -> None:
    # 1000 W @12:00, invalid @12:02, 1000 W @12:04. The invalid middle sample must break the segment
    # on both sides: no energy is integrated across the known-bad point and that span stays
    # uncovered. Previously the invalid sample was dropped first and the two valid ones were paired,
    # inventing 4 minutes of energy and 240 s of coverage.
    base = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    samples = [
        PowerSample(base, 1000.0),
        PowerSample(base + timedelta(minutes=2), float("nan")),
        PowerSample(base + timedelta(minutes=4), 1000.0),
    ]
    buckets = aggregate_samples(samples)
    accumulator = buckets[floor_to_bucket(base)]
    assert accumulator.energy_wh == pytest.approx(0.0)
    assert accumulator.coverage_seconds == pytest.approx(0.0)
    assert accumulator.sample_count == 2  # the two valid endpoints are still observed


def test_aggregation_integrates_a_valid_pair_but_stops_at_the_next_invalid() -> None:
    # Valid, valid, invalid: the first pair integrates (2 min @1000 W), the second span is dropped.
    base = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    samples = [
        PowerSample(base, 1000.0),
        PowerSample(base + timedelta(minutes=2), 1000.0),
        PowerSample(base + timedelta(minutes=4), float("-inf")),
    ]
    accumulator = aggregate_samples(samples)[floor_to_bucket(base)]
    assert accumulator.coverage_seconds == pytest.approx(120.0)  # only the valid first segment
    assert accumulator.energy_wh == pytest.approx(1000.0 * 120.0 / 3600.0)


# --- historian: gap markers, shutdown tracking, single counting ---------------------------------


def test_a_bad_reading_reaches_the_aggregator_and_breaks_the_segment(tmp_path: Path) -> None:
    from tests.conftest import ManualClock

    store = _store(tmp_path)
    historian = ConsumptionHistorian(store, ManualClock())
    historian.sink.sample("main", "household_load_power", 1000.0, BASE)
    historian.sink.sample("main", "household_load_power", -1.0, BASE + timedelta(minutes=2))
    historian.sink.sample("main", "household_load_power", 1000.0, BASE + timedelta(minutes=4))
    asyncio.run(historian.drain_once())
    row = store.get_consumption_intervals("main")[0]
    assert row["energy_wh"] == pytest.approx(0.0)
    assert row["coverage_seconds"] == pytest.approx(0.0)
    assert row["sample_count"] == 2


def test_historian_store_calls_run_in_tracked_store_threads(tmp_path: Path) -> None:
    from app.admin import forecast_refresh
    from tests.conftest import ManualClock

    store = _store(tmp_path)
    seen: dict[str, int] = {}
    original_upsert = store.upsert_consumption_intervals
    original_prune = store.prune_consumption_intervals

    def upsert(*args):
        seen["upsert"] = len(forecast_refresh._inflight)
        return original_upsert(*args)

    def prune(*args):
        seen["prune"] = len(forecast_refresh._inflight)
        return original_prune(*args)

    store.upsert_consumption_intervals = upsert  # type: ignore[method-assign]
    store.prune_consumption_intervals = prune  # type: ignore[method-assign]
    historian = ConsumptionHistorian(store, ManualClock())
    for minute in range(3):
        historian.sink.sample("main", "household_load_power", 500.0, BASE + timedelta(minutes=minute))

    async def run() -> None:
        await historian.drain_once()
        await historian._maybe_prune()

    asyncio.run(run())
    assert seen["upsert"] >= 1
    assert seen["prune"] >= 1
    assert not forecast_refresh._inflight


def test_a_carried_sample_is_counted_exactly_once_across_drains(tmp_path: Path) -> None:
    from tests.conftest import ManualClock

    store = _store(tmp_path)
    historian = ConsumptionHistorian(store, ManualClock())
    for minute in range(3):
        historian.sink.sample("main", "household_load_power", 1200.0, BASE + timedelta(minutes=minute))
    asyncio.run(historian.drain_once())
    for minute in range(3, 6):
        historian.sink.sample("main", "household_load_power", 1200.0, BASE + timedelta(minutes=minute))
    asyncio.run(historian.drain_once())
    rows = store.get_consumption_intervals("main")
    assert sum(row["sample_count"] for row in rows) == 6
    assert sum(row["energy_wh"] for row in rows) == pytest.approx(1200.0 * 5 / 60.0)


# --- inverter day history: daily reconciliation -------------------------------------------------

from datetime import date
from zoneinfo import ZoneInfo

from app.admin.consumption_backfill import (
    ConsumptionHistoryBackfillService,
    load_daily_consumption,
)
from app.errors import DeviceTimeout
from app.gateway.history import DailyConsumptionReading
from app.optimizer.consumption import rows_to_history
from app.optimizer.daily import DailySource, local_day_bounds
from tests.conftest import AutoClock

BERLIN = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 10, 15)
W0, W1 = "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00"


class _FakeProvider:
    def __init__(self, days: dict[date, float], error: Exception | None = None) -> None:
        self.days, self.error, self.calls = days, error, 0

    async def daily_consumption(self, device_id, *, before, limit):
        self.calls += 1
        if self.error is not None:
            raise self.error
        cutoff = before.astimezone(BERLIN).date()
        older = sorted((d for d in self.days if d < cutoff), reverse=True)[:limit]
        return [DailyConsumptionReading(d, self.days[d]) for d in older]


def _clock(now: datetime = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)) -> AutoClock:
    clock = AutoClock()
    clock._wall = now
    return clock


def _service(store: AdminStore, provider, clock: AutoClock) -> ConsumptionHistoryBackfillService:
    return ConsumptionHistoryBackfillService(
        provider, store, clock, lambda: "Europe/Berlin", lambda: ["main"], lambda _d: True
    )


def _fill_local_day(store: AdminStore, day: date, *, share: float = 1.0, wh_per_bucket: float = 100.0) -> float:
    start, end = local_day_bounds(day, BERLIN)
    count = int((end - start) / timedelta(minutes=15) * share)
    rows = [
        {
            "interval_start": (start + timedelta(minutes=15 * i)).isoformat(),
            "interval_end": (start + timedelta(minutes=15 * (i + 1))).isoformat(),
            "energy_wh": wh_per_bucket, "sample_count": 30, "coverage_seconds": 900.0,
        }
        for i in range(count)
    ]
    store.upsert_consumption_intervals("main", rows)
    return wh_per_bucket * count


def _resolve(store: AdminStore, first: date, last: date, now: datetime | None = None):
    return {
        d.date: d
        for d in load_daily_consumption(
            store, "main", first, last, zone_name="Europe/Berlin", now=now or _clock().now()
        )
    }


def test_bootstrap_stores_up_to_sixty_real_days_and_no_quarter_hours(tmp_path: Path) -> None:
    store = _store(tmp_path)
    days = {TODAY - timedelta(days=i): 10000.0 + i for i in range(1, 80)}  # the device holds more than the window
    provider = _FakeProvider(days)
    assert asyncio.run(_service(store, provider, _clock()).backfill_device("main")) is True
    stored = store.get_consumption_daily("main", "2000-01-01", "2100-01-01")
    assert len(stored) == 60 and {r["source"] for r in stored} == {"inverter_history"}
    assert store.get_consumption_intervals("main") == [] and store.get_consumption_meter_intervals("main", W0, W1) == []
    assert rows_to_history(store.get_consumption_intervals("main"), store.get_consumption_meter_intervals("main", W0, W1)) == []


def test_a_device_with_fewer_days_leaves_the_rest_unavailable_and_backfill_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _FakeProvider({TODAY - timedelta(days=i): 9000.0 for i in range(1, 26)})
    service = _service(store, provider, _clock())
    asyncio.run(service.backfill_device("main"))
    first = store.get_consumption_daily("main", "2000-01-01", "2100-01-01")
    asyncio.run(service.backfill_device("main"))
    assert store.get_consumption_daily("main", "2000-01-01", "2100-01-01") == first
    assert len(first) == 25
    resolved = _resolve(store, TODAY - timedelta(days=60), TODAY - timedelta(days=1))
    assert sum(d.source.value == "unavailable" for d in resolved.values()) == 35
    assert all(d.energy_wh is None for d in resolved.values() if d.source.value == "unavailable")


def test_an_outage_gap_is_healed_per_day_and_sums_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    oct_ = lambda n: date(2026, 10, n)
    local = {oct_(n): _fill_local_day(store, oct_(n)) for n in range(1, 11)}
    now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    provider = _FakeProvider({oct_(n): 5000.0 + n for n in range(11, 18)})
    asyncio.run(_service(store, provider, _clock(now)).backfill_device("main"))
    resolved = _resolve(store, oct_(1), oct_(17), now)
    assert [resolved[oct_(n)].source.value for n in (10, 11, 17)] == ["local_measured", "inverter_history", "inverter_history"]
    assert all(resolved[oct_(n)].profile_available is False for n in range(11, 18))
    assert sum(resolved[oct_(n)].energy_wh for n in range(1, 18)) == pytest.approx(sum(local.values()) + sum(5000.0 + n for n in range(11, 18)))
    assert len(store.get_consumption_intervals("main")) == 96 * 10  # no bucket created by the daily backfill


def test_complete_local_day_wins_and_an_incomplete_one_uses_the_inverter_total(tmp_path: Path) -> None:
    store = _store(tmp_path)
    complete, partial = date(2026, 10, 10), date(2026, 10, 11)
    wh = _fill_local_day(store, complete)
    _fill_local_day(store, partial, share=0.5)
    store.upsert_consumption_daily("main", [
        {"local_date": d.isoformat(), "energy_wh": 77777.0, "source": "inverter_history", "timezone": "Europe/Berlin",
         "fetched_at": "2026-10-15T00:00:00+00:00"} for d in (complete, partial)
    ])
    resolved = _resolve(store, complete, partial)
    assert resolved[complete].source.value == "local_measured" and resolved[complete].energy_wh == pytest.approx(wh)
    assert resolved[complete].profile_available is True and resolved[complete].measured_coverage == pytest.approx(1.0)
    assert resolved[partial].source.value == "inverter_history" and resolved[partial].energy_wh == 77777.0
    assert resolved[partial].profile_available is False  # never an addition of local partial + inverter total


def test_dst_days_are_one_local_date_each(tmp_path: Path) -> None:
    store = _store(tmp_path)
    short, long_ = date(2026, 3, 29), date(2026, 10, 25)
    assert (local_day_bounds(short, BERLIN)[1] - local_day_bounds(short, BERLIN)[0]) == timedelta(hours=23)
    assert (local_day_bounds(long_, BERLIN)[1] - local_day_bounds(long_, BERLIN)[0]) == timedelta(hours=25)
    wh = {d: _fill_local_day(store, d) for d in (short, long_)}
    now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    for d in (short, long_):
        got = _resolve(store, d, d, now)
        assert list(got) == [d] and got[d].source.value == "local_measured"
        assert got[d].energy_wh == pytest.approx(wh[d]) and got[d].measured_coverage == pytest.approx(1.0)


def test_a_device_timeout_during_bootstrap_keeps_existing_data_and_does_not_crash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    wh = _fill_local_day(store, date(2026, 10, 10))
    service = _service(store, _FakeProvider({}, error=DeviceTimeout("response_timeout")), _clock())
    assert asyncio.run(service.backfill_device("main")) is False
    assert store.get_consumption_daily("main", "2000-01-01", "2100-01-01") == []
    assert _resolve(store, date(2026, 10, 10), date(2026, 10, 10))[date(2026, 10, 10)].energy_wh == pytest.approx(wh)


def test_a_failed_run_retries_later_and_a_reconnect_rearms_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    provider = _FakeProvider({}, error=DeviceTimeout("response_timeout"))
    ready = {"main": True}
    clock = _clock()
    service = ConsumptionHistoryBackfillService(
        provider, store, clock, lambda: "Europe/Berlin", lambda: ["main"], lambda d: ready[d]
    )

    async def scenario() -> list[int]:
        calls = []
        await service.run_due_once()
        calls.append(provider.calls)
        await service.run_due_once()  # not due: no aggressive retry
        calls.append(provider.calls)
        ready["main"] = False
        await service.run_due_once()
        ready["main"] = True
        await service.run_due_once()  # reconnected
        calls.append(provider.calls)
        return calls

    assert asyncio.run(scenario()) == [1, 1, 2]


def test_a_stored_day_is_bound_to_the_zone_its_local_date_was_interpreted_in(tmp_path: Path) -> None:
    # A daily total stored under Europe/Berlin must not be silently reused when the active site zone
    # changed: the same local_date read under another zone resolves to UNAVAILABLE (so a future
    # backfill re-reconciles it), while the matching zone still yields the inverter history value.
    store = _store(tmp_path)
    day = date(2026, 10, 11)
    store.upsert_consumption_daily("main", [
        {"local_date": day.isoformat(), "energy_wh": 54321.0, "source": "inverter_history",
         "timezone": "Europe/Berlin", "fetched_at": "2026-10-15T00:00:00+00:00"}
    ])
    now = _clock().now()
    same_zone = {
        d.date: d
        for d in load_daily_consumption(store, "main", day, day, zone_name="Europe/Berlin", now=now)
    }
    assert same_zone[day].source is DailySource.INVERTER_HISTORY
    assert same_zone[day].energy_wh == 54321.0
    other_zone = {
        d.date: d
        for d in load_daily_consumption(store, "main", day, day, zone_name="America/New_York", now=now)
    }
    assert other_zone[day].source is DailySource.UNAVAILABLE
    assert other_zone[day].energy_wh is None


def test_daily_values_never_enter_the_forecast_history_or_the_pruned_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    before = rows_to_history(store.get_consumption_intervals("main"), store.get_consumption_meter_intervals("main", W0, W1))
    asyncio.run(_service(store, _FakeProvider({TODAY - timedelta(days=1): 12000.0}), _clock()).backfill_device("main"))
    after = rows_to_history(store.get_consumption_intervals("main"), store.get_consumption_meter_intervals("main", W0, W1))
    assert before == after == []
    store.upsert_consumption_daily("main", [
        {"local_date": "2026-07-01", "energy_wh": 1.0, "source": "inverter_history", "timezone": "UTC", "fetched_at": "x"}
    ])
    historian = ConsumptionHistorian(store, _clock(), timezone=lambda: "Europe/Berlin")
    asyncio.run(historian._maybe_prune())
    kept = [r["local_date"] for r in store.get_consumption_daily("main", "2000-01-01", "2100-01-01")]
    assert kept == ["2026-10-14"]


async def test_daily_backfill_always_starts_without_a_switch() -> None:
    from unittest.mock import patch

    from tests.api_helpers import make_settings, running_app

    settings = make_settings()
    # The import is not optional: no settings switch exists to turn it off.
    assert not hasattr(settings, "consumption_daily_backfill_enabled")
    # The service is built synchronously during lifespan startup, so a settled app proves it was.
    with patch("app.admin.consumption_backfill.ConsumptionHistoryBackfillService") as service:
        async with running_app(settings):
            pass
    service.assert_called_once()


def test_several_logger_points_for_one_local_date_log_once_and_the_last_one_wins(caplog) -> None:
    from app.gateway.rct_history import RctDailyConsumptionHistory
    from app.protocol.timeseries import TimeSeriesPoint

    class _Gateway:
        async def query_history_series(self, device_id, name, before):
            # Two points on 2026-10-14 in Berlin (UTC+2) and one on the 13th.
            return (
                TimeSeriesPoint(datetime(2026, 10, 14, 6, 0, tzinfo=UTC), 100.0),
                TimeSeriesPoint(datetime(2026, 10, 14, 18, 0, tzinfo=UTC), 200.0),
                TimeSeriesPoint(datetime(2026, 10, 13, 12, 0, tzinfo=UTC), 300.0),
            )

    history = RctDailyConsumptionHistory(_Gateway(), lambda: "Europe/Berlin")
    before = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
    with caplog.at_level("WARNING", logger="app.gateway.rct_history"):
        first = asyncio.run(history.daily_consumption("main", before=before, limit=10))
        asyncio.run(history.daily_consumption("main", before=before, limit=10))
    assert {r.local_date: r.energy_wh for r in first} == {date(2026, 10, 14): 200.0, date(2026, 10, 13): 300.0}
    assert len([m for m in caplog.messages if "several logger points" in m]) == 1


def test_day_logger_uses_wall_clock_date_at_local_midnight_boundary() -> None:
    from datetime import date
    from zoneinfo import ZoneInfo

    from app.gateway.rct_history import RctDailyConsumptionHistory
    from app.protocol.timeseries import TimeSeriesPoint

    class _Gateway:
        query_before = None

        async def query_history_series(self, device_id, name, before):
            self.query_before = before
            return (
                TimeSeriesPoint(datetime(2026, 10, 16, 23, 59, 59, tzinfo=UTC), 19_000.0),
                TimeSeriesPoint(datetime(2026, 10, 15, 23, 59, 59, tzinfo=UTC), 18_018.7),
            )

    gateway = _Gateway()
    history = RctDailyConsumptionHistory(gateway, lambda: "Europe/Berlin")
    before = datetime(2026, 10, 16, tzinfo=ZoneInfo("Europe/Berlin"))
    readings = asyncio.run(history.daily_consumption("main", before=before, limit=10))

    assert gateway.query_before == datetime(2026, 10, 16, tzinfo=UTC)
    assert [(reading.local_date, reading.energy_wh) for reading in readings] == [
        (date(2026, 10, 15), 18_018.7)
    ]


# --- F2: retention prune indexes exist after init (review finding 2) ---------------------------


def _index_names(store: AdminStore) -> set[str]:
    with store.connect() as db:
        return {row["name"] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='index'").fetchall()}


def test_init_creates_the_batch_time_prune_index(tmp_path: Path) -> None:
    # Retention deletes consumption_applied_batches by batch_time, which does not lead the
    # (device_id, batch_id) primary key. Without this index the hourly prune full-scans an
    # append-only ledger that grows unbounded on an active install. It is created and
    # startup-verified by the same mechanism as the interval_start prune index.
    store = _store(tmp_path)
    names = _index_names(store)
    assert "consumption_applied_batches_batch_time_idx" in names
    assert "consumption_history_intervals_interval_start_idx" in names


def test_prune_index_survives_a_store_reopen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.close()
    reopened = AdminStore(tmp_path / "rct.db", SECRET)
    reopened.initialize()
    assert "consumption_applied_batches_batch_time_idx" in _index_names(reopened)


# --- F3: the counter buffer logs its drops like the power buffer (review finding 3) ------------


def test_counter_buffer_is_bounded_and_counts_and_logs_its_drops(caplog) -> None:
    from app.admin.consumption import MAX_BUFFERED_COUNTER_READINGS
    from app.optimizer.meter import TOTAL_COUNTER

    sink = ConsumptionSampleSink()
    overflow = 1200
    with caplog.at_level("WARNING", logger="app.admin.consumption"):
        for i in range(MAX_BUFFERED_COUNTER_READINGS + overflow):
            sink.sample("main", TOTAL_COUNTER, float(i), BASE + timedelta(seconds=i))

    drained = sink.drain_counters()["main"][TOTAL_COUNTER]
    assert len(drained) == MAX_BUFFERED_COUNTER_READINGS  # never exceeds the bound
    assert sink.dropped_counters == overflow  # every reading past the bound is accounted
    assert drained[0][1] == pytest.approx(float(overflow))  # oldest-first eviction

    # Sparse logging mirrors the power buffer: the 1st drop and every 1000th -> {1, 1000} = 2 records.
    warnings = [r for r in caplog.records if "counter buffer full" in r.getMessage()]
    assert len(warnings) == 2


def test_restore_counters_trim_also_counts_drops() -> None:
    from app.admin.consumption import MAX_BUFFERED_COUNTER_READINGS
    from app.optimizer.meter import TOTAL_COUNTER

    sink = ConsumptionSampleSink()
    series = [(BASE + timedelta(seconds=i), float(i)) for i in range(MAX_BUFFERED_COUNTER_READINGS + 5)]
    sink.restore_counters("main", {TOTAL_COUNTER: series})
    assert sink.dropped_counters == 5
    assert len(sink.drain_counters()["main"][TOTAL_COUNTER]) == MAX_BUFFERED_COUNTER_READINGS
