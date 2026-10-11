#!/usr/bin/env python3
#
# tests/test_write_path.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Write path: no values in logs, atomic write plus readback, actions without a WRITE answer, answer window, scaled integer bodies, allowlist type rules."""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.allowlist import Allowlist, AllowlistEntry
from app.config import DeviceKey, EndpointKey
from app.errors import DeviceTimeout, WriteRejected
from app.protocol.frames import Frame, encode_frame
from app.protocol.stream import StreamParser
from app.protocol.types import WRITE_COMMANDS, Command, DataType, FrameKind
from app.transport.counters import EndpointCounters
from app.transport.demux import Demultiplexer, PendingTransaction
from app.transport.endpoint import EndpointConfig, TransportEndpoint
from app.transport.receiver import ArrivalLedger, Receiver
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


async def test_late_write_echo_cannot_answer_the_readback_read() -> None:
    """The echo arrives while the write holds the lock, so it must not be taken for the READ's response."""
    echo = Frame(Command.RESPONSE, 0x1234, b"\x00\x00\x00\x05")  # differs from the stored value 7

    class EchoDuringQuietWindow(AutoClock):
        net: FakeNetwork | None = None

        async def sleep(self, seconds: float) -> None:
            if self.net is not None and seconds == 0.05:  # the write quiet window
                self.net.push(echo)
                await asyncio.sleep(0.01)  # let the receiver classify the echo
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
    written = await endpoint.execute(_request(Command.WRITE))
    assert not written.ok and written.committed
    read = await endpoint.execute(_request(Command.READ))
    assert read.ok and read.frame is not None and read.frame.payload == b"\x00\x00\x00\x07"
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
    """Bytes buffered in the StreamReader before the Commit_Point must not be
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
    payloads = [f.payload.hex() for _, f in net.frames if f.command in WRITE_COMMANDS and f.object_id == object_id]
    assert len(payloads) == 1, payloads
    return payloads[0]


async def _put(h, name: str, value) -> tuple:
    response = await h.client.put(f"/api/v1/devices/main/metrics/{name}", json={"value": value}, headers=WRITER)
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
async def test_scaled_register_divides_an_int_body_at_the_range_bounds(value, expected) -> None:
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
    must be refused before any device transaction, not scaled through to the hardware."""
    async with running_app(_shipped_settings()) as h:
        response, body = await _put(h, SOC_TARGET, value)
        wire = [f for _, f in h.net.frames if f.command in WRITE_COMMANDS and f.object_id == SOC_TARGET_OBJECT_ID]
    assert response.status_code == 422, response.text
    assert body["code"] == "value_out_of_range"
    assert wire == []  # the device was never touched


