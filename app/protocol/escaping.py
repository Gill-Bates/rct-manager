#!/usr/bin/env python3
#
# app/protocol/escaping.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Stop-byte escaping, kept apart from the CRC (the stop byte is not part of it)."""

from app.protocol.types import START_BYTE, STOP_BYTE

_SPECIAL = (START_BYTE, STOP_BYTE)


def escape_body(body: bytes) -> bytes:
    """Prefix every 0x2B and 0x2D with the stop byte 0x2D."""
    out = bytearray()
    for byte in body:
        if byte in _SPECIAL:
            out.append(STOP_BYTE)
        out.append(byte)
    return bytes(out)


def unescape_body(data: bytes) -> bytes:
    """Resolve 0x2D 0x2D and 0x2D 0x2B into one payload byte each."""
    out = bytearray()
    it = iter(data)
    for byte in it:
        if byte == STOP_BYTE:
            nxt = next(it, None)
            if nxt not in _SPECIAL:
                raise ValueError("invalid escape sequence")
            out.append(nxt)
        else:
            out.append(byte)
    return bytes(out)
