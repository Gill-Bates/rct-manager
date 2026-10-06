#!/usr/bin/env python3
#
# tests/test_singleflight_properties.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Property 15: N concurrent requests share exactly one read."""

import asyncio
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import DeviceKey, EndpointKey
from app.protocol.frames import Frame
from app.protocol.types import Command
from app.scheduling.serializer import AccessSerializer
from app.scheduling.singleflight import SingleFlight
from app.transport.types import (
    SendOutcome,
    TransactionOrigin,
    TransactionRequest,
    TransactionResult,
)


def _request(recheck: bool = False) -> TransactionRequest:
    return TransactionRequest(
        DeviceKey(EndpointKey("h", 1)), Frame(Command.READ, 1), TransactionOrigin.CALLER, "read", recheck_cache=recheck
    )


# Feature: rct-rest-api, Property 15: single flight, N concurrent requests share exactly one read
@settings(max_examples=100, deadline=None)
@given(st.integers(1, 12), st.sets(st.integers(0, 11), max_size=12), st.booleans())
def test_n_callers_share_one_read(n: int, cancelled: set[int], cancel_first: bool) -> None:
    async def scenario() -> tuple[int, list[object]]:
        flight = SingleFlight()
        reads = 0
        release = asyncio.Event()

        async def device_read() -> int:
            nonlocal reads
            reads += 1
            await release.wait()
            return 42

        tasks = [asyncio.create_task(flight.run(("wr1", "p_ac"), device_read)) for _ in range(n)]
        await asyncio.sleep(0)
        doomed = {i for i in cancelled if i < n} | ({0} if cancel_first else set())
        for i in doomed:
            tasks[i].cancel()
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        survivors = [r for i, r in enumerate(results) if i not in doomed]
        doomed_results = [r for i, r in enumerate(results) if i in doomed]
        assert all(r == 42 for r in survivors)
        # Every cancelled caller must actually observe its own cancellation, never a swallowed
        # cancellation that returns the shared result instead (Finding 8). This also makes the
        # n == len(doomed) case non-vacuous: survivors being empty no longer lets `all(...)` pass
        # for free, because this loop still runs over doomed_results.
        assert len(doomed_results) == len(doomed)
        for result in doomed_results:
            assert isinstance(result, asyncio.CancelledError)
        for _ in range(5):
            await asyncio.sleep(0)  # let the shared task finish when every waiter left
        assert not flight.inflight(("wr1", "p_ac"))
        return reads, results

    reads, _ = asyncio.run(scenario())
    assert reads == 1


def test_flight_failure_reaches_every_waiter() -> None:
    async def scenario() -> list[object]:
        flight = SingleFlight()

        async def boom() -> int:
            await asyncio.sleep(0)
            raise ValueError("device error")

        return await asyncio.gather(*(flight.run(("a", "b"), boom) for _ in range(3)), return_exceptions=True)

    assert all(isinstance(r, ValueError) for r in asyncio.run(scenario()))


def test_cache_recheck_skips_the_device_when_a_value_became_fresh() -> None:
    async def scenario() -> tuple[int, TransactionResult]:
        calls = 0

        async def handler(request: TransactionRequest) -> TransactionResult:
            nonlocal calls
            calls += 1
            return TransactionResult(SendOutcome(True, None), frame=request.frame)

        serializer = AccessSerializer(SimpleNamespace(), handler, cache_hit=lambda request: True)
        serializer.start()
        result = await serializer.submit(_request(recheck=True))
        await serializer.stop()
        return calls, result

    calls, result = asyncio.run(scenario())
    assert calls == 0
    assert result.skipped_by_cache and not result.committed