@pytest.mark.parametrize("name,object_id,value,expected", [
    ("power_mng_soc_strategy", 0xF168B748, 2, "02"),  # t_enum, int body
    ("power_mng_soc_charge_power", 0x1D2994EA, 100, "42c80000"),  # t_float with scale 1, int body
])
async def test_unscaled_register_keeps_an_int_body_untouched(name, object_id, value, expected) -> None:
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
    not be divided, which for t_float means it is rejected as a type mismatch, not silently scaled."""
    from app.allowlist import Allowlist
    from app.cache import MemoryCache
    from app.catalog.registry import RegistryCatalog
    from app.clock import SystemClock
    from app.errors import WriteRejected
    from app.gateway.rct import RctGateway

    catalog = RegistryCatalog.from_file(ROOT / "app/catalog/objects.json")
    gateway = RctGateway(catalog, MemoryCache(30, 30), SystemClock(), Allowlist({}, catalog))
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
    return make_settings(enable_write_support=True, write_response_timeout_ms=100, **paths)


def _leaks(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if SECRET in r.getMessage() or SECRET in str(r.args)]


async def test_secret_string_write_never_reaches_the_log(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    async with running_app(_with_string_entry(tmp_path)) as h:
        url = f"/api/v1/devices/main/metrics/{SECRET_NAME}"
        ok = await h.client.put(url, json={"value": SECRET}, headers=WRITER)
        h.net.freeze_writes = True
        h.net.payloads[0x1234ABCE] = b"\x00\x00\x00\x00"
        failed = await h.client.put(url, json={"value": SECRET + "x"}, headers=WRITER)
        rejected = await h.client.put(url, json={"value": SECRET + "\x00"}, headers=WRITER)
        wrong_type = await h.client.put(
            f"/api/v1/devices/main/metrics/{TARGET_NAME}", json={"value": SECRET}, headers=WRITER
        )
    assert ok.status_code == 200, ok.text
    assert failed.status_code == 502 and rejected.status_code == 422 and wrong_type.status_code == 422
    assert _leaks(caplog) == []


async def test_concurrent_writes_to_one_object_are_each_confirmed(tmp_path) -> None:
    async with running_app(_settings(tmp_path)) as h:
        url = f"/api/v1/devices/main/metrics/{TARGET_NAME}"
        responses = await asyncio.gather(
            h.client.put(url, json={"value": 0.2}, headers=WRITER),
            h.client.put(url, json={"value": 0.8}, headers=WRITER),
        )
        order = [(f.command, f.payload) for _, f in h.net.frames if f.object_id == TARGET_OBJECT_ID]
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert [round(r.json()["readback_value"], 3) for r in responses] == [0.2, 0.8]
    assert [c for c, _ in order] == [Command.WRITE, Command.READ, Command.WRITE, Command.READ]


async def test_action_without_write_answer_and_readback_ok_is_200(tmp_path) -> None:
    async with running_app(_settings(tmp_path)) as h:
        response = await h.client.post(f"/api/v1/devices/main/actions/{ACTION_NAME}", json={"value": 1}, headers=WRITER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action_confirmed"] is False and body["requested_value"] == 1 and body["readback_value"] is not None


async def test_action_without_readback_is_502_outcome_unknown(tmp_path) -> None:
    def behavior(frame):
        return "ignore" if frame.command is Command.READ and frame.object_id != TARGET_OBJECT_ID else "respond"

    async with running_app(_settings(tmp_path), behavior=behavior) as h:
        h.net.payloads[TARGET_OBJECT_ID] = float_payload(0.5)
        response = await h.client.post(f"/api/v1/devices/main/actions/{ACTION_NAME}", json={"value": 1}, headers=WRITER)
    assert response.status_code == 502 and response.json()["code"] == "action_outcome_unknown", response.text


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
    return await h.client.put(f"/api/v1/devices/main/metrics/{name}", json={"value": value}, headers=WRITER)


async def test_bool_write_is_confirmed_by_a_nonzero_readback(tmp_path) -> None:
    async with running_app(_settings_with_bool(tmp_path), behavior=_IGNORE_WRITE) as h:
        response = await _put_readback(h, BOOL_NAME, True, b"\x07")
        assert response.status_code == 200, response.text
        assert response.json()["confirmed"] is True and response.json()["readback_value"] is True


async def test_bool_write_with_opposite_readback_stays_unconfirmed(tmp_path) -> None:
    async with running_app(_settings_with_bool(tmp_path), behavior=_IGNORE_WRITE) as h:
        response = await _put_readback(h, BOOL_NAME, True, b"\x00")
        assert response.status_code == 502 and response.json()["code"] == "write_outcome_unknown"


async def test_float_write_is_confirmed_despite_float32_rounding(tmp_path) -> None:
    async with running_app(_settings_with_bool(tmp_path), behavior=_IGNORE_WRITE) as h:
        response = await _put_readback(h, TARGET_NAME, 0.1, float_payload(0.1))
        assert response.status_code == 200, response.text
        wrong = await _put_readback(h, TARGET_NAME, 0.1, float_payload(0.5))
        assert wrong.status_code == 502 and wrong.json()["code"] == "write_outcome_unknown"


async def test_integer_valued_float_for_an_enum_is_422_not_500(tmp_path) -> None:
    async with running_app(_settings_with_bool(tmp_path)) as h:
        response = await h.client.post(f"/api/v1/devices/main/actions/{ACTION_NAME}", json={"value": 1.0}, headers=WRITER)
        assert response.status_code == 422 and response.json()["code"] == "value_type_mismatch", response.text


@pytest.mark.parametrize("kind", [DataType.UINT8, DataType.INT32, DataType.ENUM])
def test_allowlist_rejects_a_float_for_integer_types(kind) -> None:
    entry = AllowlistEntry(name="x", data_type=kind, minimum=0, maximum=10)
    for value in (1.0, 1.5):
        with pytest.raises(WriteRejected) as info:
            Allowlist._check_value(entry, value)
        assert info.value.code == "value_type_mismatch"
    Allowlist._check_value(entry, 1)


@pytest.mark.parametrize("extra", [{"minimum": 0}, {"maximum": 1}, {"step": 1}, {"allowed_values": [0]}])
@pytest.mark.parametrize("kind", [DataType.BOOL, DataType.STRING])
def test_allowlist_rejects_restrictions_for_bool_and_string(kind, extra) -> None:
    with pytest.raises(ValidationError):
        AllowlistEntry(name="x", data_type=kind, **extra)
    AllowlistEntry(name="x", data_type=kind)
