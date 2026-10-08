#!/usr/bin/env python3
#
# app/scheduling/shutdown.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shutdown phases with one overall deadline (Requirement 27)."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.clock import Clock
from app.scheduling.periodic import PeriodicManager
from app.scheduling.retry import cancel_and_wait
from app.scheduling.serializer import AccessSerializer
from app.transport.endpoint import TransportEndpoint

log = logging.getLogger(__name__)


class ShutdownPhase(StrEnum):
    RUNNING = "running"
    RESTORE_DISPATCH = "restore_dispatch"
    STOP_ACCEPTING = "stop_accepting"
    DRAIN_WORK = "drain_work"
    DRAIN_PERIODIC = "drain_periodic"
    FINALIZE = "finalize"


@dataclass(slots=True)
class ShutdownPlan:
    signal_at: datetime
    deadline: float  # monotonic signal time + SHUTDOWN_GRACE_SECONDS
    work_deadline: float  # monotonic deadline - SHUTDOWN_PERIODIC_RESERVE_SECONDS
    phase: ShutdownPhase = ShutdownPhase.RUNNING


class ShutdownCoordinator:
    def __init__(
        self,
        clock: Clock,
        *,
        grace_seconds: float,
        periodic_reserve_seconds: float,
        serializers: Sequence[AccessSerializer],
        periodic: Sequence[PeriodicManager],
        endpoints: Sequence[TransportEndpoint],
    ) -> None:
        self._clock = clock
        self._grace = grace_seconds
        self._reserve = periodic_reserve_seconds
        self._serializers = serializers
        self._periodic = periodic
        self._endpoints = endpoints
        self.plan: ShutdownPlan | None = None
        self.aborted_transactions = 0
        self.deregistered = 0
        self._restore_dispatch: Callable[[], Awaitable[None]] | None = None

    def set_dispatch_restore(self, restore: Callable[[], Awaitable[None]]) -> None:
        """Install the battery restore hook before shutdown begins."""
        if self.plan is not None:
            raise RuntimeError("shutdown already started")
        self._restore_dispatch = restore

    async def _within(self, awaitable: Awaitable[object], deadline: float) -> bool:
        """Run until done or the monotonic deadline; the clock port keeps this testable."""
        # Normalized first: a gather() already has running children that an expired deadline must
        # cancel instead of leaving them behind.
        task = asyncio.ensure_future(awaitable)
        remaining = deadline - self._clock.monotonic()
        if remaining <= 0:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return False
        timer = asyncio.create_task(self._clock.sleep(remaining))
        try:
            await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            await cancel_and_wait(task, timer)
            raise
        timer.cancel()
        if task.done():
            if task.cancelled():
                log.warning("Shutdown step was cancelled")
                return False
            if (exc := task.exception()) is not None:
                log.warning("Shutdown step failed: %s", type(exc).__name__)
                return False
            return task.result() is not False  # e.g. PeriodicManager.teardown() reports failure this way
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return False

    async def _stop_all(self) -> None:
        async def stop(serializer: AccessSerializer) -> None:
            self.aborted_transactions += await serializer.stop()

        await asyncio.gather(*(stop(s) for s in self._serializers))

    async def _close_all(self) -> None:
        await asyncio.gather(*(e.close() for e in self._endpoints))

    async def run(self) -> int:
        start = self._clock.monotonic()
        deadline = start + self._grace
        plan = self.plan = ShutdownPlan(self._clock.now(), deadline, deadline - self._reserve)

        if self._restore_dispatch is not None:
            plan.phase = ShutdownPhase.RESTORE_DISPATCH
            if not await self._within(self._restore_dispatch(), plan.work_deadline):
                log.warning("Battery dispatch restore did not finish before the work deadline")

        plan.phase = ShutdownPhase.STOP_ACCEPTING
        for serializer in self._serializers:
            serializer.stop_accepting()

        plan.phase = ShutdownPhase.DRAIN_WORK
        drained = await self._within(asyncio.gather(*(s.drain() for s in self._serializers)), plan.work_deadline)
        if not drained:
            log.warning("Work deadline reached with transactions still pending")
            # Queued work, and any transaction still running, must not starve the periodic
            # teardown of the transaction lock: a running READ can hold it for the whole
            # response_timeout_seconds * (READ_RETRIES + 1) window otherwise. stop() cancels the
            # worker (queued and running alike) but only drops the current connection, it does not
            # close the endpoint, so PeriodicManager.teardown() below can still reconnect and send
            # pas.period = 0 on it.
            self.aborted_transactions += sum(await asyncio.gather(*(s.stop() for s in self._serializers)))

        plan.phase = ShutdownPhase.DRAIN_PERIODIC
        skipped = 0
        for manager in self._periodic:
            count = manager.registrations
            if count == 0 and not manager.period_enabled:  # a set interval must be reset as well
                continue
            if self._clock.monotonic() >= deadline:
                skipped += count
                continue
            if await self._within(manager.teardown(), deadline):
                self.deregistered += count
            else:
                skipped += count
                log.warning("Periodic teardown failed or timed out for a device")
        if skipped:
            log.warning("Periodic requests left registered: %d", skipped)

        plan.phase = ShutdownPhase.FINALIZE
        if not await self._within(self._stop_all(), deadline):
            log.warning("Serializer stop did not finish before the deadline")
        if not await self._within(self._close_all(), deadline):
            log.warning("Endpoint close did not finish before the deadline: aborting transports")
            for endpoint in self._endpoints:
                endpoint.abort()
        log.info(
            "Shutdown finished: duration=%.2fs phase=%s deregistered=%d aborted=%d",
            self._clock.monotonic() - start,
            plan.phase.value,
            self.deregistered,
            self.aborted_transactions,
        )
        return 0
