#!/usr/bin/env python3
#
# tests/test_stream_properties.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Properties 4 to 6 and stream edge cases."""

import logging
from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from app.protocol.crc import crc16_ccitt
from app.protocol.escaping import escape_body
from app.protocol.frames import Frame, encode_frame
from app.protocol.stream import StreamParser
from app.protocol.types import (
    BOOTLOADER_MAGIC,
    LONG_COMMANDS,
    PLANT_BIT,
    START_BYTE,
    STOP_BYTE,
    Command,
)
from tests.strategies import frames

PROP = settings(max_examples=200, deadline=None)


def _split(data: bytes, cuts: list[int]) -> list[bytes]:
    points = sorted({c % (len(data) + 1) for c in cuts} | {0, len(data)})
    return [data[a:b] for a, b in pairwise(points)]


# Feature: rct-manager, Property 4: any split of the stream leaves the result unchanged
@PROP
@given(st.lists(frames(), min_size=1, max_size=5), st.lists(st.integers(0, 10_000), max_size=20))
def test_arbitrary_split_gives_same_frames(sequence: list[Frame], cuts: list[int]) -> None:
    stream = b"".join(encode_frame(f) for f in sequence)
    whole = StreamParser().feed(stream)
    parser = StreamParser()
    parts: list[Frame] = []
    for chunk in _split(stream, cuts):
        parts.extend(parser.feed(chunk))
    assert whole == sequence
    assert parts == sequence
    assert parser.stats.discarded_bytes == 0


# Feature: rct-manager, Property 5: resynchronization after a disturbance
@PROP
@given(
    st.binary(max_size=60).map(lambda b: b.replace(bytes([START_BYTE]), b"\x01")),
    st.lists(frames(), min_size=1, max_size=3),
)
def test_resync_after_noise_prefix(noise: bytes, sequence: list[Frame]) -> None:
    parser = StreamParser()
    result = parser.feed(noise + b"".join(encode_frame(f) for f in sequence))
    assert result == sequence
    assert parser.stats.discarded_bytes >= len(noise) - 1


def test_leading_null_byte_is_skipped_without_counting() -> None:
    parser = StreamParser()
    frame = Frame(Command.READ, 5)
    assert parser.feed(b"\x00" + encode_frame(frame)) == [frame]
    assert parser.stats.discarded_bytes == 0


def test_unmasked_start_byte_restarts_frame() -> None:
    frame = Frame(Command.READ, 5)
    parser = StreamParser()
    assert parser.feed(b"\x2b\x01\x04\x00" + encode_frame(frame)) == [frame]


def test_invalid_escape_counts_framing_error_and_resyncs() -> None:
    frame = Frame(Command.READ, 5)
    parser = StreamParser()
    assert parser.feed(b"\x2b\x01\x2d\x99\x00" + encode_frame(frame)) == [frame]
    assert parser.stats.framing_errors == 1


def test_crc_error_is_counted_not_raised() -> None:
    raw = bytearray(encode_frame(Frame(Command.READ, 0x01020304)))
    raw[-1] ^= 0x01
    parser = StreamParser()
    good = Frame(Command.READ, 9)
    assert parser.feed(bytes(raw) + encode_frame(good)) == [good]
    assert parser.stats.crc_errors == 1


def test_crc_error_does_not_smuggle_an_escaped_inner_frame(caplog) -> None:
    """A CRC-defective outer frame may carry a syntactically complete, correctly
    escaped frame inside its payload. Discarding the outer frame must not let a resync that finds
    no further start byte fall back before the end of that already-consumed payload, or the inner
    frame is accepted as a new one once the preceding bytes are gone."""
    inner = Frame(Command.RESPONSE, 0x11223344, b"\x01\x02\x03")
    outer = bytearray(encode_frame(Frame(Command.RESPONSE, 0x55667788, encode_frame(inner))))
    outer[-1] ^= 0x01  # corrupt the CRC; the inner frame's own start byte stays escaped inside it
    parser = StreamParser()
    with caplog.at_level(logging.WARNING, logger="app.protocol.stream"):
        frames = parser.feed(bytes(outer))
    assert frames == []
    assert parser.stats.crc_errors == 1


def _encode_with_short_length(frame: Frame, shortfall: int) -> bytes:
    """Serialise a long frame whose length field is ``shortfall`` bytes too small.

    Mirrors the measured device: the length field is wrong, but the CRC is formed over the true
    body including those wrong length bytes.
    """
    header = 8 if frame.is_plant else 4
    declared = header + len(frame.payload) - shortfall
    body = bytes([int(frame.command)]) + declared.to_bytes(2, "big")
    if frame.plant_address is not None:
        body += frame.plant_address.to_bytes(4, "big")
    body += frame.object_id.to_bytes(4, "big") + frame.payload
    crc = crc16_ccitt(body).to_bytes(2, "big")
    return bytes([START_BYTE]) + escape_body(body) + escape_body(crc)


