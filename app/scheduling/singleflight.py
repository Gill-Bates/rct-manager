#!/usr/bin/env python3
#
# app/scheduling/singleflight.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""At most one caller-triggered, cache-eligible read per key (Requirement 15.12)."""

import asyncio
from collections.abc import Awaitable, Callable

type SingleFlightKey = tuple[str, str]  # (device id, metric name)


class SingleFlight:
    """Shares one running operation between concurrent callers."""

    def __init__(self) -> None:
        self._inflight: dict[SingleFlightKey, asyncio.Task] = {}

    def inflight(self, key: SingleFlightKey) -> bool:
        return key in self._inflight

    async def run[T](self, key: SingleFlightKey, factory: Callable[[], Awaitable[T]]) -> T:
        """Keep the shared operation alive when any single waiter is cancelled."""
        running = self._inflight.get(key)
        if running is None:

            async def perform() -> T:
                try:
                    return await factory()
                finally:
                    self._inflight.pop(key, None)

            running = asyncio.create_task(perform())
            self._inflight[key] = running
            # Retrieve the failure even when every HTTP waiter has left.
            running.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return await asyncio.shield(running)
