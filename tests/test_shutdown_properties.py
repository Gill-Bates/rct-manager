#!/usr/bin/env python3
#
# tests/test_shutdown_properties.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Property 16: the shutdown deadline is one overall deadline."""

import asyncio
from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import DeviceKey, EndpointKey
from app.errors import DeviceApiError
from app.protocol.frames import Frame
from app.protocol.types import Command
from app.scheduling.periodic import PeriodicManager
from app.scheduling.serializer import AccessSerializer
from app.scheduling.shutdown import ShutdownCoordinator, ShutdownPhase
from app.transport.endpoint import EndpointConfig, TransportEndpoint
from app.transport.types import TransactionOrigin, TransactionRequest
from tests.conftest import ManualClock
from tests.fakes import FakeNetwork

STEP = 0.25


async def _drive(clock: ManualClock, task: asyncio.Task, limit: float) -> None:
    waited = 0.0
    while not task.done():
        clock.advance(STEP)
        waited += STEP
        for _ in range(8):
            await asyncio.sleep(0)
        assert waited <= limit, "shutdown overran its deadline"


# Feature: rct-manager, Property 16: the shutdown deadline is one overall deadline
@settings(max_examples=100, deadline=None)
@given(
    delay=st.one_of(st.none(), st.floats(0.0, 30.0)),  # None means the device never answers
    queued=st.integers(0, 6),
    periodic=st.integers(0, 4),
    grace=st.integers(2, 20),
    reserve=st.integers(0, 1),
)
def test_shutdown_never_exceeds_the_deadline(delay, queued: int, periodic: int, grace: int, reserve: int) -> None:
    async def scenario() -> tuple[int, float, ShutdownCoordinator, AccessSerializer]:
        clock = ManualClock()
        key = EndpointKey("10.0.0.5", 8899)
        net = FakeNetwork(clock, behavior=lambda f: "ignore" if delay is None else ("delay", delay))
        cfg = EndpointConfig(response_timeout_seconds=1000, min_interval=timedelta(seconds=0.3))
        endpoint = TransportEndpoint("endpoint-1", key, cfg, clock, connector=net.connect)

        async def handler(request):
            return await endpoint.execute(request)

        serializer = AccessSerializer(endpoint, handler, queue_max_length=10, queue_max_wait_seconds=1000)
        serializer.start()
        device = DeviceKey(key)
        managers = []
        for _ in range(periodic):
            manager = PeriodicManager(endpoint, serializer, device, [1], 30, clock)
            manager.registrations = 1
            managers.append(manager)
        callers = [
            asyncio.create_task(
                serializer.submit(TransactionRequest(device, Frame(Command.READ, i), TransactionOrigin.CALLER, "read"))
            )
            for i in range(queued)
        ]
        await asyncio.sleep(0)
        coordinator = ShutdownCoordinator(
            clock,
            grace_seconds=grace,
            periodic_reserve_seconds=reserve,
            serializers=[serializer],
            periodic=managers,
            endpoints=[endpoint],
        )
        start = clock.monotonic()
        run = asyncio.create_task(coordinator.run())
        await _drive(clock, run, grace + 4 * STEP)
        await asyncio.gather(*callers, return_exceptions=True)
        return run.result(), clock.monotonic() - start, coordinator, serializer

    code, elapsed, coordinator, serializer = asyncio.run(scenario())
    assert code == 0
    assert elapsed <= grace + 2 * STEP + 1e-9  # simulation granularity
    assert coordinator.plan.phase is ShutdownPhase.FINALIZE
    assert not serializer.accepting()


def test_phases_and_new_work_is_refused() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        key = EndpointKey("h", 1)
        endpoint = TransportEndpoint("endpoint-1", key, EndpointConfig(), clock, connector=FakeNetwork(clock).connect)
        serializer = AccessSerializer(endpoint, lambda r: None)
        serializer.start()
        coordinator = ShutdownCoordinator(
            clock,
            grace_seconds=5,
            periodic_reserve_seconds=1,
            serializers=[serializer],
            periodic=[],
            endpoints=[endpoint],
        )
        run = asyncio.create_task(coordinator.run())
        await _drive(clock, run, 5)
        request = TransactionRequest(DeviceKey(key), Frame(Command.READ, 1), TransactionOrigin.CALLER, "read")
        with pytest.raises(DeviceApiError):
            await serializer.submit(request)
        assert run.result() == 0

    asyncio.run(scenario())


