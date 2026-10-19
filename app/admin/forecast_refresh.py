#!/usr/bin/env python3
#
# app/admin/forecast_refresh.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Daily background refresh and database projections for the three-day energy forecasts.

Everything the GUI shows comes from the database. The refresh loops evaluate the stored rows and
contact the provider only when a day's data is missing: spot prices for tomorrow once the day-ahead
auction results are out, the solar forecast once per local day. Failures are retried with backoff
and surface as a warning (log and GUI) only while the missing data stays missing.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.admin.store import AdminStore
from app.admin.store_threads import (  # noqa: F401 - re-exported for app_factory/tests that import them from here
    _inflight,
    drain_store_threads,
    store_thread,
)
from app.clock import Clock, SystemClock
from app.energy.activity import EnergyActivity
from app.optimizer.models import SolarForecast
from app.spotmarket.base import (
    SpotMarketError,
    SpotMarketNotAvailableYet,
    SpotPriceProvider,
)
from app.spotmarket.models import SpotMarketConfig, SpotPriceSnapshot
from app.weather.base import WeatherError
from app.weather.models import WeatherConfig

log = logging.getLogger(__name__)
# The SDAC day-ahead auction results (EPEX SPOT / NEMO, published for all EU bidding zones) are out
# around 12:45 CET (13:45 is the documented fallback window). Retrieval starts at 13:00 market time,
# which is Europe/Berlin (CET/CEST) regardless of the operator's own zone.
MARKET_TIMEZONE = ZoneInfo("Europe/Berlin")
DAY_AHEAD_FETCH_FROM = time(13, 0)
# Policy choice, not a provider guarantee: a still-missing tomorrow becomes a warning only this long
# after the retrieval time, so a slightly late publication never alarms the operator.
PUBLICATION_GRACE = timedelta(hours=1)
# Retry spacing after a failed attempt, doubling up to the cap. Energy-Charts allows roughly two
# requests per minute, so even the first retry stays far below its rate limit.
RETRY_BACKOFF_FIRST = timedelta(minutes=5)
RETRY_BACKOFF_MAX = timedelta(hours=1)
# Upper bound of one sleep; the loop then re-reads the database (no provider request) so a clock
# jump or a manual database change is noticed.
MAX_IDLE_WAIT = timedelta(hours=1)
SPOT_STATUS_KEY = "spot_refresh_status"
SOLAR_STATUS_KEY = "solar_refresh_status"
_MAX_ERROR_LENGTH = 200
# The cancellation-safe store-thread mechanism now lives in the neutral app.admin.store_threads
# module and is shared by every background service that writes the store. This alias keeps the
# existing in-module callers (and importers that already reach _store_thread here) working while the
# single _inflight registry and drain_store_threads stay the ones the neutral module owns.
_store_thread = store_thread


def record_refresh_status(
    store: AdminStore,
    status_key: str,
    config_key: str,
    ok: bool,
    message: str | None = None,
) -> None:
    """Persist the outcome of the latest background refresh; a success resets a prior error."""
    store.put(
        status_key,
        {
            "config_key": config_key,
            "ok": ok,
            "at": datetime.now(UTC).isoformat(),
            "message": (
                None if ok else (message or "Refresh failed")[:_MAX_ERROR_LENGTH]
            ),
        },
    )


def load_refresh_status(
    store: AdminStore, status_key: str, config_key: str
) -> dict | None:
    """The last refresh outcome for this configuration, or ``None`` when none was recorded yet.

    The lookup goes through the store's dedicated ``get_refresh_status`` accessor rather than the
    generic settings ``get``. The refresh status is error-annotation metadata layered on top of the
    requested forecast data, not one of the caches a dashboard projection was asked to read, so a
    store that cannot provide the accessor simply reports no status instead of being treated as a
    data-cache access.
    """
    reader = getattr(store, "get_refresh_status", None)
    if not callable(reader):
        return None
    saved = reader(status_key)
    if not isinstance(saved, dict) or saved.get("config_key") != config_key:
        return None
    return saved


async def _record(
    store: AdminStore,
    status_key: str,
    config_key: str,
    ok: bool,
    message: str | None = None,
) -> None:
    try:
        await _store_thread(
            record_refresh_status, store, status_key, config_key, ok, message
        )
    except Exception:
        log.exception("Could not persist the forecast refresh status")


def _error_text(error: BaseException) -> str:
    return str(error).strip() or type(error).__name__


