#!/usr/bin/env python3
#
# app/scheduling/heartbeat.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Heartbeat: a cheap read per device, skipped whenever the device already proved it is alive."""

import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum

from app.clock import Clock
from app.transport.counters import DeviceCounters

log = logging.getLogger(__name__)


class LivenessSource(StrEnum):
    TRANSACTION = "transaction"
    PERIODIC = "periodic"
    HEARTBEAT = "heartbeat"


class Heartbeat:
    def __init__(
        self,
        counters: DeviceCounters,
        read: Callable[[], Awaitable[bool]],
        clock: Clock,
        *,
        interval_seconds: float,
        failure_threshold: int,
    ) -> None:
        self._counters = counters
        self._read = read
        self._clock = clock
        self._interval = interval_seconds
        self._threshold = failure_threshold
        self.consecutive_failures = 0
        self.liveness_source: LivenessSource | None = None

    @property
    def unreachable(self) -> bool:
        return self.consecutive_failures >= self._threshold

    async def tick(self) -> None:
        horizon = self._clock.monotonic() - self._interval
        success = self._counters.last_success_monotonic
        periodic = self._counters.last_periodic_monotonic
        if success is not None and success >= horizon:
            self.liveness_source = LivenessSource.TRANSACTION
            self.consecutive_failures = 0
        elif periodic is not None and periodic >= horizon:
            self.liveness_source = LivenessSource.PERIODIC
            self.consecutive_failures = 0
        elif await self._read():
            self.liveness_source = LivenessSource.HEARTBEAT
            self.consecutive_failures = 0
        else:
            # No source proved the device alive this tick; the stale source would otherwise be
            # reported as the current one while the device is DEGRADED or UNREACHABLE.
            self.liveness_source = None
            self.consecutive_failures += 1

    async def run(self) -> None:
        while True:
            await self._clock.sleep(self._interval)
            try:
                await self.tick()
            except Exception:  # keep the daemon task alive on a single bad reading
                log.exception("Heartbeat tick failed")
                self.liveness_source = None
                self.consecutive_failures += 1
