#!/usr/bin/env python3
#
# tests/test_stream.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Stream decoding from real device bytes (wrong length fields) and the logging policy for unknown command bytes."""

import logging
import re
from pathlib import Path

import pytest

from app.protocol.crc import crc16_ccitt
from app.protocol.escaping import escape_body, unescape_body
from app.protocol.frames import Frame, encode_frame
from app.protocol.stream import StreamParser
from app.protocol.types import Command
from app.transport.noise import BURST_GAP_SECONDS, REMIND_SECONDS, StreamNoiseMonitor

FIXTURE = Path(__file__).parent / "fixtures" / "device_long_frames.hex"


NULL = b"\x00"  # the single separator byte the device puts between frames


_TAG = re.compile(r"#\s*\(([a-g])\)")


def _load() -> dict[str, bytes]:
    """Read the fixture: each hex line belongs to the tag of the comment above it."""
    sequences: dict[str, bytes] = {}
    tag: str | None = None
    for line in FIXTURE.read_text(encoding="ascii").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            match = _TAG.match(stripped)
            if match:
                tag = match.group(1)
            continue
        if not stripped:
            continue
        assert tag is not None, "hex line without a preceding tag comment"
        sequences[tag] = bytes.fromhex(stripped)
    return sequences


DEVICE = _load()


TRAILER = encode_frame(Frame(Command.RESPONSE, 0x11223344, b"\x01"))  # gives the scan a frame end


def test_fixture_holds_every_sequence() -> None:
    assert sorted(DEVICE) == ["a", "b", "c", "d", "e", "f", "g"]
    assert DEVICE["g"] == NULL


def test_underdeclared_long_frame_is_recovered() -> None:
    """(a) declared 16, measured 32: the frame is won instead of discarded."""
    parser = StreamParser()
    frames = parser.feed(DEVICE["a"] + NULL + TRAILER)
    assert [int(f.command) for f in frames] == [int(Command.LONG_RESPONSE), int(Command.RESPONSE)]
    assert frames[0].object_id == 0x5293B668
    assert len(frames[0].payload) == 28
    assert parser.stats.length_field_corrected == 1
    assert parser.stats.crc_errors == 0
    assert parser.stats.discarded_bytes == 0


def test_successful_length_corrections_only_log_debug(caplog) -> None:
    parser = StreamParser()
    with caplog.at_level(logging.DEBUG, logger="app.protocol.stream"):
        frames = parser.feed(DEVICE["a"] + NULL + DEVICE["c"] + NULL + TRAILER)
    assert len(frames) == 3
    assert parser.stats.length_field_corrected == 2
    records = [record for record in caplog.records if record.name == "app.protocol.stream"]
    assert len(records) == 2
    assert all(record.levelno == logging.DEBUG for record in records)
    assert all("Corrected length field" in record.getMessage() for record in records)


def test_recovery_and_escaping_work_together() -> None:
    """(b) declared 8, measured 24, with the timestamp byte 0x2B masked as 0x2D 0x2B."""
    assert b"\x6a\xc0\x2d\x2b\xb8" in DEVICE["b"], "the fixture must keep the masked timestamp"
    parser = StreamParser()
    frames = parser.feed(DEVICE["b"] + NULL + TRAILER)
    assert frames[0].object_id == 0x9247DB99
    assert len(frames[0].payload) == 20
    assert b"\x6a\xc0\x2b\xb8" in frames[0].payload, "the masked byte must be resolved in the payload"
    assert parser.stats.length_field_corrected == 1
    assert parser.stats.crc_errors == 0


def test_overdeclared_long_frames_are_recovered() -> None:
    """(c) and (d) declare 488 and carry 16 and 8; they used to be lost without any warning."""
    for tag, payload_size in (("c", 12), ("d", 4)):
        parser = StreamParser()
        frames = parser.feed(DEVICE[tag] + NULL + TRAILER)
        assert [int(f.command) for f in frames] == [int(Command.LONG_RESPONSE), int(Command.RESPONSE)], tag
        assert frames[0].object_id == 0xCBDAD315, tag
        assert len(frames[0].payload) == payload_size, tag
        assert parser.stats.length_field_corrected == 1, tag
        assert parser.stats.crc_errors == 0, tag


def test_overdeclared_long_frames_are_recovered_without_a_following_frame() -> None:
    """Without a trailer the end of the buffer is the provisional frame end; the CRC vouches for it."""
    for tag, payload_size in (("c", 12), ("d", 4)):
        for tail in (b"", NULL):
            parser = StreamParser()
            frames = parser.feed(DEVICE[tag] + tail)
            assert len(frames) == 1, (tag, tail)
            assert frames[0].object_id == 0xCBDAD315, tag
            assert len(frames[0].payload) == payload_size, tag
            assert parser.stats.length_field_corrected == 1, tag
            assert parser.stats.crc_errors == 0, tag


def test_overdeclared_long_frame_split_into_chunks_yields_one_frame() -> None:
    """Every split point of (c)/(d): exactly one frame, and none before the CRC-verified end."""
    for tag in ("c", "d"):
        data = DEVICE[tag]
        for cut in range(1, len(data)):
            parser = StreamParser()
            frames = parser.feed(data[:cut]) + parser.feed(data[cut:])
            assert len(frames) == 1, (tag, cut)
            assert frames[0].object_id == 0xCBDAD315, (tag, cut)


def test_correct_long_frame_in_chunks_is_not_recovered_early() -> None:
    """A correctly declared frame arriving in pieces is decoded once, at its declared end."""
    data = DEVICE["e"]
    for cut in range(1, len(data)):
        parser = StreamParser()
        early = parser.feed(data[:cut])
        assert early == [], cut
        frames = parser.feed(data[cut:])
        assert len(frames) == 1, cut
        assert len(frames[0].payload) == 476, cut
        assert parser.stats.length_field_corrected == 0, cut
        assert parser.stats.crc_errors == 0, cut


def test_correctly_declared_long_frame_takes_the_fast_path() -> None:
    """(e) declared 480, measured 480: nothing is corrected and no trailer is needed."""
    parser = StreamParser()
    frames = parser.feed(DEVICE["e"])
    assert len(frames) == 1
    assert frames[0].object_id == 0x3906A1D0
    assert len(frames[0].payload) == 476
    assert parser.stats.length_field_corrected == 0
    assert parser.stats.crc_errors == 0
    assert parser.stats.discarded_bytes == 0


def test_serial_bridge_burst_is_discarded_between_two_frames(caplog) -> None:
    """(f) '+++' and UART noise: both frames survive, the bytes are counted, a single burst stays DEBUG."""
    first = Frame(Command.RESPONSE, 0x0A0B0C0D, b"\x02\x03")
    second = Frame(Command.RESPONSE, 0x0A0B0C0E, b"\x04")
    stream = encode_frame(first) + DEVICE["f"] + NULL + encode_frame(second)
    parser = StreamParser()
    with caplog.at_level(logging.WARNING):
        frames = parser.feed(stream)
    assert frames == [first, second]
    assert parser.stats.discarded_bytes == len(DEVICE["f"])
    assert parser.stats.unknown_commands == 2  # 0x0d from the '+++\r' and 0xf8 from the noise
    assert parser.stats.last_unknown_command == 0xF8
    assert caplog.records == [], "the parser itself must stay silent; the receiver rate-limits"
    assert _receiver_warnings(stream) == 0


def test_bursts_within_one_read_count_once() -> None:
    """Many unknown command bytes at one instant are one burst: no warning on a quiet line."""
    frame = encode_frame(Frame(Command.RESPONSE, 0x0A0B0C0D, b"\x02"))
    assert _receiver_warnings((DEVICE["f"] + NULL + frame) * 20) == 0


def test_any_split_of_the_device_bytes_yields_the_same_frames() -> None:
    """Criterion 2.12: the result must not depend on how the stream arrives in chunks."""
    stream = NULL.join([DEVICE[tag] for tag in "abcde"] + [DEVICE["f"], TRAILER])
    expected = StreamParser().feed(stream)
    assert len(expected) == 6
    for size in range(1, len(stream) + 1):
        parser = StreamParser()
        frames: list[Frame] = []
        for offset in range(0, len(stream), size):
            frames.extend(parser.feed(stream[offset : offset + size]))
        assert frames == expected, f"chunk size {size} changed the result"
        assert parser.stats.length_field_corrected == 4
        assert parser.stats.crc_errors == 0


