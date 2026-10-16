#!/usr/bin/env python3
#
# tests/test_forecast_refresh.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SQLite persistence and rolling refresh behavior for the spot and solar forecasts."""

import asyncio
import threading
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.admin import forecast_refresh
from app.admin.forecast_refresh import (
    SOLAR_STATUS_KEY,
    SPOT_STATUS_KEY,
    RetryBackoff,
    drain_store_threads,
    load_refresh_status,
    refresh_solar_forecast,
    refresh_spot_market,
    run_spot_market_refresh_loop,
    solar_config_key,
    solar_refresh_tick,
    spot_config_key,
    spot_refresh_plan,
    spot_refresh_tick,
)
from app.admin.store import AdminStore
from app.optimizer.models import SolarForecast, SolarForecastInterval
from app.spotmarket.base import SpotMarketError, SpotMarketNotAvailableYet
from app.spotmarket.models import SpotMarketConfig, SpotMarketProvider, SpotPriceInterval, SpotPriceSnapshot
from app.weather.base import WeatherError
from app.weather.models import WeatherConfig

SECRET = "f" * 48


def _three_days(now: datetime):
    today = now.date()
    return [
        [
            datetime.combine(today + timedelta(days=offset), time.min, tzinfo=UTC)
            + timedelta(minutes=15 * slot)
            for slot in range(96)
        ]
        for offset in (-1, 0, 1)
    ]


def test_admin_schema_creates_forecast_tables_idempotently(tmp_path):
    path = tmp_path / "admin.db"
    initial = AdminStore(path, SECRET)
    initial.initialize()
    initial.close()
    reopened = AdminStore(path, SECRET)
    reopened.initialize()
    with reopened.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN (?, ?)",
                          ("spot_market_intervals", "solar_forecast_intervals")).fetchone()[0] == 2
        assert "STRICT" in db.execute(
            "SELECT sql FROM sqlite_schema WHERE name='spot_market_intervals'"
        ).fetchone()[0]
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 0
    reopened.close()


# 14:00 Berlin (CEST): the day-ahead retrieval time (13:00 market time) has passed by an hour.
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
# 11:00 Berlin: tomorrow's prices cannot be retrieved yet.
EARLY = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
SPOT = SpotMarketConfig(enabled=True, bidding_zone="DE-LU")
SOLAR = WeatherConfig(
    enabled=True, latitude=51.18, longitude=7.19, timezone="UTC",
    pv_peak_power_wp=10000, tilt_deg=35, azimuth_deg=0,
)


def _day(now: datetime, offset: int, price: str = "0.10") -> SpotPriceSnapshot:
    midnight = datetime.combine(now.date() + timedelta(days=offset), time.min, tzinfo=UTC)
    return SpotPriceSnapshot(
        SpotMarketProvider.ENERGY_CHARTS, "DE-LU", "EUR", now,
        tuple(SpotPriceInterval(midnight + timedelta(minutes=15 * slot), midnight + timedelta(minutes=15 * slot + 15),
                                Decimal(price)) for slot in range(96)),
        timezone="UTC",
    )


class _Provider:
    """Spot provider that records every request; ``fetch_current`` must never be needed."""

    def __init__(self, now: datetime = NOW, *, published: bool = True, error: Exception | None = None):
        self.now, self.published, self.error, self.calls = now, published, error, []

    def fetch_today(self, config):
        self.calls.append("today")
        if self.error:
            raise self.error
        return _day(self.now, 0)

    def fetch_yesterday(self, config, timezone):
        self.calls.append("yesterday")
        return _day(self.now, -1)

    def fetch_day_ahead(self, config):
        self.calls.append("tomorrow")
        if self.error:
            raise self.error
        if not self.published:
            raise SpotMarketNotAvailableYet("not yet")
        return _day(self.now, 1, "0.30")

    def fetch_current(self, config):
        self.calls.append("current")
        raise AssertionError("the current price is derived from the database")


class _Zones:
    def __init__(self):
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1


def _store(tmp_path):
    store = AdminStore(tmp_path / "admin.db", SECRET)
    store.initialize()
    return store


