#!/usr/bin/env python3
#
# tests/test_write_path.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Write path: no values in logs, atomic write plus readback, actions without a WRITE answer, answer window, scaled integer bodies, allowlist type rules."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import DeviceKey, EndpointKey, TokenRole
from app.errors import DeviceTimeout, WriteRejected
from app.protocol.frames import Frame, encode_frame
from app.protocol.stream import StreamParser
from app.protocol.types import WRITE_COMMANDS, Command, DataType, FrameKind
from app.security.allowlist import Allowlist, AllowlistEntry
from app.transport.counters import EndpointCounters
from app.transport.demux import Demultiplexer, PendingTransaction
from app.transport.endpoint import EndpointConfig, TransportEndpoint
from app.transport.receiver import ArrivalLedger, Receiver, ReceiverExit
from app.transport.types import TransactionOrigin, TransactionRequest
from tests.api_helpers import (
    ACTION_NAME,
    TARGET_NAME,
    TARGET_OBJECT_ID,
    WRITE_TOKEN,
    float_payload,
    make_settings,
    running_app,
    write_fixtures,
)
from tests.conftest import AutoClock
from tests.fakes import FakeNetwork, response_to

KEY = EndpointKey("10.0.0.5", 8899)


def _endpoint(net: FakeNetwork, clock: AutoClock) -> TransportEndpoint:
    cfg = EndpointConfig(
        response_timeout_seconds=1.0,
        write_response_timeout_seconds=0.05,
        min_interval=timedelta(0),
    )
    return TransportEndpoint("e", KEY, cfg, clock, connector=net.connect)


def _request(command: Command, object_id: int = 0x1234) -> TransactionRequest:
    kind = "write" if command is Command.WRITE else "read"
    return TransactionRequest(
        DeviceKey(KEY),
        Frame(command, object_id, b"\x00\x00\x00\x07"),
        TransactionOrigin.CALLER,
        kind,
    )


async def test_unanswered_write_keeps_the_connection() -> None:
    clock = AutoClock()
    net = FakeNetwork(clock)
    net.behavior = lambda frame: "ignore"
    endpoint = _endpoint(net, clock)
    started = clock.monotonic()
    result = await endpoint.execute(_request(Command.WRITE))
    advanced = clock.monotonic() - started
    # No answer to a WRITE is normal device behavior, so it is not a transport failure (Finding
    # P3-1): error stays None and the endpoint/device failure counters are not touched.
    assert not result.ok and result.committed and result.error is None
    assert endpoint.counters.failures == 0
    # Deterministic on the virtual clock: after an unanswered WRITE the endpoint sleeps exactly the
    # write quiet window via clock.sleep (not real wall-clock). The read/response-timeout path would
    # instead drop the connection, so a kept connection plus this advance proves the write window
    # was taken, independent of how fast the CI runner is.
    assert advanced == pytest.approx(EndpointConfig().write_quiet_window_seconds)
    assert endpoint.connection_epoch == 1 and net.connects == 1 and net.open_now == 1
    await endpoint.close()


async def test_cancel_in_the_write_quiet_window_drops_the_connection() -> None:
    """A late write echo carries no transaction id, so a cancelled write must not leave its connection."""
    entered = asyncio.Event()

    class CancellableQuietWindow(AutoClock):
        async def sleep(self, seconds: float) -> None:
            if seconds == EndpointConfig().write_quiet_window_seconds:
                entered.set()
                await asyncio.Event().wait()  # parked until the task is cancelled
            await super().sleep(seconds)

    clock = CancellableQuietWindow()
    net = FakeNetwork(clock)
    net.behavior = lambda frame: "ignore"
    endpoint = _endpoint(net, clock)
    task = asyncio.create_task(endpoint.execute(_request(Command.WRITE)))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert net.open_now == 0
    net.behavior = lambda frame: "respond"
    result = await endpoint.execute(_request(Command.READ))
    assert result.ok and net.connects == 2
    await endpoint.close()


async def test_arrival_ledger_failure_closes_the_fresh_connection(monkeypatch) -> None:
    import app.transport.endpoint as endpoint_module

    def broken(*_args, **_kwargs):
        raise RuntimeError("ArrivalLedger could not install its feed_data wrapper")

    monkeypatch.setattr(endpoint_module, "ArrivalLedger", broken)
    clock = AutoClock()
    net = FakeNetwork(clock)
    endpoint = _endpoint(net, clock)
    with pytest.raises(RuntimeError):
        await endpoint.execute(_request(Command.READ))
    assert net.open_now == 0 and all(w.closed for w in net.writers)
    await endpoint.close()