async def _write_if_current(
    store: AdminStore, is_current: Callable[[], bool] | None, func, *args
) -> bool:
    """Write under the save lock, and only while the refreshed configuration is still the active one."""

    def run() -> bool:
        with store.forecast_write_lock:
            if is_current is not None and not is_current():
                return False
            func(*args)
            return True

    return await _store_thread(run)


def spot_config_key(config: SpotMarketConfig) -> str:
    identity = {"provider": config.provider.value, "bidding_zone": config.bidding_zone}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def solar_config_key(config: WeatherConfig) -> str:
    identity = config.model_dump(
        mode="json", exclude={"enabled", "location_name", "location_region"}
    )
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def local_three_day_window(now: datetime, timezone: str) -> tuple[ZoneInfo, date, date]:
    """The zone and the first/last local date of the yesterday..tomorrow window around ``now``."""
    zone = ZoneInfo(timezone)
    local_date = now.astimezone(zone).date()
    return zone, local_date - timedelta(days=1), local_date + timedelta(days=1)


def _window(now: datetime, timezone: str) -> tuple[ZoneInfo, date, date, str, str]:
    zone, first_day, last_day = local_three_day_window(now, timezone)
    return zone, first_day, last_day, first_day.isoformat(), last_day.isoformat()


def _spot_rows(
    snapshots: tuple[SpotPriceSnapshot | None, ...],
    config_key: str,
    timezone: str,
    now: datetime,
) -> list[dict]:
    zone, first_day, last_day, _, _ = _window(now, timezone)
    rows: dict[str, dict] = {}
    for snapshot in snapshots:
        if snapshot is None:
            continue
        for interval in snapshot.intervals:
            day = interval.start.astimezone(zone).date()
            if first_day <= day <= last_day:
                start = interval.start.isoformat()
                rows[start] = {
                    "config_key": config_key,
                    "local_date": day.isoformat(),
                    "interval_start": start,
                    "interval_end": interval.end.isoformat(),
                    "price_eur_per_kwh": float(interval.price_eur_per_kwh),
                    "timezone": timezone,
                    "fetched_at": snapshot.fetched_at.isoformat(),
                }
    return list(rows.values())


@dataclass(frozen=True, slots=True)
class RefreshPlan:
    """What the stored rows say about the next provider retrieval; derived from the database only."""

    fetch_today: bool
    fetch_tomorrow: bool
    fetch_yesterday: bool
    complete: bool  # nothing that is due is missing
    overdue: bool  # tomorrow is missing and the publication grace has passed
    wake_at: datetime  # when time alone can change this plan

    @property
    def due(self) -> bool:
        return not self.complete


def day_ahead_available_from(delivery_date: date) -> datetime:
    """When the day-ahead prices for ``delivery_date`` can be retrieved (D-1, market time)."""
    return datetime.combine(
        delivery_date - timedelta(days=1), DAY_AHEAD_FETCH_FROM, tzinfo=MARKET_TIMEZONE
    )


def spot_refresh_plan(rows: list[dict], now: datetime) -> RefreshPlan:
    """Decide from the stored spot rows whether a retrieval is due.

    Today must be stored. Tomorrow is only expected once its auction results can be retrieved; before
    that its absence is the normal state, not an error.
    """
    zone = ZoneInfo(rows[0]["timezone"]) if rows else MARKET_TIMEZONE
    days = {row["local_date"] for row in rows}
    today = now.astimezone(zone).date()
    tomorrow = today + timedelta(days=1)
    has_yesterday = (today - timedelta(days=1)).isoformat() in days
    has_today, has_tomorrow = today.isoformat() in days, tomorrow.isoformat() in days
    opens = day_ahead_available_from(tomorrow)
    fetch_tomorrow = not has_tomorrow and now >= opens
    complete = has_yesterday and has_today and not fetch_tomorrow
    return RefreshPlan(
        fetch_today=not has_today,
        fetch_tomorrow=fetch_tomorrow,
        fetch_yesterday=not has_yesterday,
        complete=complete,
        overdue=not has_tomorrow and now >= opens + PUBLICATION_GRACE,
        wake_at=(
            day_ahead_available_from(today + timedelta(days=2))
            if has_tomorrow
            else max(opens, now)
        ),
    )


