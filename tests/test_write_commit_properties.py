#!/usr/bin/env python3
#
# tests/test_write_commit_properties.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Property 11: exactly one WRITE frame per write operation from the Commit_Point on."""

import asyncio
from datetime import timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import DeviceKey, EndpointKey
from app.protocol.frames import Frame
from app.protocol.types import WRITE_COMMANDS, Command
from app.scheduling.retry import RetryConfig, execute_with_retry
from app.transport.endpoint import EndpointConfig, TransportEndpoint
from app.transport.types import TransactionOrigin, TransactionRequest
from tests.conftest import AutoClock
from tests.fakes import FakeNetwork

KEY = EndpointKey("10.0.0.5", 8899)
OBJECT_ID = 0x1234


# Feature: rct-rest-api, Property 11: exactly one WRITE frame per write operation from the Commit_Point on
@settings(max_examples=100, deadline=None)
@given(
    fault=st.sampled_from(["none", "connect", "write_raises", "drain_raises", "no_response", "drop"]),
    connect_failures=st.integers(0, 4),
    idempotent=st.booleans(),
    is_action=st.booleans(),
)
def test_at_most_one_write_frame_after_commit(
    fault: str, connect_failures: int, idempotent: bool, is_action: bool
) -> None:
    async def scenario() -> tuple[FakeNetwork, bool]:
        clock = AutoClock()
        net = FakeNetwork(clock, fail_connects=connect_failures if fault in ("connect", "none") else 0)
        net.behavior = lambda frame: {"no_response": "ignore", "drop": "drop"}.get(fault, "respond")
        if fault == "write_raises":
            net.write_error_at = 1
        if fault == "drain_raises":
            net.drain_error_at = 1
        cfg = EndpointConfig(
            response_timeout_seconds=0.01, write_response_timeout_seconds=0.01, min_interval=timedelta(0)
        )
        endpoint = TransportEndpoint("endpoint-1", KEY, cfg, clock, connector=net.connect)
        request = TransactionRequest(
            DeviceKey(KEY),
            Frame(Command.WRITE, OBJECT_ID, b"\x00\x00\x00\x07"),
            TransactionOrigin.CALLER,
            "write",
            idempotent=idempotent,
            is_action=is_action,
        )
        result = await execute_with_retry(endpoint, request, RetryConfig(response_timeout_seconds=0.01), clock)
        await endpoint.close()
        return net, result.committed

    net, committed = asyncio.run(scenario())
    writes = [f for _, f in net.frames if f.command in WRITE_COMMANDS and f.object_id == OBJECT_ID]
    # Pre-commit failures send no frame, so every frame on the wire is a committed attempt.
    assert len(writes) <= 1
    if fault in ("write_raises", "drain_raises", "no_response", "drop"):
        assert net.writes == 1
    if (not idempotent or is_action) and fault in ("connect", "none") and connect_failures > 0:
        assert net.connects == 1  # never retried automatically, not even before the Commit_Point
    if fault in ("connect", "none") and connect_failures == 0:
        # No failure injected anywhere (a "connect" fault with 0 failures injects none either):
        # every case here must commit exactly one WRITE frame, regardless of idempotent/is_action.
        # A write that silently sends zero frames must fail this, not slip through as "<= 1"
        # (Finding 4).
        assert committed is True
        assert len(writes) == 1
    elif idempotent and not is_action and fault in ("connect", "none") and connect_failures <= 2:
        assert len(writes) == 1 and committed
