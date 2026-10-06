#!/usr/bin/env python3
#
# app/protocol/values.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Conversion between payload bytes and scalar values (MSBF, two's complement, IEEE 754)."""

import struct
from collections.abc import Mapping

from app.errors import DecodeLengthMismatch
from app.protocol.types import DataType

type ScalarValue = bool | int | float | str

DEFAULT_WIDTHS: Mapping[DataType, int | None] = {
    DataType.BOOL: 1,
    DataType.UINT8: 1,
    DataType.INT8: 1,
    DataType.UINT16: 2,
    DataType.INT16: 2,
    DataType.UINT32: 4,
    DataType.INT32: 4,
    DataType.FLOAT: 4,
    DataType.ENUM: 4,
    DataType.STRING: None,
    DataType.STRUCT: None,
}

_SIGNED = frozenset({DataType.INT8, DataType.INT16, DataType.INT32})


def decode_string(payload: bytes, encoding: str = "utf-8") -> tuple[str, int]:
    """Decode up to the first NUL; return the text and the number of replacement characters."""
    text = payload.split(b"\x00", 1)[0].decode(encoding, errors="replace")
    return text, text.count("�")


def decode_value(
    data_type: DataType, payload: bytes, *, byte_width: int | None = None, encoding: str = "utf-8"
) -> ScalarValue:
    """Decode a payload; a length differing from the expected width raises DecodeLengthMismatch."""
    if data_type is DataType.STRUCT:
        raise ValueError("t_struct is decoded by its structure decoder")
    if data_type is DataType.STRING:
        if byte_width is not None and len(payload) != byte_width:
            raise DecodeLengthMismatch(byte_width, len(payload))
        return decode_string(payload, encoding)[0]
    width = byte_width if byte_width is not None else DEFAULT_WIDTHS[data_type]
    assert width is not None
    if len(payload) != width:
        raise DecodeLengthMismatch(width, len(payload))
    if data_type is DataType.FLOAT:
        if width != 4:
            raise ValueError("t_float is single precision (4 bytes)")
        return struct.unpack(">f", payload)[0]
    raw = int.from_bytes(payload, "big", signed=data_type in _SIGNED)
    if data_type is DataType.BOOL:
        return raw != 0
    return raw


def encode_value(
    data_type: DataType, value: ScalarValue, *, byte_width: int | None = None, encoding: str = "utf-8"
) -> bytes:
    """Encode a scalar; raises ValueError for a foreign type and OverflowError outside the range."""
    if data_type is DataType.STRUCT:
        raise ValueError("t_struct is not writable")
    if data_type is DataType.STRING:
        if not isinstance(value, str):
            raise ValueError("t_string requires str")
        data = value.encode(encoding)
        if byte_width is not None:
            if len(data) >= byte_width:
                raise ValueError("string does not fit the configured byte width")
            return data.ljust(byte_width, b"\x00")
        return data
    width = byte_width if byte_width is not None else DEFAULT_WIDTHS[data_type]
    assert width is not None
    if data_type is DataType.FLOAT:
        if width != 4:
            raise ValueError("t_float is single precision (4 bytes)")
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("t_float requires a numeric value")
        return struct.pack(">f", float(value))
    # Exact types only: a conversion would write a different value than the caller named.
    if data_type is DataType.BOOL:
        if type(value) is not bool:
            raise ValueError("t_bool requires bool")
        return int(value).to_bytes(width, "big")
    if type(value) is not int:
        raise ValueError(f"{data_type} requires int")
    return value.to_bytes(width, "big", signed=data_type in _SIGNED)