def solar_refresh_plan(
    rows: list[dict], now: datetime, timezone: str | None
) -> RefreshPlan:
    """Decide from the stored solar rows whether a retrieval is due: today and tomorrow must be stored.

    The forecast model has no publication time, so a new local day simply needs its window loaded
    once. That keeps the "next 24 hours" figure fully backed by the database around the clock.
    """
    zone = ZoneInfo(timezone or "UTC")
    days = {row["local_date"] for row in rows}
    today = now.astimezone(zone).date()
    complete = {today.isoformat(), (today + timedelta(days=1)).isoformat()} <= days
    midnight = datetime.combine(today + timedelta(days=1), time.min, tzinfo=zone)
    return RefreshPlan(
        fetch_today=not complete,
        fetch_tomorrow=not complete,
        fetch_yesterday=False,
        complete=complete,
        overdue=not complete,
        wake_at=midnight if complete else now,
    )


@dataclass(slots=True)
class RetryBackoff:
    """In-memory retry spacing; a config save (user action) resets it."""

    failures: int = 0
    next_attempt: datetime | None = None
    seeded: bool = False

    def failed(self, now: datetime) -> timedelta:
        self.failures += 1
        delay = min(RETRY_BACKOFF_FIRST * 2 ** (self.failures - 1), RETRY_BACKOFF_MAX)
        self.next_attempt = now + delay
        return delay

    def reset(self) -> None:
        self.failures, self.next_attempt, self.seeded = 0, None, True


def _clamp_wait(delta: timedelta) -> timedelta:
    return min(max(delta, timedelta(seconds=1)), MAX_IDLE_WAIT)


async def _evaluate(
    store: AdminStore,
    status_key: str,
    config_key: str,
    plan: RefreshPlan,
    backoff: RetryBackoff,
    now: datetime,
    refresh,
    active: Callable[[], bool] | None = None,
) -> timedelta:
    """Run ``refresh`` only when ``plan`` says it is due and the backoff allows; return the next wait."""
    if not plan.due:
        return _clamp_wait(plan.wake_at - now)
    if not backoff.seeded:
        # After a restart the persisted failure keeps the retry spacing instead of retrying at once.
        backoff.seeded = True
        status = await _store_thread(load_refresh_status, store, status_key, config_key)
        if status and not status.get("ok"):
            backoff.failures = 1
            backoff.next_attempt = (
                datetime.fromisoformat(status["at"]) + RETRY_BACKOFF_FIRST
            )
    if backoff.next_attempt is not None and now < backoff.next_attempt:
        return _clamp_wait(backoff.next_attempt - now)
    if active is not None and not active():
        return MAX_IDLE_WAIT  # the Energy Manager went to rest meanwhile: begin no provider call
    if await refresh():
        backoff.reset()
        return timedelta(seconds=1)
    return backoff.failed(now)


async def refresh_spot_market(
    store: AdminStore,
    provider: SpotPriceProvider,
    config: SpotMarketConfig,
    is_current: Callable[[], bool] | None = None,
    *,
    now: datetime | None = None,
    zone_cache=None,
    on_stored: Callable[[], None] | None = None,
) -> bool:
    """Retrieve only the spot days that are due (see :func:`spot_refresh_plan`) and store them.

    Returns ``True`` once the stored rows are complete. ``is_current`` tells whether ``config`` is
    still the active configuration; a refresh that was overtaken by a config save discards its
    result. ``zone_cache`` (the bidding-zone list) is refreshed together with a successful retrieval.
    """
    if not config.enabled:
        return False
    now = now or datetime.now(UTC)
    key = spot_config_key(config)
    existing = await _store_thread(store.get_spot_market_intervals, key)
    plan = spot_refresh_plan(existing, now)
    if plan.complete:
        return True
    zone_name = existing[0]["timezone"] if existing else None
    _, _, _, first_text, last_text = _window(now, zone_name or "UTC")
    await _store_thread(store.prune_spot_market_intervals, key, first_text, last_text)

    snapshots: list[SpotPriceSnapshot] = []
    error: SpotMarketError | None = None
    if plan.fetch_today:
        try:
            today = await asyncio.to_thread(provider.fetch_today, config)
            snapshots.append(today)
            zone_name = today.timezone or zone_name or "UTC"
        except SpotMarketError as exc:
            error = exc
    if error is None and plan.fetch_tomorrow:
        try:
            tomorrow = await asyncio.to_thread(provider.fetch_day_ahead, config)
            snapshots.append(tomorrow)
            zone_name = zone_name or tomorrow.timezone or "UTC"
        except SpotMarketNotAvailableYet:
            pass  # judged below against the publication grace
        except SpotMarketError as exc:
            error = exc
    if error is None and plan.fetch_yesterday and zone_name:
        try:
            snapshots.append(
                await asyncio.to_thread(provider.fetch_yesterday, config, zone_name)
            )
        except SpotMarketError as exc:
            error = exc

    zone_name = zone_name or "UTC"
    _, _, _, first_text, last_text = _window(now, zone_name)
    merged = {row["interval_start"]: row for row in existing}
    for row in _spot_rows(tuple(snapshots), key, zone_name, now):
        merged[row["interval_start"]] = row
    rows = [
        row for row in merged.values() if first_text <= row["local_date"] <= last_text
    ]
    if snapshots and not await _write_if_current(
        store, is_current, store.replace_spot_market_intervals, rows
    ):
        log.info(
            "Spot-market configuration changed during the refresh; discarding its result"
        )
        return False
    if snapshots and on_stored is not None:
        on_stored()  # new rows are stored even when tomorrow is still missing

    after = spot_refresh_plan(rows, now)
    if error is None and not after.complete:
        if not after.overdue:
            log.info(
                "The spot-price window is incomplete; missing day(s) will be retried"
            )
            return False
        error = SpotMarketNotAvailableYet(
            "Tomorrow's prices were not published after the usual retrieval time"
        )
    if error is not None:
        log.warning(
            "Daily spot-price retrieval failed (%s): %s", config.bidding_zone, error
        )
        await _record(store, SPOT_STATUS_KEY, key, False, _error_text(error))
        return False
    await _record(store, SPOT_STATUS_KEY, key, True)
    if zone_cache is not None:
        try:
            await asyncio.to_thread(zone_cache.refresh)
        except SpotMarketError as exc:
            log.warning("Bidding-zone list could not be retrieved: %s", exc)
    return True