# Complements Property 4 (measured device behaviour): an under-declared long frame parses like a
# correctly declared one, as long as the CRC was formed over the true body.
@PROP
@given(
    st.sampled_from(sorted(LONG_COMMANDS)),
    st.integers(0, 2**32 - 1),
    st.binary(min_size=24, max_size=120),
    st.sampled_from([4, 8, 16, 24]),
    st.integers(0, 4),
)
def test_underdeclared_long_frame_parses_like_a_correct_one(
    command: Command, object_id: int, payload: bytes, shortfall: int, separators: int
) -> None:
    plant = int(command) & PLANT_BIT
    frame = Frame(command, object_id, payload, object_id if plant else None)
    trailer = Frame(Command.READ, 7)
    stream = _encode_with_short_length(frame, shortfall) + b"\x00" * separators + encode_frame(trailer)
    parser = StreamParser()
    assert parser.feed(stream) == [frame, trailer]
    assert parser.stats.length_field_corrected == 1
    assert parser.stats.crc_errors == 0


# Hypothesis found this case: with the length field 8 bytes short, the truncated body ends in bytes
# that happen to be a valid CRC (2**-16 chance). The outer frame boundary must win, so the parser
# may not return a shortened frame with the tail dropped as noise.
_COLLISION_PAYLOAD = (
    b'\x86\xa0\xce+Xx\x94\x0f\x14\x03<\xa4\xe0\xf5\x93\x05%\x8fT\xc0\x903>\xdc\x14X\xe6\x95\xe8\xa8\xf3"},'
    b"\x06\x8a\x03\x1d,o\xa0\xb5"
)


def test_crc_collision_of_a_short_declared_length_does_not_shorten_the_frame() -> None:
    frame = Frame(Command.LONG_RESPONSE_M, 5907, _COLLISION_PAYLOAD, 5907)
    wire = _encode_with_short_length(frame, 8)
    # Premise: read with its declared length, the truncated body is CRC-valid.
    declared = 8 + len(_COLLISION_PAYLOAD) - 8
    addresses = (5907).to_bytes(4, "big") * 2
    body = bytes([int(frame.command)]) + declared.to_bytes(2, "big") + addresses + _COLLISION_PAYLOAD
    cut = 3 + declared
    assert crc16_ccitt(body[:cut]) == int.from_bytes(body[cut : cut + 2], "big")
    trailer = Frame(Command.READ, 7)
    parser = StreamParser()
    assert parser.feed(wire + encode_frame(trailer)) == [frame, trailer]
    assert parser.stats.length_field_corrected == 1


def test_bootloader_magic_detected_across_chunks() -> None:
    parser = StreamParser()
    parser.feed(BOOTLOADER_MAGIC[:2])
    assert not parser.bootloader_magic_seen
    parser.feed(BOOTLOADER_MAGIC[2:])
    assert parser.bootloader_magic_seen


# Complements Property 4: CRC-valid frames are delivered exactly once, in order
@PROP
@given(st.lists(frames(), max_size=5))
def test_every_valid_frame_is_delivered_once(sequence: list[Frame]) -> None:
    parser = StreamParser()
    assert parser.feed(b"".join(encode_frame(f) for f in sequence)) == sequence


def test_oversize_frame_is_dropped_and_logged(caplog) -> None:
    parser = StreamParser(max_frame_bytes=64)
    big = encode_frame(Frame(Command.RESPONSE, 1, b"\x01" * 100))
    good = Frame(Command.READ, 2)
    with caplog.at_level(logging.WARNING):
        assert parser.feed(big + encode_frame(good)) == [good]
    assert parser.stats.oversize_frames == 1
    assert "0x05" in caplog.text
    assert "declared_length=104" in caplog.text


def test_long_frame_recovery_logs_framing_error_not_oversize(caplog) -> None:
    """An invalid escape pair hit while recovering a long frame's end must be
    logged as a framing error, not misreported as an oversize frame."""
    header = bytes([int(Command.LONG_RESPONSE)]) + (50).to_bytes(2, "big")
    body = b"\x01\x02" + bytes([STOP_BYTE, 0x99]) + b"\x03" * 10  # invalid escape pair 0x2D 0x99
    raw = bytes([START_BYTE]) + header + body
    parser = StreamParser(max_frame_bytes=4096)
    with caplog.at_level(logging.DEBUG, logger="app.protocol.stream"):
        frames = parser.feed(raw)
    assert frames == []
    assert parser.stats.framing_errors == 1
    assert parser.stats.oversize_frames == 0
    assert "invalid escaping" in caplog.text
    assert "oversize" not in caplog.text