def test_hanging_close_is_aborted_at_the_deadline() -> None:
    class HangingEndpoint:
        aborted = False

        async def close(self) -> None:
            await asyncio.Event().wait()

        def abort(self) -> None:
            self.aborted = True

    async def scenario() -> tuple[float, bool, int]:
        clock = ManualClock()
        stuck = HangingEndpoint()
        coordinator = ShutdownCoordinator(
            clock, grace_seconds=5, periodic_reserve_seconds=1, serializers=[], periodic=[], endpoints=[stuck]
        )
        start = clock.monotonic()
        run = asyncio.create_task(coordinator.run())
        await _drive(clock, run, 5 + 2 * STEP)
        return clock.monotonic() - start, stuck.aborted, run.result()

    elapsed, aborted, code = asyncio.run(scenario())
    assert (aborted, code) == (True, 0)
    assert elapsed <= 5 + STEP + 1e-9


def test_running_transaction_does_not_starve_the_periodic_reserve() -> None:
    """P2: a running READ must not hold the transaction lock into the periodic-teardown window."""

    async def scenario() -> tuple[float, int, int]:
        clock = ManualClock()
        key = EndpointKey("10.0.0.5", 8899)
        net = FakeNetwork(clock, behavior=lambda f: "ignore" if f.command is Command.READ else "respond")
        # A virtual deadline must not race the fake WRITE's real-time response timeout.
        net.answer_writes = True
        cfg = EndpointConfig(response_timeout_seconds=1000, min_interval=timedelta(seconds=0))
        endpoint = TransportEndpoint("endpoint-1", key, cfg, clock, connector=net.connect)

        async def handler(request):
            return await endpoint.execute(request)

        serializer = AccessSerializer(endpoint, handler, queue_max_length=10, queue_max_wait_seconds=1000)
        serializer.start()
        device = DeviceKey(key)
        manager = PeriodicManager(endpoint, serializer, device, [1], 30, clock)
        manager.registrations = 1
        manager.period_enabled = True
        running = asyncio.create_task(
            serializer.submit(TransactionRequest(device, Frame(Command.READ, 1), TransactionOrigin.CALLER, "read"))
        )
        for _ in range(8):
            await asyncio.sleep(0)  # let the worker pick the item up and reach the response wait
        coordinator = ShutdownCoordinator(
            clock,
            grace_seconds=10,
            periodic_reserve_seconds=3,
            serializers=[serializer],
            periodic=[manager],
            endpoints=[endpoint],
        )
        start = clock.monotonic()
        run = asyncio.create_task(coordinator.run())
        await _drive(clock, run, 10 + 4 * STEP)
        await asyncio.gather(running, return_exceptions=True)
        return clock.monotonic() - start, run.result(), coordinator.deregistered

    elapsed, code, deregistered = asyncio.run(scenario())
    assert code == 0
    assert elapsed <= 10 + 2 * STEP + 1e-9
    # Without cancelling the running transaction, teardown() never gets the transaction lock
    # before the overall deadline and deregistered stays 0.
    assert deregistered == 1


def test_failed_teardown_is_not_counted_as_deregistered() -> None:
    class FailingManager:
        registrations = 2

        async def teardown(self) -> bool:
            return False

    async def scenario() -> int:
        clock = ManualClock()
        coordinator = ShutdownCoordinator(
            clock,
            grace_seconds=5,
            periodic_reserve_seconds=1,
            serializers=[],
            periodic=[FailingManager()],
            endpoints=[],
        )
        run = asyncio.create_task(coordinator.run())
        await _drive(clock, run, 5 + 2 * STEP)
        return coordinator.deregistered

    assert asyncio.run(scenario()) == 0