async def test_late_write_echo_cannot_answer_the_readback_read() -> None:
    """The echo arrives while the write holds the lock, so it must not be taken for the READ's response."""
    echo = Frame(
        Command.RESPONSE, 0x1234, b"\x00\x00\x00\x05"
    )  # differs from the stored value 7

    class EchoDuringQuietWindow(AutoClock):
        net: FakeNetwork | None = None
        counters: EndpointCounters | None = None

        async def sleep(self, seconds: float) -> None:
            if (
                self.net is not None and self.counters is not None and seconds == 0.05
            ):  # the write quiet window
                self.net.push(echo)
                # Deterministic hand-off: yield to the event loop until the receiver has actually
                # classified the pushed echo as an unexpected frame, instead of racing a fixed
                # asyncio.sleep(0.01) and hoping the receiver task ran first.
                for _ in range(1000):
                    if self.counters.unexpected_frames >= 1:
                        break
                    await asyncio.sleep(0)
                else:  # pragma: no cover - the receiver always classifies the echo well before this
                    raise AssertionError("receiver never classified the late echo")
            await super().sleep(seconds)

    clock = EchoDuringQuietWindow()
    net = FakeNetwork(clock)
    clock.net = net
    cfg = EndpointConfig(
        response_timeout_seconds=1.0,
        write_response_timeout_seconds=0.05,
        write_quiet_window_seconds=0.05,
        min_interval=timedelta(0),
    )
    endpoint = TransportEndpoint("e", KEY, cfg, clock, connector=net.connect)
    clock.counters = endpoint.counters
    written = await endpoint.execute(_request(Command.WRITE))
    assert not written.ok and written.committed
    read = await endpoint.execute(_request(Command.READ))
    assert (
        read.ok and read.frame is not None and read.frame.payload == b"\x00\x00\x00\x07"
    )
    assert endpoint.counters.unexpected_frames == 1  # the late echo, discarded
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
    assert (
        demux.dispatch(frame, 9.0) is FrameKind.UNEXPECTED
    )  # received before the Commit_Point
    assert not future.done() and counters.unexpected_frames == 1
    assert (
        demux.dispatch(frame, 10.0) is FrameKind.TRANSACTION_RESPONSE
    )  # the send instant itself still counts
    assert future.result() is frame


async def test_a_single_read_timeout_keeps_the_connection_and_a_second_in_a_row_drops_it() -> (
    None
):
    clock = AutoClock()
    net = FakeNetwork(clock)
    net.behavior = lambda frame: "ignore"
    endpoint = _endpoint(net, clock)
    first = await endpoint.execute(_request(Command.READ, 0x1), response_timeout=0.05)
    assert isinstance(first.error, DeviceTimeout)
    assert (
        net.open_now == 1 and endpoint.connection_epoch == 1
    )  # one lost answer is not a dead link
    second = await endpoint.execute(_request(Command.READ, 0x2), response_timeout=0.05)
    assert isinstance(second.error, DeviceTimeout)
    assert net.open_now == 0
    await endpoint.close()


async def test_an_answered_read_resets_the_unanswered_count() -> None:
    clock = AutoClock()
    net = FakeNetwork(clock)
    endpoint = _endpoint(net, clock)
    net.behavior = lambda frame: "ignore"
    await endpoint.execute(_request(Command.READ, 0x1), response_timeout=0.05)
    net.behavior = lambda frame: "respond"
    assert (await endpoint.execute(_request(Command.READ, 0x2))).ok
    net.behavior = lambda frame: "ignore"
    await endpoint.execute(_request(Command.READ, 0x3), response_timeout=0.05)
    assert net.open_now == 1 and endpoint.connection_epoch == 1
    await endpoint.close()


async def test_reads_of_other_objects_keep_the_connection_after_one_miss() -> None:
    clock = AutoClock()
    net = FakeNetwork(clock)
    net.behavior = lambda frame: "ignore" if frame.object_id == 0x1 else "respond"
    endpoint = _endpoint(net, clock)
    await endpoint.execute(_request(Command.READ, 0x1), response_timeout=0.05)
    for object_id in (0x2, 0x3):
        assert (await endpoint.execute(_request(Command.READ, object_id))).ok
    assert endpoint.connection_epoch == 1 and net.connects == 1
    await endpoint.close()


