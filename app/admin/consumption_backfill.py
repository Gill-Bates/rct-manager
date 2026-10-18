#!/usr/bin/env python3
#
# app/admin/consumption_backfill.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Daily consumption reconciliation from the inverter's own day history.

``ConsumptionHistoryBackfillService`` is a background service outside the frame receiver: it asks a
vendor-neutral :class:`DailyConsumptionHistoryProvider` for completed local calendar days that the
local history lacks, and stores them as day totals in ``consumption_daily_history``. It runs once
shortly after start, again after a device came back from an outage, and otherwise about once a day.
A day total is never spread over 15-minute buckets and never reaches the forecast, which learns
from the local 15-minute measurements only (``app.optimizer.consumption``).
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.admin.forecast_refresh import _store_thread
from app.admin.store import AdminStore
from app.clock import Clock
from app.errors import DeviceApiError
from app.gateway.history import DailyConsumptionHistoryProvider
from app.optimizer.consumption import rows_to_history
from app.optimizer.daily import (
    SOURCE_INVERTER_HISTORY,
    DailyConsumption,
    DailySource,
    local_day_bounds,
    resolve_daily_consumption,
)

log = logging.getLogger(__name__)

# The history window: the last this-many COMPLETED local calendar days. The device may hold fewer.
WINDOW_DAYS = 60
# Readings requested per query, a pause between queries (the device queue is shared with live reads),
# and a hard cap on queries per run.
QUERY_LIMIT = 64
MAX_QUERIES_PER_RUN = 8
PAUSE_BETWEEN_QUERIES_SECONDS = 2.0
# Readiness is checked this often (no device I/O); a successful run repeats after a day, a failed one
# after the retry interval, a reconnect re-arms the run at once.
CHECK_INTERVAL_SECONDS = 900.0
STARTUP_DELAY_SECONDS = 60.0
SUCCESS_INTERVAL = timedelta(hours=24)
RETRY_INTERVAL = timedelta(hours=6)


def resolve_zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def load_daily_consumption(
    store: AdminStore,
    device_id: str,
    first: date,
    last: date,
    *,
    zone_name: str,
    now: datetime,
) -> list[DailyConsumption]:
    """Resolve the days from the local store only; blocking SQLite, call from a worker thread."""
    zone = resolve_zone(zone_name)
    today = now.astimezone(zone).date()
    start = local_day_bounds(first, zone)[0].isoformat()
    end = local_day_bounds(last, zone)[1].isoformat()
    history = rows_to_history(
        store.get_consumption_intervals(device_id, start, end),
        store.get_consumption_meter_intervals(device_id, start, end),
    )
    stored = store.get_consumption_daily(device_id, first.isoformat(), last.isoformat())
    # A daily value is valid only together with the zone its local_date was interpreted in. A row
    # stored under a different zone must be re-reconciled under the current zone, not silently reused;
    # dropping it here leaves the day UNAVAILABLE so the backfill fetches it afresh.
    inverter = {
        date.fromisoformat(row["local_date"]): float(row["energy_wh"])
        for row in stored
        if row.get("timezone") == zone_name
    }
    return resolve_daily_consumption(
        first, last, today=today, zone=zone, history=history, inverter_days=inverter
    )


class ConsumptionHistoryBackfillService:
    def __init__(
        self,
        provider: DailyConsumptionHistoryProvider,
        store: AdminStore,
        clock: Clock,
        timezone: Callable[[], str],
        device_ids: Callable[[], Sequence[str]],
        is_ready: Callable[[str], bool],
    ) -> None:
        self._provider = provider
        self._store = store
        self._clock = clock
        self._timezone = timezone
        self._device_ids = device_ids
        self._is_ready = is_ready
        self._next_run: dict[str, datetime] = {}
        self._was_ready: dict[str, bool] = {}

    async def run(self) -> None:
        """Check readiness periodically and backfill when due; never raises except on cancellation."""
        await self._clock.sleep(STARTUP_DELAY_SECONDS)
        while True:
            try:
                await self.run_due_once()
            except Exception:
                log.exception("Daily consumption backfill cycle failed")
            await self._clock.sleep(CHECK_INTERVAL_SECONDS)

    async def run_due_once(self) -> None:
        now = self._clock.now()
        for device_id in sorted(self._device_ids()):
            ready = self._is_ready(device_id)
            reconnected = ready and self._was_ready.get(device_id) is False
            self._was_ready[device_id] = ready
            if not ready:
                continue
            due = self._next_run.get(device_id)
            if due is not None and now < due and not reconnected:
                continue
            ok = await self.backfill_device(device_id)
            self._next_run[device_id] = self._clock.now() + (
                SUCCESS_INTERVAL if ok else RETRY_INTERVAL
            )

    async def backfill_device(self, device_id: str) -> bool:
        """Fill missing completed days of the 60-day window; ``False`` when the device could not answer."""
        try:
            return await self._backfill(device_id)
        except asyncio.CancelledError:
            raise
        except DeviceApiError as exc:
            log.info(
                "Inverter day history of %s not available (%s); retrying later",
                device_id,
                exc.code,
            )
        except Exception:
            log.exception("Inverter day history backfill of %s failed", device_id)
        return False

    async def _backfill(self, device_id: str) -> bool:
        zone_name = self._timezone() or "UTC"
        zone = resolve_zone(zone_name)
        now = self._clock.now()
        today = now.astimezone(zone).date()
        first, last = today - timedelta(days=WINDOW_DAYS), today - timedelta(days=1)
        resolved = await _store_thread(
            partial(
                load_daily_consumption,
                self._store,
                device_id,
                first,
                last,
                zone_name=zone_name,
                now=now,
            )
        )
        missing = {d.date for d in resolved if d.source is DailySource.UNAVAILABLE}
        if not missing:
            return True
        found: dict[date, float] = {}
        before = local_day_bounds(today, zone)[
            0
        ]  # the running day is never a completed day
        for query in range(MAX_QUERIES_PER_RUN):
            if query:
                await self._clock.sleep(
                    PAUSE_BETWEEN_QUERIES_SECONDS
                )  # frees the shared device queue
            readings = await self._provider.daily_consumption(
                device_id, before=before, limit=QUERY_LIMIT
            )
            fresh = [
                r for r in readings if r.local_date < before.astimezone(zone).date()
            ]
            if not fresh:
                break
            found.update(
                (r.local_date, r.energy_wh)
                for r in fresh
                if first <= r.local_date <= last
            )
            oldest = min(r.local_date for r in fresh)
            if oldest <= first or not (missing - found.keys()):
                break
            before = local_day_bounds(oldest, zone)[0]
        rows = [
            {
                "local_date": day.isoformat(),
                "energy_wh": wh,
                "source": SOURCE_INVERTER_HISTORY,
                "timezone": zone_name,
                "fetched_at": datetime.now(UTC).isoformat(),
            }
            for day, wh in sorted(found.items())
            if day in missing
        ]
        await _store_thread(self._store.upsert_consumption_daily, device_id, rows)
        log.info(
            "Inverter day history of %s: %d of %d missing day(s) stored",
            device_id,
            len(rows),
            len(missing),
        )
        return True
