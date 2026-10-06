#!/usr/bin/env python3
#
# tests/test_send_gate_properties.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Properties 7 and 8: minimum pause at the send gate, one connection and one transaction per endpoint."""

import asyncio
from datetime import timedelta
from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import DeviceKey, EndpointKey
from app.protocol.frames import Frame
from app.protocol.types import Command
from app.transport.endpoint import EndpointConfig, TransportEndpoint
from app.transport.send_gate import SendGate
from app.transport.types import TransactionOrigin, TransactionRequest
from tests.conftest import AutoClock
from tests.fakes import FakeNetwork

PROP = settings(max_examples=100, deadline=None)
KEY = EndpointKey("10.0.0.5", 8899)


def _request(object_id: int = 1, command: Command = Command.READ) -> TransactionRequest:
    return TransactionRequest(DeviceKey(KEY), Frame(command, object_id), TransactionOrigin.CALLER, "read")


# Feature: rct-rest-api, Property 7: minimum pause at the Send_Gate
@PROP
@given(
    st.integers(0, 1000),
    st.lists(st.tuples(st.integers(0, 1500), st.integers(1, 3)), min_size=1, max_size=8),
)
def test_minimum_pause_between_any_two_frames(interval_ms: int, steps: list[tuple[int, int]]) -> None:
    async def collect() -> list[float]:
        clock = AutoClock()
        gate = SendGate(timedelta(milliseconds=interval_ms), clock)
        stamps: list[float] = []

        class Writer:
            closed = False

            def is_closing(self) -> bool:
                return False

            def write(self, data: bytes) -> None:
                stamps.append(clock.monotonic())

            async def drain(self) -> None:
                return None

        for gap_ms, burst in steps:
            clock.advance(gap_ms / 1000)
            await asyncio.gather(*(gate.send(Writer(), b"x") for _ in range(burst)))
        return stamps

    stamps = asyncio.run(collect())
    for a, b in pairwise(stamps):
        assert b - a >= interval_ms / 1000 - 1e-9


# Feature: rct-rest-api, Property 8: at most one connection and at most one transaction per endpoint
@PROP
@given(
    st.lists(st.sampled_from(["respond", "ignore", "drop"]), min_size=1, max_size=6),
    st.integers(0, 2),
)
def test_single_connection_and_single_transaction(behaviors: list[str], failing_connects: int) -> None:
    async def scenario() -> tuple[int, int]:
        clock = AutoClock()
        net = FakeNetwork(clock, fail_connects=failing_connects)
        queue = list(behaviors)
        net.behavior = lambda frame: queue.pop(0) if queue else "respond"
        cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
        endpoint = TransportEndpoint("endpoint-1", KEY, cfg, clock, connector=net.connect)
        running = peak = 0
        original = endpoint._attempt

        async def counted(request, timeout):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            try:
                return await original(request, timeout)
            finally:
                running -= 1

        endpoint._attempt = counted
        await asyncio.gather(*(endpoint.execute(_request(i)) for i in range(len(behaviors) + 1)))
        await endpoint.close()
        return net.max_open, peak

    max_open, peak = asyncio.run(scenario())
    assert max_open <= 1
    assert peak <= 1
