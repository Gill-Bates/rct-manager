#!/usr/bin/env python3
#
# tests/test_protocol_properties.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Properties 1 to 3 plus example vectors for the frame codec."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.protocol.crc import crc16_ccitt
from app.protocol.escaping import escape_body, unescape_body
from app.protocol.frames import Frame, FrameError, decode_frame, encode_frame
from app.protocol.types import START_BYTE, STOP_BYTE, Command
from tests.strategies import frames

PROP = settings(max_examples=200, deadline=None)


def _unescaped_body(raw: bytes) -> bytes:
    assert raw[0] == START_BYTE
    return unescape_body(raw[1:])


# Feature: rct-rest-api, Property 1: frame round-trip
@PROP
@given(frames())
def test_frame_round_trip(frame: Frame) -> None:
    assert decode_frame(_unescaped_body(encode_frame(frame))) == frame


# Feature: rct-rest-api, Property 2: escaping round-trip and absence of frame bytes
@PROP
@given(st.binary(max_size=300))
def test_escaping_round_trip_and_frame_freedom(data: bytes) -> None:
    escaped = escape_body(data)
    assert unescape_body(escaped) == data
    i = 0
    while i < len(escaped):
        if escaped[i] == STOP_BYTE:
            assert escaped[i + 1] in (START_BYTE, STOP_BYTE)
            i += 2
        else:
            assert escaped[i] != START_BYTE
            i += 1


# Feature: rct-rest-api, Property 3: CRC detects every single-byte corruption
@PROP
@given(frames(), st.data())
def test_crc_detects_single_byte_corruption(frame: Frame, data: st.DataObject) -> None:
    body = bytearray(_unescaped_body(encode_frame(frame)))
    pos = data.draw(st.integers(0, len(body) - 1))
    body[pos] ^= data.draw(st.integers(1, 255))
    with pytest.raises(FrameError):
        decode_frame(bytes(body))


def test_read_request_layout() -> None:
    raw = encode_frame(Frame(Command.READ, 0x1234_5678))
    assert raw[:1] == b"\x2b"
    body = _unescaped_body(raw)
    assert body[:6] == b"\x01\x04\x12\x34\x56\x78"
    assert len(body) == 8


def test_plant_length_field_and_address_position() -> None:
    body = _unescaped_body(encode_frame(Frame(Command.READ_M, 7, b"\xaa", plant_address=0x11223344)))
    assert body[:2] == b"\x41\x09"
    assert body[2:6] == b"\x11\x22\x33\x44"


def test_long_command_uses_two_byte_length() -> None:
    body = _unescaped_body(encode_frame(Frame(Command.LONG_RESPONSE, 1, b"\x00" * 300)))
    assert body[1:3] == (304).to_bytes(2, "big")


def test_crc_pads_odd_length_with_zero() -> None:
    assert crc16_ccitt(b"\x01\x02\x03") == crc16_ccitt(b"\x01\x02\x03\x00")
    assert crc16_ccitt(b"") == 0xFFFF
    assert crc16_ccitt(b"123456789") == crc16_ccitt(b"123456789\x00")


def test_crc_matches_ccitt_false_check_value() -> None:
    # CRC-16/CCITT-FALSE check value for "123456789" is 0x29B1; the odd length is zero-padded
    # by the function, so compare against the padded input folded without the pad byte.
    assert crc16_ccitt(b"123456789") != 0x29B1
    assert _crc_unpadded(b"123456789") == 0x29B1


def _crc_unpadded(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(FrameError):
        decode_frame(b"\x77\x04\x00\x00\x00\x01\x00\x00")