async def test_a_late_answer_of_a_missed_read_cannot_answer_the_retry_of_the_same_object() -> (
    None
):
    """The protocol has no transaction id: answer #1 arriving after request #2 was sent would
    satisfy #2. The same object is therefore re-read only on a new connection."""
    clock = AutoClock()
    net = FakeNetwork(clock)
    late = b"\x00\x00\x00\x2a"
    first_writer = None

    def behavior(frame: Frame) -> str:
        # Once request #2 is on the wire, the old connection delivers the answer to #1 (if still open).
        if len(net.frames) == 2 and not first_writer.closed:
            first_writer.deliver(encode_frame(response_to(frame, late)))
        return "ignore"

    net.behavior = behavior
    endpoint = _endpoint(net, clock)
    first = await endpoint.execute(_request(Command.READ), response_timeout=0.05)
    assert isinstance(first.error, DeviceTimeout)
    first_writer = net.writers[0]
    second = await endpoint.execute(_request(Command.READ), response_timeout=0.05)
    assert not second.ok and isinstance(second.error, DeviceTimeout)
    assert endpoint.connection_epoch == 2 and first_writer.closed
    await endpoint.close()


async def test_a_late_answer_for_a_missed_periodic_object_is_not_cached_as_a_push() -> (
    None
):
    clock = AutoClock()
    net = FakeNetwork(clock)
    net.behavior = lambda frame: "ignore" if frame.object_id == 0x1 else "respond"
    seen: list[int] = []
    endpoint = TransportEndpoint(
        "e",
        KEY,
        EndpointConfig(response_timeout_seconds=1.0, min_interval=timedelta(0)),
        clock,
        connector=net.connect,
        on_value=lambda frame, _kind: seen.append(frame.object_id),
    )
    assert (await endpoint.execute(_request(Command.READ, 0x2))).ok
    seen.clear()
    endpoint.register_periodic(DeviceKey(KEY), 0x1)
    endpoint.register_periodic(DeviceKey(KEY), 0x2)
    await endpoint.execute(_request(Command.READ, 0x1), response_timeout=0.05)
    net.push(
        Frame(Command.RESPONSE, 0x1, b"\x00\x00\x00\x2a")
    )  # late answer, no pending transaction
    net.push(
        Frame(Command.RESPONSE, 0x2, b"\x00\x00\x00\x2b")
    )  # an unrelated push is unaffected
    await asyncio.sleep(0.01)
    assert seen == [0x2]
    await endpoint.close()


async def test_a_write_after_an_unanswered_read_starts_on_a_fresh_connection() -> None:
    """The late answer to the missed read must not be taken as the write's acknowledgement."""
    clock = AutoClock()
    net = FakeNetwork(clock)
    net.behavior = lambda frame: "ignore"
    endpoint = _endpoint(net, clock)
    await endpoint.execute(_request(Command.READ), response_timeout=0.05)
    net.behavior = lambda frame: "respond"
    await endpoint.execute(_request(Command.WRITE))
    assert endpoint.connection_epoch == 2
    await endpoint.close()


async def test_receiver_credits_a_frame_with_its_real_arrival_not_the_later_read_call() -> (
    None
):
    """Bytes buffered in the StreamReader before the Commit_Point must not be
    credited with a later arrival time just because reader.read() only drains them afterwards.
    """
    times = iter(
        [5.0, 20.0]
    )  # 5.0: feed_data of the stale frame; 20.0: the send's Commit_Point

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
    ledger = ArrivalLedger(
        reader, monotonic
    )  # installed before feed_data, as the real endpoint does
    stale = Frame(Command.RESPONSE, 0x1234, b"\x00\x00\x00\x07")
    reader.feed_data(
        encode_frame(stale)
    )  # arrives at monotonic() == 5.0, well before the send
    reader.feed_eof()
    await receiver.run(reader, ledger)
    # Without the ledger this frame would be timestamped at the read() call (20.0) and wrongly
    # satisfy the pending transaction sent at the same instant.
    assert not future.done()
    assert counters.unexpected_frames == 1


