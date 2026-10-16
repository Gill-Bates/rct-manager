#!/usr/bin/env python3
#
# app/gateway/rct_history.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""RCT implementation of the daily-consumption history port (logger.day_eload_log_ts).

UNVERIFIED without hardware: the unit of the logged value (Wh is assumed, matching the cumulative
load counter) and which calendar day an entry timestamp describes (the local date of the timestamp
is assumed). The productive backfill that uses this class is therefore disabled by default.
"""

import math
from collections.abc import Callable, Sequence
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.gateway.history import DailyConsumptionReading
from app.gateway.rct import RctGateway

DAY_LOAD_LOG_METRIC = "logger_day_eload_log_ts"
# Plausibility ceiling for one household day; anything above is a corrupt entry, not consumption.
MAX_PLAUSIBLE_DAY_WH = 1_000_000.0


class RctDailyConsumptionHistory:
    def __init__(self, gateway: RctGateway, timezone: Callable[[], str]) -> None:
        self._gateway = gateway
        self._timezone = timezone

    async def daily_consumption(
        self, device_id: str, *, before: datetime, limit: int
    ) -> Sequence[DailyConsumptionReading]:
        points = await self._gateway.query_history_series(device_id, DAY_LOAD_LOG_METRIC, before)
        try:
            zone = ZoneInfo(self._timezone() or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("UTC")
        by_date: dict = {}
        for point in points:
            if not math.isfinite(point.value) or not 0.0 <= point.value <= MAX_PLAUSIBLE_DAY_WH:
                continue
            if point.timestamp >= before:
                continue  # the query asks for data before this instant
            by_date[point.timestamp.astimezone(zone).date()] = float(point.value)
        newest_first = sorted(by_date.items(), reverse=True)[: max(0, limit)]
        return [DailyConsumptionReading(day, wh) for day, wh in newest_first]
