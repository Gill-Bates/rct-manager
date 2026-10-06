#!/usr/bin/env python3
#
# tests/test_stream_device_frames.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Regression vectors from real device bytes: long frames whose length field is wrong.

The byte sequences live in tests/fixtures/device_long_frames.hex and come from two passive
captures of an RCT Power DC 10.0 (firmware 2.3.5687, 2026-10-02); not a single byte was sent
to the device. They are anonymised, and the header comment of the fixture states how: only
LONG RESPONSE frames are taken, because they carry nothing but logger timestamps and float
values, while the short 0x05 frames of the same capture - which carry the serial number and
the device name - are deliberately left out. Every sequence was checked for printable ASCII
runs of four characters or more; the one sequence that held such runs, (a), had them replaced
by 'X' of the same length with a recomputed CRC and a re-escaped body. Its declared length and
its measured body length are the originals, which is what these tests exercise.

The device declares a length that does not match the frame end in 16 of 18 long frames, so the
parser used to read two payload bytes as the checksum. Measured: 14 frames 16 bytes too small,
2 frames 472 and 480 bytes too large, 2 correct.
"""

import logging
import re
from pathlib import Path

from app.protocol.escaping import escape_body, unescape_body
from app.protocol.frames import Frame, encode_frame
from app.protocol.stream import StreamParser
from app.protocol.types import Command

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