def test_admin_day_ahead_retrieval_time_is_thirteen_hundred_market_time():
    # Documented constant: SDAC results ~12:45 CET, retrieval from 13:00 Europe/Berlin (CET/CEST).
    opens = forecast_refresh.day_ahead_available_from(NOW.date() + timedelta(days=1))
    assert opens.astimezone(UTC) == datetime(2026, 10, 14, 11, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_spot_first_start_fetches_three_days_once_and_the_zone_list(tmp_path):
    store, provider, zones = _store(tmp_path), _Provider(), _Zones()
    assert await refresh_spot_market(store, provider, SPOT, now=NOW, zone_cache=zones)
    assert sorted(provider.calls) == ["today", "tomorrow", "yesterday"]
    rows = store.get_spot_market_intervals(spot_config_key(SPOT))
    assert len(rows) == 3 * 96
    assert {row["local_date"] for row in rows} == {
        (NOW.date() + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)
    }
    assert zones.refreshes == 1
    assert load_refresh_status(store, SPOT_STATUS_KEY, spot_config_key(SPOT))["ok"] is True
    store.close()


@pytest.mark.asyncio
async def test_spot_complete_database_makes_no_request_and_waits_for_the_next_day(tmp_path):
    store, provider = _store(tmp_path), _Provider()
    assert await refresh_spot_market(store, provider, SPOT, now=NOW)
    provider.calls.clear()
    backoff = RetryBackoff()
    for later in (NOW + timedelta(minutes=1), NOW + timedelta(hours=9), NOW + timedelta(hours=11)):
        wait = await spot_refresh_tick(store, provider, SPOT, later, backoff)
        assert provider.calls == [] and wait == forecast_refresh.MAX_IDLE_WAIT
    # Next local day, before the retrieval time: tomorrow is not yet expected, so nothing is due.
    rows = store.get_spot_market_intervals(spot_config_key(SPOT))
    assert spot_refresh_plan(rows, NOW + timedelta(hours=12, minutes=30)).complete
    assert not spot_refresh_plan(rows, NOW + timedelta(hours=23)).complete  # 13:00 Berlin the next day
    store.close()


@pytest.mark.asyncio
async def test_spot_waits_for_the_retrieval_time_and_fetches_tomorrow_only_then(tmp_path):
    store, provider = _store(tmp_path), _Provider(EARLY)
    backoff = RetryBackoff()
    await spot_refresh_tick(store, provider, SPOT, EARLY, backoff)  # first start: today and yesterday
    assert sorted(provider.calls) == ["today", "yesterday"]
    provider.calls.clear()
    wait = await spot_refresh_tick(store, provider, SPOT, EARLY + timedelta(minutes=30), backoff)
    assert provider.calls == [] and wait == forecast_refresh.MAX_IDLE_WAIT
    assert load_refresh_status(store, SPOT_STATUS_KEY, spot_config_key(SPOT))["ok"] is True  # no failure before the time
    provider.now = NOW
    wait = await spot_refresh_tick(store, provider, SPOT, NOW - timedelta(minutes=55), backoff)
    assert provider.calls == ["tomorrow"]
    assert wait == timedelta(seconds=1)
    assert len(store.get_spot_market_intervals(spot_config_key(SPOT))) == 3 * 96
    store.close()


@pytest.mark.asyncio
async def test_spot_retries_yesterday_when_only_that_fetch_failed(tmp_path):
    class FlakyYesterday(_Provider):
        fail_yesterday = True

        def fetch_yesterday(self, config, timezone):
            self.calls.append("yesterday")
            if self.fail_yesterday:
                raise SpotMarketError("yesterday temporarily unavailable")
            return _day(self.now, -1)

    store, provider = _store(tmp_path), FlakyYesterday(EARLY)
    backoff = RetryBackoff()
    assert await spot_refresh_tick(store, provider, SPOT, EARLY, backoff) == timedelta(minutes=5)
    rows = store.get_spot_market_intervals(spot_config_key(SPOT))
    assert {row["local_date"] for row in rows} == {EARLY.date().isoformat()}
    assert not spot_refresh_plan(rows, EARLY).complete
    failed = load_refresh_status(store, SPOT_STATUS_KEY, spot_config_key(SPOT))
    assert failed["ok"] is False and "yesterday temporarily unavailable" in failed["message"]

    provider.calls.clear()
    await spot_refresh_tick(store, provider, SPOT, EARLY + timedelta(minutes=1), backoff)
    assert provider.calls == []
    provider.fail_yesterday = False
    assert await spot_refresh_tick(store, provider, SPOT, EARLY + timedelta(minutes=5), backoff) == timedelta(seconds=1)
    assert provider.calls == ["yesterday"]
    assert {row["local_date"] for row in store.get_spot_market_intervals(spot_config_key(SPOT))} == {
        (EARLY.date() - timedelta(days=1)).isoformat(), EARLY.date().isoformat(),
    }
    assert load_refresh_status(store, SPOT_STATUS_KEY, spot_config_key(SPOT))["ok"] is True
    store.close()


@pytest.mark.asyncio
async def test_spot_retries_with_backoff_and_warns_only_after_a_failure(tmp_path, caplog):
    store = _store(tmp_path)
    key = spot_config_key(SPOT)
    healthy = _Provider(EARLY)
    await refresh_spot_market(store, healthy, SPOT, now=EARLY)
    provider = _Provider(error=SpotMarketError("Energy-Charts is unavailable after 3 attempts (HTTP 503)"))
    backoff = RetryBackoff()
    caplog.set_level("WARNING")
    assert await spot_refresh_tick(store, provider, SPOT, NOW, backoff) == timedelta(minutes=5)
    assert provider.calls == ["tomorrow"]
    assert "Daily spot-price retrieval failed" in caplog.text and "HTTP 503" in caplog.text
    assert load_refresh_status(store, SPOT_STATUS_KEY, key)["ok"] is False
    assert await spot_refresh_tick(store, provider, SPOT, NOW + timedelta(minutes=2), backoff) == timedelta(minutes=3)
    assert provider.calls == ["tomorrow"]  # still backing off: no second request
    assert await spot_refresh_tick(store, provider, SPOT, NOW + timedelta(minutes=5), backoff) == timedelta(minutes=10)
    assert provider.calls == ["tomorrow", "tomorrow"]
    provider.error = None
    assert await spot_refresh_tick(store, provider, SPOT, NOW + timedelta(minutes=15), backoff) == timedelta(seconds=1)
    assert load_refresh_status(store, SPOT_STATUS_KEY, key)["ok"] is True
    store.close()


@pytest.mark.asyncio
async def test_spot_restart_keeps_the_retry_spacing_of_a_persisted_failure(tmp_path):
    store, provider = _store(tmp_path), _Provider()
    await _store_failure(store, SPOT_STATUS_KEY, spot_config_key(SPOT))
    wait = await spot_refresh_tick(store, provider, SPOT, datetime.now(UTC) + timedelta(minutes=1), RetryBackoff())
    assert provider.calls == [] and wait <= timedelta(minutes=5)
    store.close()


async def _store_failure(store, status_key, config_key):
    await forecast_refresh._record(store, status_key, config_key, False, "down")


@pytest.mark.asyncio
async def test_spot_unpublished_tomorrow_is_a_failure_only_after_the_grace(tmp_path):
    store = _store(tmp_path)
    key = spot_config_key(SPOT)
    await refresh_spot_market(store, _Provider(EARLY), SPOT, now=EARLY)
    provider = _Provider(published=False)
    soon = datetime(2026, 10, 14, 11, 20, tzinfo=UTC)  # 13:20 Berlin, inside the grace
    assert not await refresh_spot_market(store, provider, SPOT, now=soon)
    assert load_refresh_status(store, SPOT_STATUS_KEY, key)["ok"] is True  # still within the grace: no warning
    assert not await refresh_spot_market(store, provider, SPOT, now=NOW)
    status = load_refresh_status(store, SPOT_STATUS_KEY, key)
    assert status["ok"] is False and "not published" in status["message"]
    store.close()


@pytest.mark.asyncio
async def test_solar_fetches_once_per_local_day_and_otherwise_stays_quiet(tmp_path):
    store, key = _store(tmp_path), solar_config_key(SOLAR)
    starts = [start for day in _three_days(NOW) for start in day]
    calls = []

    class Provider:
        def fetch_preview(self, config, now):
            calls.append(now)
            return SolarForecast(created_at=now, intervals=tuple(
                SolarForecastInterval(start + timedelta(days=(now.date() - NOW.date()).days),
                                      start + timedelta(days=(now.date() - NOW.date()).days, minutes=15), 100.0)
                for start in starts
            ))

    backoff = RetryBackoff()
    assert await solar_refresh_tick(store, Provider(), SOLAR, NOW, backoff) == timedelta(seconds=1)
    assert len(calls) == 1
    for later in (NOW + timedelta(minutes=20), NOW + timedelta(hours=8)):
        assert await solar_refresh_tick(store, Provider(), SOLAR, later, backoff) > timedelta(0)
    assert len(calls) == 1  # no request while today and tomorrow are stored
    await solar_refresh_tick(store, Provider(), SOLAR, NOW + timedelta(hours=13), backoff)  # next local day
    assert len(calls) == 2
    assert load_refresh_status(store, SOLAR_STATUS_KEY, key)["ok"] is True
    store.close()


@pytest.mark.asyncio
async def test_solar_failure_warns_backs_off_and_recovers(tmp_path, caplog):
    store, key = _store(tmp_path), solar_config_key(SOLAR)
    starts = [start for day in _three_days(NOW) for start in day]

    class Provider:
        calls = 0
        error = WeatherError("Open-Meteo is unreachable")

        def fetch_preview(self, config, now):
            self.calls += 1
            if self.error:
                raise self.error
            return SolarForecast(created_at=now, intervals=tuple(
                SolarForecastInterval(start, start + timedelta(minutes=15), 100.0) for start in starts))

    provider, backoff = Provider(), RetryBackoff()
    caplog.set_level("WARNING")
    assert await solar_refresh_tick(store, provider, SOLAR, NOW, backoff) == timedelta(minutes=5)
    assert "Daily solar forecast retrieval failed" in caplog.text
    assert load_refresh_status(store, SOLAR_STATUS_KEY, key)["ok"] is False
    await solar_refresh_tick(store, provider, SOLAR, NOW + timedelta(minutes=1), backoff)
    assert provider.calls == 1
    provider.error = None
    await solar_refresh_tick(store, provider, SOLAR, NOW + timedelta(minutes=5), backoff)
    assert provider.calls == 2 and load_refresh_status(store, SOLAR_STATUS_KEY, key)["ok"] is True
    store.close()


@pytest.mark.asyncio
async def test_solar_forecast_that_misses_tomorrow_counts_as_a_failed_retrieval(tmp_path):
    store, key = _store(tmp_path), solar_config_key(SOLAR)

    class Provider:
        def fetch_preview(self, config, now):
            return SolarForecast(created_at=now, intervals=())

    assert not await refresh_solar_forecast(store, Provider(), SOLAR, now=NOW)
    status = load_refresh_status(store, SOLAR_STATUS_KEY, key)
    assert status["ok"] is False and "tomorrow" in status["message"]
    store.close()


@pytest.mark.asyncio
async def test_solar_refresh_records_a_long_failure_truncated_and_a_success_resets_it(tmp_path):
    store, key = _store(tmp_path), solar_config_key(SOLAR)
    starts = [start for day in _three_days(NOW) for start in day]

    class Provider:
        fail = True

        def fetch_preview(self, config, now):
            if self.fail:
                raise WeatherError("x" * 500)
            return SolarForecast(created_at=now, intervals=tuple(
                SolarForecastInterval(start, start + timedelta(minutes=15), 100.0) for start in starts))

    provider = Provider()
    assert not await refresh_solar_forecast(store, provider, SOLAR, now=NOW)
    status = load_refresh_status(store, SOLAR_STATUS_KEY, key)
    assert status["ok"] is False and len(status["message"]) == 200
    provider.fail = False
    assert await refresh_solar_forecast(store, provider, SOLAR, now=NOW)
    assert load_refresh_status(store, SOLAR_STATUS_KEY, key)["ok"] is True
    store.close()


@pytest.mark.asyncio
async def test_refresh_discards_its_result_when_the_config_changed_meanwhile(tmp_path):
    store = _store(tmp_path)
    assert not await refresh_spot_market(store, _Provider(), SPOT, lambda: False, now=NOW)
    assert store.get_spot_market_intervals(spot_config_key(SPOT)) == []
    assert load_refresh_status(store, SPOT_STATUS_KEY, spot_config_key(SPOT)) is None
    store.close()


class _App:
    def __init__(self, config):
        self.state = SimpleNamespace(spotmarket_config=config)


@pytest.mark.asyncio
async def test_spot_loop_wakes_on_event_survives_an_exception_and_records_it(tmp_path, monkeypatch):
    store = AdminStore(tmp_path / "admin.db", SECRET)
    store.initialize()
    config = SpotMarketConfig(enabled=True, bidding_zone="DE-LU")
    calls: list[int] = []
    reached = asyncio.Event()

    async def fake_refresh(store_, provider, config_, is_current=None, **kwargs):
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("boom")
        reached.set()
        return True

    monkeypatch.setattr(forecast_refresh, "refresh_spot_market", fake_refresh)
    event = asyncio.Event()
    task = asyncio.create_task(run_spot_market_refresh_loop(store, object(), _App(config), event))
    try:
        for _ in range(200):  # first pass raises; wait until the loop is parked on the event
            await asyncio.sleep(0.01)
            status = load_refresh_status(store, SPOT_STATUS_KEY, spot_config_key(config))
            if status:
                break
        assert status["ok"] is False and status["message"] == "boom"
        assert calls == [0]
        event.set()
        await asyncio.wait_for(reached.wait(), timeout=5)
        assert calls == [0, 1]
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


@pytest.mark.asyncio
async def test_loop_keeps_a_signal_set_right_after_the_wait_timed_out(monkeypatch):
    event, ticks, third = asyncio.Event(), [], asyncio.Event()
    real_wait_for = asyncio.wait_for
    waits = 0

    async def tick(config, backoff):
        ticks.append(len(ticks))
        if len(ticks) == 3:
            third.set()
        return timedelta(hours=1)

    async def racing_wait_for(awaitable, timeout):
        nonlocal waits
        waits += 1
        if waits == 1:  # the save lands in the gap between the timeout and the loop's next step
            awaitable.close()
            event.set()
            raise TimeoutError
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(forecast_refresh.asyncio, "wait_for", racing_wait_for)
    task = asyncio.create_task(forecast_refresh._run_loop(
        tick, SPOT_STATUS_KEY, object(), lambda: "config", lambda config: "key", event))
    try:
        await real_wait_for(third.wait(), timeout=5)  # the lost signal would leave the loop asleep for an hour
        assert ticks == [0, 1, 2]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_drain_waits_for_a_store_thread_whose_awaiter_was_cancelled():
    started, release = threading.Event(), threading.Event()
    finished = []

    def slow_write():
        started.set()
        release.wait(5)
        finished.append(True)

    task = asyncio.create_task(forecast_refresh._store_thread(slow_write))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not finished  # cancelling the task left the thread running

    drain = asyncio.create_task(drain_store_threads(timeout=5))
    await asyncio.sleep(0.05)
    assert not drain.done()
    release.set()
    await drain
    assert finished == [True]


@pytest.mark.parametrize(
    ("now", "zone", "first", "last"),
    [
        (datetime(2026, 3, 10, 23, 30, tzinfo=UTC), "Europe/Berlin", "2026-03-10", "2026-03-12"),
        (datetime(2026, 3, 10, 23, 30, tzinfo=UTC), "UTC", "2026-03-09", "2026-03-11"),
        (datetime(2026, 3, 28, 23, 30, tzinfo=UTC), "Europe/Berlin", "2026-03-28", "2026-03-30"),
        (datetime(2026, 3, 29, 12, 0, tzinfo=UTC), "Europe/Berlin", "2026-03-28", "2026-03-30"),
    ],
)
def test_local_three_day_window_spans_yesterday_to_tomorrow_in_the_zone(now, zone, first, last) -> None:
    result_zone, first_date, last_date = forecast_refresh.local_three_day_window(now, zone)
    assert (result_zone.key, first_date.isoformat(), last_date.isoformat()) == (zone, first, last)


def test_local_three_day_window_rejects_an_unknown_zone() -> None:
    with pytest.raises(Exception) as raised:
        forecast_refresh.local_three_day_window(datetime(2026, 3, 10, tzinfo=UTC), "Not/AZone")
    assert type(raised.value).__name__ == "ZoneInfoNotFoundError"
