#!/usr/bin/env python3
#
# tests/test_write_response_window.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""A WRITE gets a short answer window and the connection is kept (Requirement 9.18)."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

from app.config import DeviceKey, EndpointKey
from app.errors import DeviceTimeout
from app.protocol.frames import Frame, encode_frame
from app.protocol.stream import StreamParser
from app.protocol.types import Command, FrameKind
from app.transport.counters import EndpointCounters
from app.transport.demux import Demultiplexer, PendingTransaction
from app.transport.endpoint import EndpointConfig, TransportEndpoint
from app.transport.receiver import ArrivalLedger, Receiver
from app.transport.types import TransactionOrigin, TransactionRequest
from tests.api_helpers import (
    TARGET_NAME,
    TARGET_OBJECT_ID,
    WRITE_TOKEN,
    float_payload,
    make_settings,
    running_app,
    write_fixtures,
)
from tests.conftest import AutoClock
from tests.fakes import FakeNetwork

KEY = EndpointKey("10.0.0.5", 8899)


def _endpoint(net: FakeNetwork, clock: AutoClock) -> TransportEndpoint:
    cfg = EndpointConfig(response_timeout_seconds=1.0, write_response_timeout_seconds=0.05, min_interval=timedelta(0))
    return TransportEndpoint("e", KEY, cfg, clock, connector=net.connect)


def _request(command: Command) -> TransactionRequest:
    kind = "write" if command is Command.WRITE else "read"
    return TransactionRequest(
        DeviceKey(KEY), Frame(command, 0x1234, b"\x00\x00\x00\x07"), TransactionOrigin.CALLER, kind
    )


async def test_unanswered_write_keeps_the_connection() -> None:
    clock = AutoClock()
    net = FakeNetwork(clock)
    net.behavior = lambda frame: "ignore"
    endpoint = _endpoint(net, clock)
    started = time.monotonic()
    result = await endpoint.execute(_request(Command.WRITE))
    elapsed = time.monotonic() - started
    # No answer to a WRITE is normal device behavior, so it is not a transport failure (Finding
    # P3-1): error stays None and the endpoint/device failure counters are not touched.
    assert not result.ok and result.committed and result.error is None
    assert endpoint.counters.failures == 0
    assert elapsed < 0.5  # the write window, not RESPONSE_TIMEOUT_SECONDS
    assert endpoint.connection_epoch == 1 and net.connects == 1 and net.open_now == 1
    await endpoint.close()


async def test_answered_write_is_confirmed() -> None:
    clock = AutoClock()
    net = FakeNetwork(clock)
    net.answer_writes = True
    endpoint = _endpoint(net, clock)
    result = await endpoint.execute(_request(Command.WRITE))
    assert result.ok and endpoint.connection_epoch == 1
    await endpoint.close()


async def test_frame_received_before_the_send_cannot_answer_the_transaction() -> None:
    """P2-2: a frame already buffered when the request went out must not satisfy the pending read."""
    counters = EndpointCounters()
    demux = Demultiplexer(counters, lambda: 20.0)
    future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
    demux.pending = PendingTransaction(0x1234, None, 10.0, future)
    frame = Frame(Command.RESPONSE, 0x1234, b"\x00\x00\x00\x07")
    assert demux.dispatch(frame, 9.0) is FrameKind.UNEXPECTED  # received before the Commit_Point
    assert not future.done() and counters.unexpected_frames == 1
    assert demux.dispatch(frame, 10.0) is FrameKind.TRANSACTION_RESPONSE  # the send instant itself still counts
    assert future.result() is frame


async def test_read_timeout_still_drops_the_connection() -> None:
    clock = AutoClock()
    net = FakeNetwork(clock)
    net.behavior = lambda frame: "ignore"
    endpoint = _endpoint(net, clock)
    result = await endpoint.execute(_request(Command.READ), response_timeout=0.05)
    assert isinstance(result.error, DeviceTimeout)
    assert net.open_now == 0
    await endpoint.close()


async def test_receiver_credits_a_frame_with_its_real_arrival_not_the_later_read_call() -> None:
    """Finding P2-1: bytes buffered in the StreamReader before the Commit_Point must not be
    credited with a later arrival time just because reader.read() only drains them afterwards."""
    times = iter([5.0, 20.0])  # 5.0: feed_data of the stale frame; 20.0: the send's Commit_Point

    def monotonic() -> float:
        return next(times, 20.0)

    counters = EndpointCounters()
    demux = Demultiplexer(counters, lambda: 20.0)
    future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
    demux.pending = PendingTransaction(0x1234, None, 20.0, future)
    receiver = Receiver(
        parser=StreamParser(),
        demux=demux,
        counters=counters,
        monotonic=monotonic,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        max_frame_bytes=4096,
        unexpected_limit=50,
        unexpected_window_seconds=60,
        on_bootloader=lambda: None,
    )
    reader = asyncio.StreamReader()
    ledger = ArrivalLedger(reader, monotonic)  # installed before feed_data, as the real endpoint does
    stale = Frame(Command.RESPONSE, 0x1234, b"\x00\x00\x00\x07")
    reader.feed_data(encode_frame(stale))  # arrives at monotonic() == 5.0, well before the send
    reader.feed_eof()
    await receiver.run(reader, ledger)
    # Without the ledger this frame would be timestamped at the read() call (20.0) and wrongly
    # satisfy the pending transaction sent at the same instant.
    assert not future.done()
    assert counters.unexpected_frames == 1


async def test_put_without_write_answer_is_confirmed_by_readback_on_the_same_connection(tmp_path) -> None:
    settings = make_settings(
        enable_write_support=True, write_response_timeout_ms=100, **write_fixtures(tmp_path)
    )
    behavior = lambda frame: "ignore" if frame.command is Command.WRITE else "respond"
    async with running_app(settings, behavior=behavior) as h:
        h.net.payloads[TARGET_OBJECT_ID] = float_payload(0.5)
        connects = h.net.connects
        response = await h.client.put(
            f"/api/v1/devices/main/metrics/{TARGET_NAME}",
            json={"value": 0.5},
            headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["confirmed"] is True and body["send_unconfirmed"] is True and body["readback_value"] == 0.5
        assert h.net.connects == connects  # no reconnect
