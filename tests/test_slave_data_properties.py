#!/usr/bin/env python3
#
# tests/test_slave_data_properties.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Property 17 (slave structure share)."""

import math
import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.errors import DecodeLengthMismatch, ProtocolError
from app.protocol.slave_data import (
    SLAVE_DATA_SIZE,
    SlaveData,
    decode_slave_data,
    encode_slave_data,
)

_f32 = st.floats(width=32, allow_nan=False, allow_infinity=False)
_text = st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=15)


@st.composite
def slave_data(draw: st.DrawFn) -> SlaveData:
    return SlaveData(
        network_id=draw(st.integers(0, 2**32 - 1)),
        name=draw(_text),
        ac_power_w=draw(_f32),
        battery_power_w=draw(_f32),
        battery_soc_ratio=draw(_f32),
        fault_index=draw(st.integers(0, 2**16 - 1)),
        equipment_bits=draw(st.integers(0, 255)),
        device_state=draw(st.integers(0, 255)),
        external_power_w=draw(_f32),
        software_version=draw(_text),
        serial_number=draw(_text),
        completeness=draw(st.integers(0, 2**32 - 1)),
        bms_software_version=draw(st.integers(0, 2**32 - 1)),
    )


# Feature: rct-manager, Property 17: value round-trip (slave structure share)
@settings(max_examples=200, deadline=None)
@given(slave_data())
def test_slave_data_round_trip(value: SlaveData) -> None:
    raw = encode_slave_data(value)
    assert len(raw) == SLAVE_DATA_SIZE
    assert raw[88:] == b"\x00" * 20
    assert decode_slave_data(raw) == value


def test_wrong_length_reports_expected_and_received() -> None:
    with pytest.raises(DecodeLengthMismatch) as info:
        decode_slave_data(b"\x00" * 100)
    assert (info.value.expected, info.value.received) == (108, 100)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_float_is_rejected(bad: float) -> None:
    raw = bytearray(SLAVE_DATA_SIZE)
    struct.pack_into("<f", raw, 28, bad)  # ac_power_w
    with pytest.raises(ProtocolError):
        decode_slave_data(bytes(raw))


def test_offsets_follow_the_specification() -> None:
    raw = bytearray(108)
    raw[0:4] = b"\x4b\x79\x42\xcb"  # net.id 0xCB42794B as sent by a device, little-endian
    raw[36:40] = b"\x00\x00\x00\x3f"  # 0.5, little-endian
    raw[40:42] = b"\x01\x00"
    raw[42] = 0b1010
    raw[84:88] = b"\xeb\x15\x00\x00"
    value = decode_slave_data(bytes(raw))
    assert value.network_id == 0xCB42794B
    assert math.isclose(value.battery_soc_ratio, 0.5)
    assert value.fault_index == 1
    assert value.equipment_bits == 0b1010
    assert value.bms_software_version == 5611