def test_single_bit_error_in_the_payload_is_still_discarded() -> None:
    """Negative probe: the recovery must not rescue a frame that is genuinely corrupt."""
    body = bytearray(unescape_body(DEVICE["a"][1:]))
    body[20] ^= 0x01  # one payload bit, far from the length field and the CRC
    assert body[20] not in (0x2B, 0x2D), "the flipped byte must not change the escaping"
    corrupt = bytes([DEVICE["a"][0]]) + escape_body(bytes(body[:-2])) + escape_body(bytes(body[-2:]))
    good = Frame(Command.RESPONSE, 0x01020304, b"\x07")
    parser = StreamParser()
    assert parser.feed(corrupt + NULL + encode_frame(good)) == [good]
    assert parser.stats.crc_errors == 1
    assert parser.stats.length_field_corrected == 0


def test_long_frame_reaches_the_demultiplexer_with_its_full_payload() -> None:
    """(e) through receiver and demultiplexer: 476 payload bytes arrive, not just a CRC-valid frame."""
    import asyncio
    from datetime import UTC, datetime

    from app.config import DeviceKey, EndpointKey
    from app.protocol.types import FrameKind
    from app.transport.counters import EndpointCounters
    from app.transport.demux import Demultiplexer
    from app.transport.receiver import ArrivalLedger, Receiver, ReceiverExit

    async def scenario() -> tuple[ReceiverExit, list[tuple[Frame, FrameKind]]]:
        seen: list[tuple[Frame, FrameKind]] = []
        counters = EndpointCounters()
        demux = Demultiplexer(counters, lambda: 10.0, lambda frame, kind: seen.append((frame, kind)))
        demux.register_periodic(DeviceKey(EndpointKey("device", 8899), None), 0x3906A1D0)
        receiver = Receiver(
            parser=StreamParser(),
            demux=demux,
            counters=counters,
            monotonic=lambda: 10.0,
            now=lambda: datetime(2026, 10, 2, tzinfo=UTC),
            max_frame_bytes=4096,
            unexpected_limit=50,
            unexpected_window_seconds=60,
            on_bootloader=lambda: None,
        )
        reader = asyncio.StreamReader()
        ledger = ArrivalLedger(reader, lambda: 10.0)
        reader.feed_data(DEVICE["e"])
        reader.feed_eof()
        return await receiver.run(reader, ledger), seen

    exit_reason, seen = asyncio.run(scenario())
    assert exit_reason is ReceiverExit.EOF
    assert len(seen) == 1
    frame, kind = seen[0]
    assert kind is FrameKind.PERIODIC_VALUE
    assert int(frame.command) == int(Command.LONG_RESPONSE)
    assert len(frame.payload) == 476


def _receiver_warnings(stream: bytes) -> int:
    """Number of WARNING records the receiver emits for the unknown command bytes in ``stream``."""
    import asyncio
    from datetime import UTC, datetime

    from app.transport.counters import EndpointCounters
    from app.transport.demux import Demultiplexer
    from app.transport.receiver import ArrivalLedger, Receiver

    async def scenario() -> None:
        counters = EndpointCounters()
        receiver = Receiver(
            parser=StreamParser(),
            demux=Demultiplexer(counters, lambda: 10.0),
            counters=counters,
            monotonic=lambda: 10.0,  # one window, so one warning at most
            now=lambda: datetime(2026, 10, 2, tzinfo=UTC),
            max_frame_bytes=4096,
            unexpected_limit=50,
            unexpected_window_seconds=60,
            on_bootloader=lambda: None,
        )
        reader = asyncio.StreamReader()
        ledger = ArrivalLedger(reader, lambda: 10.0)
        for offset in range(0, len(stream), 8):  # several reads, still one warning
            reader.feed_data(stream[offset : offset + 8])
        reader.feed_eof()
        await receiver.run(reader, ledger)

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect(level=logging.WARNING)
    logger = logging.getLogger("app.transport.receiver")
    logger.addHandler(handler)
    try:
        asyncio.run(scenario())
    finally:
        logger.removeHandler(handler)
    return len(records)


WINDOW = 60.0


def _levels(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.levelname for r in caplog.records if r.levelno >= logging.INFO]


def _bursts(monitor: StreamNoiseMonitor, start: float, count: int, frames_each: int) -> float:
    """``count`` bursts BURST_GAP_SECONDS apart, each followed by ``frames_each`` good frames."""
    now = start
    for _ in range(count):
        monitor.unknown(now, 2, 0x0D)
        monitor.frames(now, frames_each)
        monitor.evaluate(now)
        now += BURST_GAP_SECONDS
    return now


def test_single_burst_stays_debug(caplog: pytest.LogCaptureFixture) -> None:
    monitor = StreamNoiseMonitor(WINDOW)
    with caplog.at_level(logging.DEBUG, logger="app.transport.receiver"):
        _bursts(monitor, 0.0, 1, 0)
    assert _levels(caplog) == []
    assert [r.levelname for r in caplog.records] == ["DEBUG"]


