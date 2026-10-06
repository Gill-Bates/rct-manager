#!/usr/bin/env python3
#
# app/clock.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Time source port; deadlines use the monotonic axis only."""

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Injectable time source so pacing, caching and deadlines are testable."""

    def now(self) -> datetime:
        """Timezone-aware UTC wall-clock time."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds for deadlines, ages and windows."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait for the given number of seconds."""
        ...


class SystemClock:
    """Runtime implementation backed by the system clocks."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
