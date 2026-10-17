#!/usr/bin/env python3
#
# app/gateway/rct_history.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""RCT implementation of the daily-consumption history port (logger.day_eload_log_ts).

Hardware samples confirm Wh and show that day-entry timestamps encode local wall-clock dates.
The query boundary's wire-time interpretation is still unverified, so backfill stays disabled.
"""

import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.gateway.history import DailyConsumptionReading
from app.gateway.rct import RctGateway

DAY_LOAD_LOG_METRIC = "logger_day_eload_log_ts"
# Plausibility ceiling for one household day; anything above is a corrupt entry, not consumption.
MAX_PLAUSIBLE_DAY_WH = 1_000_000.0

log = logging.getLogger(__name__)


class RctDailyConsumptionHistory:
    def __init__(self, gateway: RctGateway, timezone: Callable[[], str]) -> None:
        self._gateway = gateway
        self._timezone = timezone
        self._warned_dates: set[tuple[str, object]] = (
            set()
        )  # one log line per device and local date

    async def daily_consumption(
        self, device_id: str, *, before: datetime, limit: int
    ) -> Sequence[DailyConsumptionReading]:
        points = await self._gateway.query_history_series(
            device_id, DAY_LOAD_LOG_METRIC, before
        )
        try:
            zone = ZoneInfo(self._timezone() or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("UTC")
        by_date: dict = {}
        for point in points:
            if (
                not math.isfinite(point.value)
                or not 0.0 <= point.value <= MAX_PLAUSIBLE_DAY_WH
            ):
                continue
            # The day logger encodes local wall-clock fields in a UTC-labelled timestamp.
            day = point.timestamp.date()
            if day >= before.astimezone(zone).date():
                continue
            if day in by_date and (device_id, day) not in self._warned_dates:
                self._warned_dates.add((device_id, day))
                # The device can report multiple values for one day; keep the final entry.
                log.warning(
                    "Inverter day history of %s has several logger points for %s; the last one is used",
                    device_id,
                    day,
                )
            by_date[day] = float(point.value)
        newest_first = sorted(by_date.items(), reverse=True)[: max(0, limit)]
        return [DailyConsumptionReading(day, wh) for day, wh in newest_first]


YEAR_ENERGY_LOG_METRIC = "logger_year_eac_log_ts"
MONTH_ENERGY_LOG_METRIC = "logger_month_eac_log_ts"
# Entries older than this are an unset device clock (epoch 0), not a commissioning date.
MIN_COMMISSIONING_DATE = date(2010, 1, 1)
MIN_PLAUSIBLE_COMMISSIONING = datetime.combine(MIN_COMMISSIONING_DATE, time.min, UTC)
MAX_COMMISSIONING_PAGES = 24  # hard cap per logger, whatever the device answers


class RctCommissioningHistory:
    """Earliest logged AC energy of the inverter, as a proxy for its commissioning date.

    UNVERIFIED without hardware: that logger_year_eac_log_ts / logger_month_eac_log_ts hold the
    inverter's AC energy per year / month, that a query "before T" returns the entries preceding T,
    that paging backward ends with an empty or not-older reply, and whether an entry timestamp marks
    the start or the end of its period. The earliest non-zero entry can therefore be off by up to a
    period, and it is no more than the start of the retained log when the device has dropped older
    data. An operator-entered date always overrides it.
    """

    def __init__(
        self,
        gateway: RctGateway,
        pause: Callable[[float], Awaitable[object]],
        pause_seconds: float = 2.0,
    ) -> None:
        self._gateway = gateway
        self._pause = pause  # frees the shared device queue between pages
        self._pause_seconds = pause_seconds

    async def earliest_energy(
        self, device_id: str, *, now: datetime
    ) -> datetime | None:
        """Earliest timestamp with energy > 0 over both loggers; None when they hold none.

        A failure of either logger raises, so a partial answer is never taken for the first date.
        """
        found = [
            await self._earliest_in(device_id, metric, now)
            for metric in (YEAR_ENERGY_LOG_METRIC, MONTH_ENERGY_LOG_METRIC)
        ]
        dates = [stamp for stamp in found if stamp is not None]
        return min(dates) if dates else None

    async def _earliest_in(
        self, device_id: str, metric: str, now: datetime
    ) -> datetime | None:
        before = now
        earliest: datetime | None = None
        for page in range(MAX_COMMISSIONING_PAGES):
            if page:
                await self._pause(self._pause_seconds)
            points = await self._gateway.query_history_series(device_id, metric, before)
            older = [
                point
                for point in points
                if point.timestamp < before and math.isfinite(point.value)
            ]
            if not older:
                break  # nothing older than the last page: the log is exhausted
            for point in older:
                if point.value > 0 and point.timestamp >= MIN_PLAUSIBLE_COMMISSIONING:
                    earliest = (
                        point.timestamp
                        if earliest is None
                        else min(earliest, point.timestamp)
                    )
            before = min(point.timestamp for point in older)
        else:
            log.warning(
                "Inverter %s: %s still had older entries after %d pages",
                device_id,
                metric,
                MAX_COMMISSIONING_PAGES,
            )
        return earliest