async def refresh_solar_forecast(
    store: AdminStore,
    provider,
    config: WeatherConfig,
    is_current: Callable[[], bool] | None = None,
    *,
    now: datetime | None = None,
    on_stored: Callable[[], None] | None = None,
) -> bool:
    """Retrieve yesterday, today and tomorrow's solar forecast in one request and store it.

    Returns ``True`` once the stored rows cover today and tomorrow.
    """
    if not config.is_forecastable():
        return False
    now = now or datetime.now(UTC)
    key = solar_config_key(config)
    _, _, _, first_text, last_text = _window(now, config.timezone or "UTC")
    await _store_thread(
        store.prune_solar_forecast_intervals, key, first_text, last_text
    )
    try:
        forecast: SolarForecast = await asyncio.to_thread(
            provider.fetch_preview, config, now
        )
    except WeatherError as exc:
        log.warning("Daily solar forecast retrieval failed: %s", exc)
        await _record(store, SOLAR_STATUS_KEY, key, False, _error_text(exc))
        return False

    zone, first_day, last_day, _, _ = _window(now, config.timezone or "UTC")
    rows = []
    for interval in forecast.intervals:
        day = interval.start.astimezone(zone).date()
        if first_day <= day <= last_day:
            rows.append(
                {
                    "config_key": key,
                    "local_date": day.isoformat(),
                    "interval_start": interval.start.isoformat(),
                    "interval_end": interval.end.isoformat(),
                    "energy_wh": interval.energy_wh,
                    "timezone": config.timezone or "UTC",
                    "fetched_at": forecast.created_at.isoformat(),
                }
            )
    if not solar_refresh_plan(rows, now, config.timezone).complete:
        message = "The solar forecast does not cover tomorrow"
        log.warning("Daily solar forecast retrieval failed: %s", message)
        await _record(store, SOLAR_STATUS_KEY, key, False, message)
        return False
    if not await _write_if_current(
        store, is_current, store.replace_solar_forecast_intervals, rows
    ):
        log.info(
            "Weather configuration changed during the refresh; discarding its result"
        )
        return False
    if on_stored is not None:
        on_stored()
    await _record(store, SOLAR_STATUS_KEY, key, True)
    return True


async def spot_refresh_tick(
    store: AdminStore,
    provider: SpotPriceProvider,
    config: SpotMarketConfig,
    now: datetime,
    backoff: RetryBackoff,
    is_current: Callable[[], bool] | None = None,
    zone_cache=None,
    active: Callable[[], bool] | None = None,
    on_stored: Callable[[], None] | None = None,
) -> timedelta:
    """One evaluation of the stored spot rows: retrieve if due, return how long to wait."""
    if not config.enabled:
        return MAX_IDLE_WAIT
    key = spot_config_key(config)
    rows = await _store_thread(store.get_spot_market_intervals, key)
    return await _evaluate(
        store,
        SPOT_STATUS_KEY,
        key,
        spot_refresh_plan(rows, now),
        backoff,
        now,
        lambda: refresh_spot_market(
            store,
            provider,
            config,
            is_current,
            now=now,
            zone_cache=zone_cache,
            on_stored=on_stored,
        ),
        active,
    )