def test_buffer_stays_bounded_for_escape_heavy_frames() -> None:
    parser = StreamParser(max_frame_bytes=64)
    raw = encode_frame(Frame(Command.RESPONSE, 0x2B2B2B2B, b"\x2b" * 50))
    peak = 0
    for i in range(len(raw) - 1):
        parser.feed(raw[i : i + 1])
        peak = max(peak, parser.buffered_bytes)
    assert peak <= 2 * 64


# Feature: rct-manager, Property 6: the frame class assignment (FrameKind) is unambiguous and complete
@PROP
@given(
    st.sets(st.integers(0, 15), max_size=6),
    st.one_of(st.none(), st.integers(0, 15)),
    st.integers(0, 15),
    st.sampled_from([None, 1, 2]),
)
def test_frame_class_assignment(
    periodic: set[int], pending_id: int | None, incoming_id: int, plant: int | None
) -> None:
    import asyncio

    from app.config import DeviceKey, EndpointKey
    from app.protocol.types import FrameKind
    from app.transport.counters import EndpointCounters
    from app.transport.demux import Demultiplexer, PendingTransaction

    async def scenario() -> None:
        counters = EndpointCounters()
        values: list[FrameKind] = []
        demux = Demultiplexer(counters, lambda: 10.0, lambda frame, kind: values.append(kind))
        key = DeviceKey(EndpointKey("h", 1), plant)
        for oid in periodic:
            demux.register_periodic(key, oid)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        if pending_id is not None:
            demux.pending = PendingTransaction(pending_id, plant, 5.0, future)
        command = Command.RESPONSE if plant is None else Command.RESPONSE_M
        frame = Frame(command, incoming_id, b"\x01", plant)
        kind = demux.dispatch(frame, 10.0)  # received after the pending send at 5.0
        is_pending = pending_id == incoming_id
        is_periodic = incoming_id in periodic
        if is_pending:
            assert kind is FrameKind.TRANSACTION_RESPONSE and future.done()
            assert values == ([FrameKind.PERIODIC_VALUE] if is_periodic else [FrameKind.TRANSACTION_RESPONSE])
        elif is_periodic:
            assert kind is FrameKind.PERIODIC_VALUE and counters.unexpected_frames == 0
        else:
            assert kind is FrameKind.UNEXPECTED and counters.unexpected_frames == 1 and not values

    asyncio.run(scenario())


def _run_receiver(frames_in: list[Frame]) -> tuple[object, object]:
    import asyncio
    from datetime import UTC, datetime

    from app.transport.counters import EndpointCounters
    from app.transport.demux import Demultiplexer
    from app.transport.receiver import ArrivalLedger, Receiver

    async def scenario() -> tuple[object, object]:
        counters = EndpointCounters()
        receiver = Receiver(
            parser=StreamParser(),
            demux=Demultiplexer(counters, lambda: 10.0),
            counters=counters,
            monotonic=lambda: 10.0,
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            max_frame_bytes=4096,
            unexpected_limit=50,
            unexpected_window_seconds=60,
            on_bootloader=lambda: None,
        )
        reader = asyncio.StreamReader()
        ledger = ArrivalLedger(reader, lambda: 10.0)
        reader.feed_data(b"".join(encode_frame(f) for f in frames_in))
        reader.feed_eof()
        return await receiver.run(reader, ledger), counters

    return asyncio.run(scenario())


def test_foreign_responses_count_as_foreign_access_but_never_force_a_reconnect() -> None:
    from app.transport.receiver import ReceiverExit

    # Another client scanning the inverter: the device mirrors every response to all connections.
    exit_reason, counters = _run_receiver([Frame(Command.RESPONSE, 0x6974798A + i, b"\x00") for i in range(100)])
    assert exit_reason is ReceiverExit.EOF
    assert counters.unexpected_in_window(10.0, 60) == 100
    assert counters.flood_in_window(10.0, 60) == 0


def test_foreign_response_flood_window_memory_is_bucketed_not_per_event() -> None:
    """A window counter must not grow by one entry per event however many land in
    the same instant, or an unlimited stream of well-formed foreign responses (exempt from the
    flood-reconnect limit) could grow the deque without bound."""
    from app.transport.counters import EndpointCounters

    counters = EndpointCounters()
    for _ in range(10_000):
        counters.record_unexpected(10.0, foreign_response=True)  # all in the same instant/bucket
    assert counters.unexpected_in_window(10.0, 60) == 10_000  # the count is still exact
    assert len(counters.unexpected_frames_window) == 1  # but it cost one bucket, not 10,000 entries
    assert len(counters.flood_frames_window) == 0  # foreign responses still never feed the flood path


def test_unexpected_non_response_frames_still_force_a_reconnect() -> None:
    from app.transport.receiver import ReceiverExit

    exit_reason, counters = _run_receiver([Frame(Command.READ, 0x6974798A + i) for i in range(51)])
    assert exit_reason is ReceiverExit.UNEXPECTED_FLOOD
    assert counters.flood_in_window(10.0, 60) == 51
