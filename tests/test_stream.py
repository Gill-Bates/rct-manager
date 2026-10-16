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
from app.transport.noise import (
    BURST_GAP_SECONDS,
    CRC_MIN_ERRORS,
    REMIND_SECONDS,
    CrcLogRateLimiter,
    StreamNoiseMonitor,
)

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


def _corrupt_crc_frame() -> bytes:
    """A frame whose CRC does not match its body: the parser must count it and drop it."""
    good = encode_frame(Frame(Command.RESPONSE, 0x1111, b"\x01\x02\x03\x04"))
    wire = bytearray(good)
    wire[-3] ^= 0x01  # flip a CRC byte so the frame fails CRC but still frames correctly
    return bytes(wire)


def test_single_crc_mismatch_is_counted_and_logged_at_debug_not_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The parser itself never WARNs about CRC errors (that would reset on every reconnect); it
    counts them and logs the per-frame detail at DEBUG. A real CRC failure is still dropped."""
    good = Frame(Command.RESPONSE, 0x2222, b"\x05\x06\x07\x08")
    parser = StreamParser()
    with caplog.at_level(logging.DEBUG, logger="app.protocol.stream"):
        frames = parser.feed(_corrupt_crc_frame() + NULL + encode_frame(good))
    assert frames == [good]  # the corrupt frame is dropped, the valid one still arrives
    assert parser.stats.crc_errors == 1
    records = [r for r in caplog.records if r.name == "app.protocol.stream"]
    assert records and all(r.levelno == logging.DEBUG for r in records)
    assert not [r for r in records if r.levelno >= logging.WARNING]
    assert any("CRC mismatch" in r.getMessage() for r in records)


def _crc_errors(limiter: CrcLogRateLimiter, now: float, errors: int, frames_each: int) -> float:
    """Record ``errors`` CRC drops against ``frames_each`` good frames at ``now``, then evaluate."""
    limiter.frames(now, frames_each)
    limiter.crc_errors(now, errors)
    limiter.evaluate(now)
    return now


def test_crc_rate_limiter_single_error_stays_quiet(caplog: pytest.LogCaptureFixture) -> None:
    limiter = CrcLogRateLimiter(WINDOW)
    with caplog.at_level(logging.DEBUG, logger="app.transport.receiver"):
        _crc_errors(limiter, 0.0, 1, 100)
    assert _levels(caplog) == []  # one invalid frame never escalates
    assert not limiter.disturbed


def test_crc_rate_limiter_tolerates_a_low_error_rate(caplog: pytest.LogCaptureFixture) -> None:
    limiter = CrcLogRateLimiter(WINDOW)
    with caplog.at_level(logging.INFO, logger="app.transport.receiver"):
        # CRC_MIN_ERRORS errors but against a lot of traffic: below CRC_WARN_RATIO, so no warning.
        _crc_errors(limiter, 0.0, CRC_MIN_ERRORS, 10_000)
    assert _levels(caplog) == []
    assert not limiter.disturbed


def test_crc_rate_limiter_warns_once_on_a_sustained_rate_and_clears(caplog: pytest.LogCaptureFixture) -> None:
    limiter = CrcLogRateLimiter(WINDOW)
    with caplog.at_level(logging.INFO, logger="app.transport.receiver"):
        _crc_errors(limiter, 0.0, 10, 20)  # 10 CRC errors against 20 frames: well above the ratio
        assert limiter.disturbed
        now = WINDOW
        for _ in range(int(WINDOW) + 5):  # one quiet window of clean traffic
            now += 1.0
            limiter.frames(now, 5)
            limiter.evaluate(now)
    assert _levels(caplog) == ["WARNING", "INFO"]  # one warning on escalation, one info on clear
    assert not limiter.disturbed


def test_crc_rate_limiter_shared_across_reconnects_does_not_rewarn(caplog: pytest.LogCaptureFixture) -> None:
    """A reconnect storm rebuilds the per-connection StreamParser, but the endpoint reuses ONE
    CrcLogRateLimiter, so a lasting CRC-error phase warns at most once per CRC_REMIND_SECONDS
    instead of once per reconnect."""
    limiter = CrcLogRateLimiter(WINDOW)
    with caplog.at_level(logging.WARNING, logger="app.transport.receiver"):
        now = 0.0
        for _ in range(10):  # ten short-lived connections, each sees a few CRC errors
            _crc_errors(limiter, now, 5, 20)
            now += 1.0  # all within one window and far below CRC_REMIND_SECONDS
    assert _levels(caplog) == ["WARNING"]  # not one WARNING per reconnect


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


def _overdeclared_long_frame(payload: bytes, object_id: int = 0x5293B668, declared: int = 488) -> bytes:
    """A long response declaring far more than it carries (like fixtures (c)/(d)): the declared
    length overshoots the buffer, so the frame end can only come from the framing recovery."""
    body = bytes([Command.LONG_RESPONSE]) + declared.to_bytes(2, "big") + object_id.to_bytes(4, "big") + payload
    crc = crc16_ccitt(body).to_bytes(2, "big")
    return b"\x2b" + escape_body(body) + escape_body(crc)


def _long_payload_with_crc(low_byte: int, high_byte: int) -> bytes:
    """Smallest 12-byte over-declared payload (last byte non-zero) whose CRC equals the wanted two
    bytes, so the frame ends in exactly that CRC after the recovery strips the trailing zero run."""
    want = (high_byte << 8) | low_byte
    for counter in range(1 << 20):
        payload = counter.to_bytes(3, "big") + bytes([0xA5]) * 9
        body = bytes([Command.LONG_RESPONSE]) + (488).to_bytes(2, "big") + (0x5293B668).to_bytes(4, "big") + payload
        if crc16_ccitt(body) == want:
            return payload
    raise AssertionError("no payload found")


def test_long_frame_with_a_zero_crc_is_recovered_in_full() -> None:
    """A long frame whose valid CRC is exactly 0x0000 ends in two genuine zero bytes. The recovery
    must keep both of them: trying only the frame end minus one zero verifies for about half of
    these frames (crc16(body[:-1]) == body[-1] << 8) and would silently drop the last payload byte.
    """
    payload = _long_payload_with_crc(0x00, 0x00)
    wire = _overdeclared_long_frame(payload)
    assert wire[-2:] == b"\x00\x00", "the frame must end in the two zero CRC bytes"
    for tail in (NULL + TRAILER, NULL, b""):
        parser = StreamParser()
        frames = parser.feed(wire + tail)
        longs = [f for f in frames if int(f.command) == int(Command.LONG_RESPONSE)]
        assert [f.payload for f in longs] == [payload], tail
        assert parser.stats.length_field_corrected == 1, tail
        assert parser.stats.crc_errors == 0, tail


def test_long_frame_with_a_zero_low_crc_byte_still_wins_over_the_longer_boundary() -> None:
    """The reverse guard: a CRC whose low byte is 0x00 but whose high byte is not ends in one zero,
    followed by the separator null. The true (shorter) end must still win; the longer boundary that
    would absorb the separator may only collide at 2**-16, not systematically."""
    payload = _long_payload_with_crc(0x00, 0x3A)
    wire = _overdeclared_long_frame(payload)
    assert wire[-2:] == b"\x3a\x00", "the frame must end in a 0x00 low CRC byte"
    parser = StreamParser()
    frames = parser.feed(wire + NULL + TRAILER)
    longs = [f for f in frames if int(f.command) == int(Command.LONG_RESPONSE)]
    assert [f.payload for f in longs] == [payload]
    assert parser.stats.length_field_corrected == 1
    assert parser.stats.crc_errors == 0


# Under-declared long response (declared 22, carried 38) whose first 12 payload bytes are followed
# by two bytes that happen to be the CRC of that prefix: header+prefix verifies at offset PREFIX_CUT.
PREFIX_COLLISION = bytes.fromhex("2b0600165293b6681001303132333435363738393ef6404142434445464748494a4b4c4d4e4f50515253547f")
PREFIX_COLLISION_OBJECT = 0x5293B668
PREFIX_CUT = 22
PREFIX_COLLISION_PAYLOAD = bytes.fromhex("1001303132333435363738393ef6404142434445464748494a4b4c4d4e4f50515253")


def test_prefix_collision_fixture_is_what_it_claims_to_be() -> None:
    assert PREFIX_COLLISION[:1] == b"\x2b"
    header_and_prefix = PREFIX_COLLISION[1 : PREFIX_CUT - 2]
    assert crc16_ccitt(header_and_prefix) == int.from_bytes(PREFIX_COLLISION[PREFIX_CUT - 2 : PREFIX_CUT], "big")
    whole = StreamParser().feed(PREFIX_COLLISION)
    assert [f.payload for f in whole] == [PREFIX_COLLISION_PAYLOAD]
    assert whole[0].object_id == PREFIX_COLLISION_OBJECT


@pytest.mark.parametrize("follower", [b"", NULL + TRAILER], ids=["alone", "back_to_back"])
def test_fragmented_long_frame_is_delivered_once_for_every_cut_outside_the_collision(follower: bytes) -> None:
    data = PREFIX_COLLISION + follower
    expected = [PREFIX_COLLISION_PAYLOAD] + ([TRAILER_PAYLOAD] if follower else [])
    for cut in range(1, len(data)):
        if cut == PREFIX_CUT:
            continue  # the ambiguous cut has its own test below
        parser = StreamParser()
        frames = parser.feed(data[:cut]) + parser.feed(data[cut:])
        assert [f.payload for f in frames] == expected, cut
        assert parser.stats.crc_errors == 0, cut


TRAILER_PAYLOAD = b"\x01"


@pytest.mark.xfail(
    strict=True,
    reason="Protocol ambiguity: a CRC-valid prefix of an under-declared long frame is byte-identical to a "
    "complete frame at the read boundary; only later bytes can tell them apart (2**-16 per evaluated cut).",
)
@pytest.mark.parametrize("follower", [b"", NULL + TRAILER], ids=["alone", "back_to_back"])
def test_crc_valid_prefix_at_a_read_boundary_is_not_delivered_early(follower: bytes) -> None:
    data = PREFIX_COLLISION + follower
    parser = StreamParser()
    assert parser.feed(data[:PREFIX_CUT]) == []
    frames = parser.feed(data[PREFIX_CUT:])
    assert [f.payload for f in frames] == [PREFIX_COLLISION_PAYLOAD] + ([TRAILER_PAYLOAD] if follower else [])


def _unsolicited_demux(clock):
    from app.config import DeviceKey, EndpointKey
    from app.transport.counters import EndpointCounters
    from app.transport.demux import Demultiplexer

    demux = Demultiplexer(EndpointCounters(), lambda: clock[0])
    demux.register_periodic(DeviceKey(EndpointKey("device", 8899), None), 0x1AC87AA0)
    return demux


def _unsolicited_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "Unsolicited response" in r.getMessage()]


def test_unsolicited_burst_logs_one_start_line_then_one_summary_per_window(caplog) -> None:
    """Requested and periodic frames stay silent; a burst of many objects is not one line per object."""
    from app.protocol.types import FrameKind
    from app.transport.types import make_frame

    clock = [10.0]
    demux = _unsolicited_demux(clock)
    periodic = make_frame(None, Command.RESPONSE, 0x1AC87AA0, b"\x00" * 4)
    with caplog.at_level("DEBUG", logger="app.transport.demux"):
        assert demux.dispatch(periodic, clock[0]) is FrameKind.PERIODIC_VALUE
        assert not _unsolicited_lines(caplog)
        for oid in range(0x1000, 0x1000 + 26):
            frame = make_frame(None, Command.RESPONSE, oid, b"\x00" * 4)
            assert demux.dispatch(frame, clock[0]) is FrameKind.UNEXPECTED
        lines = _unsolicited_lines(caplog)
        assert len(lines) == 1 and "burst started" in lines[0] and "object_id=0x00001000" in lines[0]
        clock[0] += 61.0  # the first frame after the window writes its summary and starts a new burst
        assert demux.dispatch(make_frame(None, Command.RESPONSE, 0x1000, b"\x00" * 4), clock[0]) is FrameKind.UNEXPECTED
    lines = _unsolicited_lines(caplog)
    assert len(lines) == 3
    assert "burst summary" in lines[1] and "26 frame(s), 26 object(s)" in lines[1]
    assert "burst started" in lines[2]


def test_single_unsolicited_burst_without_later_frame_is_visible_and_flushable(caplog) -> None:
    from app.transport.types import make_frame

    clock = [10.0]
    demux = _unsolicited_demux(clock)
    with caplog.at_level("DEBUG", logger="app.transport.demux"):
        for _ in range(3):
            demux.dispatch(make_frame(None, Command.RESPONSE, 0x16A1F844, b"\x00" * 4), clock[0])
        assert len(_unsolicited_lines(caplog)) == 1  # visible without any later frame
        clock[0] += 5.0
        demux.flush_unsolicited_log()  # connection end
    lines = _unsolicited_lines(caplog)
    assert len(lines) == 2 and "3 frame(s), 1 object(s)" in lines[1]