def test_bytes_inside_the_burst_gap_are_one_burst(caplog: pytest.LogCaptureFixture) -> None:
    monitor = StreamNoiseMonitor(WINDOW)
    with caplog.at_level(logging.INFO, logger="app.transport.receiver"):
        for i in range(10):
            monitor.unknown(i * 0.1, 2, 0x0D)
            monitor.evaluate(i * 0.1)
    assert _levels(caplog) == []


def test_bursts_on_a_busy_line_are_tolerated(caplog: pytest.LogCaptureFixture) -> None:
    """5 bursts against 1000 frames are 0.5 %: normal for a shared device."""
    monitor = StreamNoiseMonitor(WINDOW)
    with caplog.at_level(logging.INFO, logger="app.transport.receiver"):
        _bursts(monitor, 0.0, 5, 200)
    assert _levels(caplog) == []
    assert not monitor.disturbed


def test_frequent_noise_warns_once_and_clears(caplog: pytest.LogCaptureFixture) -> None:
    monitor = StreamNoiseMonitor(WINDOW)
    with caplog.at_level(logging.INFO, logger="app.transport.receiver"):
        now = _bursts(monitor, 0.0, 6, 10)  # 6 bursts against 60 frames: 10 %
        assert monitor.disturbed
        now = _bursts(monitor, now, 4, 10)  # still disturbed: no second warning
        for _ in range(int(WINDOW) + 5):  # one quiet window with traffic
            now += 1.0
            monitor.frames(now, 5)
            monitor.evaluate(now)
    assert _levels(caplog) == ["WARNING", "INFO"]
    assert not monitor.disturbed


def test_frame_window_memory_is_bucketed_not_per_event() -> None:
    """Many frames arriving in the same instant must cost one bucket, not one deque
    entry each, or a high-rate stream could grow _frames without bound."""
    monitor = StreamNoiseMonitor(WINDOW)
    for _ in range(5000):
        monitor.frames(10.0, 1)  # all in the same instant/bucket
    assert monitor._frame_total == 5000
    assert len(monitor._frames) == 1


def test_lasting_noise_is_reminded_rarely(caplog: pytest.LogCaptureFixture) -> None:
    monitor = StreamNoiseMonitor(WINDOW)
    with caplog.at_level(logging.WARNING, logger="app.transport.receiver"):
        now = 0.0
        while now < 2 * REMIND_SECONDS + 10:  # first warning after 3 bursts, then +900 s each
            now = _bursts(monitor, now, 1, 10)
    assert _levels(caplog) == ["WARNING"] * 3  # start, plus one reminder per REMIND_SECONDS


def _underdeclared_long_frame(payload: bytes, object_id: int = 0x22334455, deficit: int = 16) -> bytes:
    """A long response as the device sends it: declared length too small, CRC over the sent bytes."""
    declared = 4 + len(payload) - deficit
    body = bytes([Command.LONG_RESPONSE]) + declared.to_bytes(2, "big") + object_id.to_bytes(4, "big") + payload
    crc = crc16_ccitt(body).to_bytes(2, "big")
    return b"\x2b" + escape_body(body) + escape_body(crc)


def _payload_with_escaped_crc_tail(stop_byte: int) -> bytes:
    """Deterministic 32-byte payload whose frame ends in an escape pair (low CRC byte 0x2B or 0x2D)."""
    for counter in range(4096):
        payload = counter.to_bytes(2, "big") + bytes(30)
        wire = _underdeclared_long_frame(payload)
        if wire[-2:] == bytes([0x2D, stop_byte]):
            return payload
    raise AssertionError("no payload found")


@pytest.mark.parametrize("stop_byte", [0x2B, 0x2D])
def test_underdeclared_long_frame_ending_in_an_escape_pair_is_delivered_without_a_follower(stop_byte: int) -> None:
    """A complete escape pair at the buffer end is no dangling escape byte; waiting for a follower would time out."""
    payload = _payload_with_escaped_crc_tail(stop_byte)
    wire = _underdeclared_long_frame(payload)
    for cut in (len(wire), len(wire) - 1):
        parser = StreamParser()
        frames = parser.feed(wire[:cut]) + parser.feed(wire[cut:])
        assert [f.payload for f in frames] == [payload], cut
        assert parser.stats.crc_errors == 0