class _ManualClock:
    """Monotonic source the test advances by hand, so arrival instants are exact, not measured."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _receive_path(
    clock: _ManualClock,
) -> tuple[Receiver, StreamParser, Demultiplexer, EndpointCounters]:
    counters = EndpointCounters()
    demux = Demultiplexer(counters, clock)
    parser = StreamParser()
    receiver = Receiver(
        parser=parser,
        demux=demux,
        counters=counters,
        monotonic=clock,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        max_frame_bytes=4096,
        unexpected_limit=50,
        unexpected_window_seconds=60,
        on_bootloader=lambda: None,
    )
    return receiver, parser, demux, counters


PERIODIC_OBJECT_ID = 0x5678


def _periodic_push(value: int) -> bytes:
    return encode_frame(
        Frame(Command.RESPONSE, PERIODIC_OBJECT_ID, value.to_bytes(4, "big"))
    )


async def test_a_stale_periodic_push_in_the_same_read_does_not_age_the_real_response() -> (
    None
):
    """One read() may drain a periodic push that arrived before the send together with the answer.

    The answer is dated by its own last byte, so the older bytes in the batch cannot push it
    below the Commit_Point and leave the transaction waiting for a response that did arrive.
    """
    clock = _ManualClock(1.0)
    receiver, _, demux, counters = _receive_path(clock)
    reader = asyncio.StreamReader()
    ledger = ArrivalLedger(reader, clock)
    demux.register_periodic(DeviceKey(KEY), PERIODIC_OBJECT_ID)
    reader.feed_data(_periodic_push(1))  # still unprocessed when the request goes out
    clock.now = 2.0
    future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
    demux.pending = PendingTransaction(0x1234, None, clock.now, future)
    clock.now = 3.0
    reader.feed_data(encode_frame(Frame(Command.RESPONSE, 0x1234, b"\x00\x00\x00\x07")))
    reader.feed_eof()

    assert await receiver.run(reader, ledger) is ReceiverExit.EOF
    assert future.result().payload == b"\x00\x00\x00\x07"
    assert counters.unexpected_frames == 0


async def test_a_frame_split_across_two_reads_does_not_age_the_response_behind_it() -> (
    None
):
    """A TCP segment boundary inside a frame is enough to reproduce this; no event loop stall.

    The split frame's own tail arrives after the send, but it answers another object, so only
    the real response may satisfy the transaction - and it must, with the connection kept.
    """
    clock = _ManualClock(1.0)
    receiver, parser, demux, counters = _receive_path(clock)
    reader = asyncio.StreamReader()
    ledger = ArrivalLedger(reader, clock)
    demux.register_periodic(DeviceKey(KEY), PERIODIC_OBJECT_ID)
    split = _periodic_push(2)
    reader.feed_data(split[:4])  # first read ends mid-frame, before the Commit_Point
    task = asyncio.create_task(receiver.run(reader, ledger))
    for _ in range(1000):
        if parser.buffered_bytes == 4:
            break
        await asyncio.sleep(0)
    else:  # pragma: no cover - the receiver always gets its first read in well before this
        task.cancel()
        raise AssertionError("receiver never consumed the partial frame")

    clock.now = 2.0
    future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
    demux.pending = PendingTransaction(0x1234, None, clock.now, future)
    clock.now = 3.0
    reader.feed_data(split[4:])
    clock.now = 4.0
    reader.feed_data(encode_frame(Frame(Command.RESPONSE, 0x1234, b"\x00\x00\x00\x07")))
    reader.feed_eof()

    assert await task is ReceiverExit.EOF
    assert future.result().payload == b"\x00\x00\x00\x07"
    assert counters.unexpected_frames == 0


async def test_put_without_write_answer_is_confirmed_by_readback_on_the_same_connection(
    tmp_path,
) -> None:
    settings = make_settings(
        enable_write_support=True,
        write_response_timeout_ms=100,
        **write_fixtures(tmp_path),
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
        assert (
            body["confirmed"] is True
            and body["send_unconfirmed"] is True
            and body["readback_value"] == 0.5
        )
        assert h.net.connects == connects  # no reconnect


ROOT = Path(__file__).resolve().parents[1]


SOC_TARGET = "battery_soc_target"  # the only shipped register with scale != 1 (percent over a 0..1 ratio)


SOC_TARGET_OBJECT_ID = 0x8B9FF008


def _shipped_settings():
    """The scaled register only exists in the shipped catalog, so the tests write against it."""
    return make_settings(
        object_registry_path=ROOT / "app/catalog/objects.json",
        write_allowlist_path=ROOT / "app/catalog/default_write_allowlist.json",
        enable_write_support=True,
        write_response_timeout_ms=100,
    )


def _write_payload(net, object_id: int) -> str:
    """Hex of the payload of the one WRITE frame that reached the fake device for ``object_id``."""
    payloads = [
        f.payload.hex()
        for _, f in net.frames
        if f.command in WRITE_COMMANDS and f.object_id == object_id
    ]
    assert len(payloads) == 1, payloads
    return payloads[0]


async def _put(h, name: str, value) -> tuple:
    response = await h.client.put(
        f"/api/v1/devices/main/metrics/{name}", json={"value": value}, headers=WRITER
    )
    return response, response.json()


@pytest.mark.parametrize("value", [80, 80.0])
async def test_scaled_register_divides_an_int_body_like_a_float_body(value) -> None:
    """An SoC target of 80 percent must reach the wire as the ratio 0.8, whether the JSON body says
    80 or 80.0. Before the fix the division was skipped for an int body, so the device silently got
    80.0 instead of 0.8: a factor of 100 too high, without an error or a warning."""
    async with running_app(_shipped_settings()) as h:
        response, body = await _put(h, SOC_TARGET, value)
        wire = _write_payload(h.net, SOC_TARGET_OBJECT_ID)
    assert response.status_code == 200, response.text
    assert wire == "3f4ccccd"  # IEEE-754 float32 of 0.8, identical for both body types
    assert body["written_value"] == value
    # float32 cannot hold 0.8 exactly, so the scaled readback is 80.0000011920929, not 80.
    assert body["confirmed"] is True and body["readback_value"] == pytest.approx(80.0)


@pytest.mark.parametrize("value,expected", [(0, "00000000"), (100, "3f800000")])
async def test_scaled_register_divides_an_int_body_at_the_range_bounds(
    value, expected
) -> None:
    """0 % and 100 % are the allowlist bounds of battery_soc_target; both must scale to the wire
    ratio (0.0 -> 00000000, 1.0 -> 3f800000) whether the JSON body is int or float."""
    async with running_app(_shipped_settings()) as h:
        response, body = await _put(h, SOC_TARGET, value)
        wire = _write_payload(h.net, SOC_TARGET_OBJECT_ID)
    assert response.status_code == 200, response.text
    assert wire == expected
    assert body["confirmed"] is True
    assert body["readback_value"] == pytest.approx(float(value), abs=1e-4)


@pytest.mark.parametrize("value", [-5, -0.1, 100.1, 1000000])
async def test_soc_target_allowlist_rejects_an_out_of_range_percentage(value) -> None:
    """battery_soc_target is a percent (object catalog: percent API-side, ratio 0..1 wire-side,
    scale 100); the write allowlist range is the pre-transaction bound, so a value outside 0..100 %
    must be refused before any device transaction, not scaled through to the hardware.
    """
    async with running_app(_shipped_settings()) as h:
        response, body = await _put(h, SOC_TARGET, value)
        wire = [
            f
            for _, f in h.net.frames
            if f.command in WRITE_COMMANDS and f.object_id == SOC_TARGET_OBJECT_ID
        ]
    assert response.status_code == 422, response.text
    assert body["code"] == "value_out_of_range"
    assert wire == []  # the device was never touched


@pytest.mark.parametrize(
    "name,object_id,value,expected",
    [
        ("power_mng_soc_strategy", 0xF168B748, 2, "02"),  # t_enum, int body
        (
            "power_mng_soc_charge_power",
            0x1D2994EA,
            100,
            "42c80000",
        ),  # t_float with scale 1, int body
    ],
)
async def test_unscaled_register_keeps_an_int_body_untouched(
    name, object_id, value, expected
) -> None:
    """Regression guard: for scale == 1.0 the int body must produce the same wire bytes as before."""
    async with running_app(_shipped_settings()) as h:
        response, body = await _put(h, name, value)
        wire = _write_payload(h.net, object_id)
    assert response.status_code == 200, response.text
    assert wire == expected
    assert body["confirmed"] is True


@pytest.mark.parametrize("value,expected", [(True, "01"), (False, "00")])
async def test_bool_body_is_never_scaled(value, expected) -> None:
    """bool is a subclass of int in Python, so it must be excluded from the scale division by hand."""
    async with running_app(_shipped_settings()) as h:
        response, body = await _put(h, "power_mng_use_grid_power_enable", value)
        wire = _write_payload(h.net, 0x36A9E9A6)
    assert response.status_code == 200, response.text
    assert wire == expected
    assert body["confirmed"] is True and body["readback_value"] is value


def test_bool_on_a_scaled_register_stays_a_bool_in_the_encoder() -> None:
    """Directly on the encoder, because no shipped register is both scaled and t_bool: a bool must
    not be divided, which for t_float means it is rejected as a type mismatch, not silently scaled.
    """
    from app.catalog.registry import RegistryCatalog
    from app.clock import SystemClock
    from app.errors import WriteRejected
    from app.gateway.cache import MemoryCache
    from app.gateway.rct import RctGateway
    from app.security.allowlist import Allowlist

    catalog = RegistryCatalog.from_file(ROOT / "app/catalog/objects.json")
    gateway = RctGateway(
        catalog, MemoryCache(30, 30), SystemClock(), Allowlist({}, catalog)
    )
    entry = catalog.object_entry(SOC_TARGET)
    assert entry.scale == 100.0
    with pytest.raises(WriteRejected) as info:
        gateway._encode(entry, True)
    assert info.value.code == "value_type_mismatch"


WRITER = {"Authorization": f"Bearer {WRITE_TOKEN}"}


SECRET = "S3cr3t-Pa55w0rd-synthetic"


SECRET_NAME = "secret_text"


def _settings(tmp_path: Path, **extra):
    return make_settings(
        enable_write_support=True,
        write_response_timeout_ms=100,
        **write_fixtures(tmp_path),
        **extra,
    )


def _with_string_entry(tmp_path: Path):
    paths = write_fixtures(tmp_path)
    registry = json.loads(paths["object_registry_path"].read_text(encoding="utf-8"))
    registry["entries"].append(
        {
            "name": SECRET_NAME,
            "object_id": "0x1234ABCE",
            "data_type": "t_string",
            "unit": "",
            "value_type": "string",
            "writable": True,
            "idempotent_write": False,
            "is_action": False,
            "preselected": False,
            "description": "synthetic secret",
        }
    )
    paths["object_registry_path"].write_text(json.dumps(registry), encoding="utf-8")
    allowlist = json.loads(paths["write_allowlist_path"].read_text(encoding="utf-8"))
    allowlist["entries"].append({"name": SECRET_NAME, "data_type": "t_string"})
    paths["write_allowlist_path"].write_text(json.dumps(allowlist), encoding="utf-8")
    return make_settings(
        enable_write_support=True, write_response_timeout_ms=100, **paths
    )


def _leaks(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if SECRET in r.getMessage() or SECRET in str(r.args)
    ]


async def test_secret_string_write_never_reaches_the_log(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    async with running_app(_with_string_entry(tmp_path)) as h:
        url = f"/api/v1/devices/main/metrics/{SECRET_NAME}"
        ok = await h.client.put(url, json={"value": SECRET}, headers=WRITER)
        h.net.freeze_writes = True
        h.net.payloads[0x1234ABCE] = b"\x00\x00\x00\x00"
        failed = await h.client.put(url, json={"value": SECRET + "x"}, headers=WRITER)
        rejected = await h.client.put(
            url, json={"value": SECRET + "\x00"}, headers=WRITER
        )
        wrong_type = await h.client.put(
            f"/api/v1/devices/main/metrics/{TARGET_NAME}",
            json={"value": SECRET},
            headers=WRITER,
        )
    assert ok.status_code == 200, ok.text
    assert (
        failed.status_code == 502
        and rejected.status_code == 422
        and wrong_type.status_code == 422
    )
    assert _leaks(caplog) == []


async def test_concurrent_writes_to_one_object_are_each_confirmed(tmp_path) -> None:
    async with running_app(_settings(tmp_path)) as h:
        url = f"/api/v1/devices/main/metrics/{TARGET_NAME}"
        responses = await asyncio.gather(
            h.client.put(url, json={"value": 0.2}, headers=WRITER),
            h.client.put(url, json={"value": 0.8}, headers=WRITER),
        )
        order = [
            (f.command, f.payload)
            for _, f in h.net.frames
            if f.object_id == TARGET_OBJECT_ID
        ]
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert [round(r.json()["readback_value"], 3) for r in responses] == [0.2, 0.8]
    assert [c for c, _ in order] == [
        Command.WRITE,
        Command.READ,
        Command.WRITE,
        Command.READ,
    ]


async def test_action_without_write_answer_and_readback_ok_is_200(tmp_path) -> None:
    async with running_app(_settings(tmp_path)) as h:
        response = await h.client.post(
            f"/api/v1/devices/main/actions/{ACTION_NAME}",
            json={"value": 1},
            headers=WRITER,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (
        body["action_confirmed"] is False
        and body["requested_value"] == 1
        and body["readback_value"] is not None
    )


async def test_action_without_readback_is_502_outcome_unknown(tmp_path) -> None:
    def behavior(frame):
        return (
            "ignore"
            if frame.command is Command.READ and frame.object_id != TARGET_OBJECT_ID
            else "respond"
        )

    async with running_app(_settings(tmp_path), behavior=behavior) as h:
        h.net.payloads[TARGET_OBJECT_ID] = float_payload(0.5)
        response = await h.client.post(
            f"/api/v1/devices/main/actions/{ACTION_NAME}",
            json={"value": 1},
            headers=WRITER,
        )
    assert (
        response.status_code == 502
        and response.json()["code"] == "action_outcome_unknown"
    ), response.text


BOOL_NAME, BOOL_ID = "switch_flag", 0x1234ABCE


_IGNORE_WRITE = lambda frame: "ignore" if frame.command is Command.WRITE else "respond"


def _settings_with_bool(tmp_path: Path):
    paths = write_fixtures(tmp_path)
    reg = json.loads(paths["object_registry_path"].read_text(encoding="utf-8"))
    reg["entries"].append(
        {
            "name": BOOL_NAME,
            "object_id": f"0x{BOOL_ID:08X}",
            "data_type": "t_bool",
            "unit": "none",
            "value_type": "boolean",
            "writable": True,
            "idempotent_write": True,
            "is_action": False,
            "preselected": False,
        }
    )
    paths["object_registry_path"].write_text(json.dumps(reg), encoding="utf-8")
    allow = json.loads(paths["write_allowlist_path"].read_text(encoding="utf-8"))
    allow["entries"].append({"name": BOOL_NAME, "data_type": "t_bool"})
    paths["write_allowlist_path"].write_text(json.dumps(allow), encoding="utf-8")
    return make_settings(enable_write_support=True, **paths)


async def _put_readback(h, name: str, value, readback: bytes):
    h.net.freeze_writes = True  # the test dictates what the device reads back
    h.net.payloads[BOOL_ID if name == BOOL_NAME else TARGET_OBJECT_ID] = readback
    return await h.client.put(
        f"/api/v1/devices/main/metrics/{name}", json={"value": value}, headers=WRITER
    )


async def test_bool_write_is_confirmed_by_a_nonzero_readback(tmp_path) -> None:
    async with running_app(_settings_with_bool(tmp_path), behavior=_IGNORE_WRITE) as h:
        response = await _put_readback(h, BOOL_NAME, True, b"\x07")
        assert response.status_code == 200, response.text
        assert (
            response.json()["confirmed"] is True
            and response.json()["readback_value"] is True
        )


async def test_bool_write_with_opposite_readback_stays_unconfirmed(tmp_path) -> None:
    async with running_app(_settings_with_bool(tmp_path), behavior=_IGNORE_WRITE) as h:
        response = await _put_readback(h, BOOL_NAME, True, b"\x00")
        assert (
            response.status_code == 502
            and response.json()["code"] == "write_outcome_unknown"
        )


async def test_float_write_is_confirmed_despite_float32_rounding(tmp_path) -> None:
    async with running_app(_settings_with_bool(tmp_path), behavior=_IGNORE_WRITE) as h:
        response = await _put_readback(h, TARGET_NAME, 0.1, float_payload(0.1))
        assert response.status_code == 200, response.text
        wrong = await _put_readback(h, TARGET_NAME, 0.1, float_payload(0.5))
        assert (
            wrong.status_code == 502 and wrong.json()["code"] == "write_outcome_unknown"
        )


async def test_integer_valued_float_for_an_enum_is_422_not_500(tmp_path) -> None:
    async with running_app(_settings_with_bool(tmp_path)) as h:
        response = await h.client.post(
            f"/api/v1/devices/main/actions/{ACTION_NAME}",
            json={"value": 1.0},
            headers=WRITER,
        )
        assert (
            response.status_code == 422
            and response.json()["code"] == "value_type_mismatch"
        ), response.text


@pytest.mark.parametrize("kind", [DataType.UINT8, DataType.INT32, DataType.ENUM])
def test_allowlist_rejects_a_float_for_integer_types(kind) -> None:
    entry = AllowlistEntry(name="x", data_type=kind, minimum=0, maximum=10)
    for value in (1.0, 1.5):
        with pytest.raises(WriteRejected) as info:
            Allowlist._check_value(entry, value)
        assert info.value.code == "value_type_mismatch"
    Allowlist._check_value(entry, 1)


@pytest.mark.parametrize(
    "kind", [DataType.STRUCT, DataType.TIMESERIES, DataType.EVENT_TABLE]
)
def test_compound_types_cannot_be_approved_for_writing(kind) -> None:
    with pytest.raises(ValidationError, match="cannot be approved for writing"):
        AllowlistEntry(name="x", data_type=kind, minimum=0, maximum=1)


@pytest.mark.parametrize(
    "extra", [{"minimum": 0}, {"maximum": 1}, {"step": 1}, {"allowed_values": [0]}]
)
@pytest.mark.parametrize("kind", [DataType.BOOL, DataType.STRING])
def test_allowlist_rejects_restrictions_for_bool_and_string(kind, extra) -> None:
    with pytest.raises(ValidationError):
        AllowlistEntry(name="x", data_type=kind, **extra)
    AllowlistEntry(name="x", data_type=kind)


def test_load_allowlist_ignores_a_broken_file_when_write_support_is_off(
    tmp_path,
) -> None:
    """A disabled feature must not fail the service start on its own broken config file.

    Regression for the read-only startup finding: load_allowlist() used to read the allowlist file
    whenever it merely existed, so a garbage file turned a read-only (writes-off) boot into a hard
    ConfigError. With writes off the result is now an empty allowlist and the file is never read.
    """
    from app.api.app_factory import load_allowlist
    from app.catalog.registry import RegistryCatalog
    from app.errors import ConfigError
    from tests.api_helpers import REGISTRY_FIXTURE

    catalog = RegistryCatalog.from_file(REGISTRY_FIXTURE)
    broken = tmp_path / "objects_write_allowed.json"
    broken.write_text("{ this is not valid json", encoding="utf-8")

    settings = make_settings(enable_write_support=False, write_allowlist_path=broken)
    allowlist = load_allowlist(settings, catalog)
    assert len(allowlist) == 0

    # The same broken file still aborts when writes are enabled: the guard only skips the read, it
    # does not swallow a real misconfiguration an operator who turned writes on must see.
    enabled = make_settings(enable_write_support=True, write_allowlist_path=broken)
    with pytest.raises(ConfigError):
        load_allowlist(enabled, catalog)


@pytest.mark.parametrize("field", ["minimum", "maximum", "step"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_allowlist_entry_rejects_non_finite_bounds(field, bad) -> None:
    fields = {"minimum": 0, "maximum": 10, "step": 1} | {field: bad}
    with pytest.raises(ValidationError):
        AllowlistEntry(name="x", data_type=DataType.FLOAT, **fields)
    AllowlistEntry(
        name="x", data_type=DataType.FLOAT, minimum=-1.5, maximum=10.5, step=0.5
    )


@pytest.mark.parametrize("write_enabled", [True, False])
@pytest.mark.parametrize("role", [TokenRole.READ, TokenRole.READ_WRITE])
async def test_require_write_checks_write_support_before_the_role(
    write_enabled, role
) -> None:
    from types import SimpleNamespace

    from app.errors import InsufficientScope, WriteDisabled
    from app.security.dependencies import require_write
    from app.security.tokens import Principal

    principal = Principal("t", role)
    ctx = SimpleNamespace(write_enabled=write_enabled)
    if not write_enabled:
        with pytest.raises(
            WriteDisabled
        ):  # also for a READ token: support is checked first
            await require_write(principal, ctx)
    elif role is TokenRole.READ:
        with pytest.raises(InsufficientScope):
            await require_write(principal, ctx)
    else:
        assert await require_write(principal, ctx) is principal