async def solar_refresh_tick(
    store: AdminStore,
    provider,
    config: WeatherConfig,
    now: datetime,
    backoff: RetryBackoff,
    is_current: Callable[[], bool] | None = None,
    active: Callable[[], bool] | None = None,
    on_stored: Callable[[], None] | None = None,
) -> timedelta:
    """One evaluation of the stored solar rows: retrieve if due, return how long to wait."""
    if not config.is_forecastable():
        return MAX_IDLE_WAIT
    key = solar_config_key(config)
    zone = ZoneInfo(config.timezone or "UTC")
    today = now.astimezone(zone).date()
    rows = await _store_thread(
        store.get_solar_forecast_intervals,
        key,
        today.isoformat(),
        (today + timedelta(days=1)).isoformat(),
    )
    return await _evaluate(
        store,
        SOLAR_STATUS_KEY,
        key,
        solar_refresh_plan(rows, now, config.timezone),
        backoff,
        now,
        lambda: refresh_solar_forecast(
            store, provider, config, is_current, now=now, on_stored=on_stored
        ),
        active,
    )


async def _run_loop(
    tick,
    status_key: str,
    store: AdminStore,
    config_of,
    key_of,
    event: asyncio.Event,
    activity: EnergyActivity | None = None,
) -> None:
    backoff = RetryBackoff()
    while True:
        if activity is not None and not activity.any_manual_active():
            # No inverter is in manual mode: rest without any evaluation, retry or provider call
            # until a mode change. off and external do not fetch the shared spot/weather inputs; a
            # configuration save only sets ``event``, which the next evaluation still sees.
            await activity.wait_until_any_manual()
            backoff.reset()
            continue
        config = config_of()
        key = key_of(config)
        try:
            wait = await tick(config, backoff)
        except Exception as exc:
            log.exception("Unexpected background forecast refresh failure")
            await _record(store, status_key, key, False, _error_text(exc))
            wait = backoff.failed(datetime.now(UTC))
        try:
            await asyncio.wait_for(event.wait(), timeout=wait.total_seconds())
        except TimeoutError:
            continue  # leave the event alone: a signal set after the timeout must survive
        event.clear()  # no await since the wake-up, so no signal can slip in unseen
        backoff.reset()  # a saved configuration is a user action: evaluate again right away


async def run_spot_market_refresh_loop(
    store: AdminStore,
    provider: SpotPriceProvider,
    app,
    event: asyncio.Event,
    clock: Clock | None = None,
    activity: EnergyActivity | None = None,
    on_stored: Callable[[], None] | None = None,
) -> None:
    """``on_stored`` is told whenever new rows were stored; it must only signal, never block."""
    clock = clock or SystemClock()

    def tick(config, backoff):
        key = spot_config_key(config)
        return spot_refresh_tick(
            store,
            provider,
            config,
            clock.now(),
            backoff,
            lambda: spot_config_key(app.state.spotmarket_config) == key,
            getattr(app.state, "spotmarket_zone_cache", None),
            activity.any_manual_active if activity is not None else None,
            on_stored,
        )

    await _run_loop(
        tick,
        SPOT_STATUS_KEY,
        store,
        lambda: app.state.spotmarket_config,
        spot_config_key,
        event,
        activity,
    )


async def run_solar_refresh_loop(
    store: AdminStore,
    provider,
    app,
    event: asyncio.Event,
    clock: Clock | None = None,
    activity: EnergyActivity | None = None,
    on_stored: Callable[[], None] | None = None,
) -> None:
    """``on_stored`` is told whenever new rows were stored; it must only signal, never block."""
    clock = clock or SystemClock()

    def tick(config, backoff):
        key = solar_config_key(config)
        return solar_refresh_tick(
            store,
            provider,
            config,
            clock.now(),
            backoff,
            lambda: solar_config_key(app.state.weather_config) == key,
            activity.any_manual_active if activity is not None else None,
            on_stored,
        )

    await _run_loop(
        tick,
        SOLAR_STATUS_KEY,
        store,
        lambda: app.state.weather_config,
        solar_config_key,
        event,
        activity,
    )
