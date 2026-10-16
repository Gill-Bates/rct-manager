#!/usr/bin/env python3
#
# app/gateway/history.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Vendor-neutral port for completed daily consumption kept by the device itself.

No object id, frame or protocol type appears here; the consumption layer depends on this port only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DailyConsumptionReading:
    local_date: date  # calendar day in the site's time zone
    energy_wh: float


class DailyConsumptionHistoryProvider(Protocol):
    """Read-only query for per-day consumption totals the device logged before ``before``."""

    async def daily_consumption(
        self, device_id: str, *, before: datetime, limit: int
    ) -> Sequence[DailyConsumptionReading]:
        """At most ``limit`` readings, newest first; raises a ``DeviceApiError`` on device trouble."""
        ...
