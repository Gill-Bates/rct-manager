#!/usr/bin/env python3
#
# app/protocol/crc.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""CRC16-CCITT as used by the RCT serial protocol."""


def crc16_ccitt(body: bytes) -> int:
    """CRC16-CCITT, polynomial 0x1021, seed 0xFFFF, zero-padded to even length."""
    if len(body) % 2:
        body += b"\x00"
    crc = 0xFFFF
    for byte in body:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc
