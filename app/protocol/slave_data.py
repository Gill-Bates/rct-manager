#!/usr/bin/env python3
#
# app/protocol/slave_data.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The 108-byte slave structure returned for net.slave_data."""

import math
import struct
from dataclasses import dataclass

from app.errors import DecodeLengthMismatch, ProtocolError
from app.protocol.values import decode_string

SLAVE_DATA_SIZE = 108

# Verified on a device: 108 bytes, all numeric fields little-endian (offset 0 matches net.id, SoC the live value).
# Offsets 88..107 are reserved and deliberately not exposed.
_NAME = slice(4, 28)
_VERSION = slice(48, 64)
_SERIAL = slice(64, 80)


@dataclass(frozen=True, slots=True)
class SlaveData:
    network_id: int  # offset 0, t_uint32
    name: str  # offset 4, 24 bytes
    ac_power_w: float  # offset 28
    battery_power_w: float  # offset 32
    battery_soc_ratio: float  # offset 36
    fault_index: int  # offset 40, t_uint16
    equipment_bits: int  # offset 42, t_uint8
    device_state: int  # offset 43, t_uint8
    external_power_w: float  # offset 44
    software_version: str  # offset 48, 16 bytes
    serial_number: str  # offset 64, 16 bytes
    completeness: int  # offset 80, t_uint32
    bms_software_version: int  # offset 84, t_uint32


def decode_slave_data(payload: bytes, *, encoding: str = "utf-8") -> SlaveData:
    if len(payload) != SLAVE_DATA_SIZE:
        raise DecodeLengthMismatch(SLAVE_DATA_SIZE, len(payload))
    (network_id,) = struct.unpack_from("<I", payload, 0)
    ac, battery, soc = struct.unpack_from("<fff", payload, 28)
    fault, equipment, state = struct.unpack_from("<HBB", payload, 40)
    (external,) = struct.unpack_from("<f", payload, 44)
    if not all(math.isfinite(value) for value in (ac, battery, soc, external)):
        raise ProtocolError("invalid_float")
    completeness, bms = struct.unpack_from("<II", payload, 80)
    return SlaveData(
        network_id=network_id,
        name=decode_string(payload[_NAME], encoding)[0],
        ac_power_w=ac,
        battery_power_w=battery,
        battery_soc_ratio=soc,
        fault_index=fault,
        equipment_bits=equipment,
        device_state=state,
        external_power_w=external,
        software_version=decode_string(payload[_VERSION], encoding)[0],
        serial_number=decode_string(payload[_SERIAL], encoding)[0],
        completeness=completeness,
        bms_software_version=bms,
    )


def _field(text: str, size: int, encoding: str) -> bytes:
    data = text.encode(encoding)
    if len(data) >= size:
        raise ValueError("string does not fit its field with a NUL terminator")
    return data.ljust(size, b"\x00")


def encode_slave_data(data: SlaveData, *, encoding: str = "utf-8") -> bytes:
    """Only used for the round-trip property; the application never writes net.slave_data."""
    out = bytearray(SLAVE_DATA_SIZE)
    struct.pack_into("<I", out, 0, data.network_id)
    out[_NAME] = _field(data.name, 24, encoding)
    struct.pack_into("<fff", out, 28, data.ac_power_w, data.battery_power_w, data.battery_soc_ratio)
    struct.pack_into("<HBB", out, 40, data.fault_index, data.equipment_bits, data.device_state)
    struct.pack_into("<f", out, 44, data.external_power_w)
    out[_VERSION] = _field(data.software_version, 16, encoding)
    out[_SERIAL] = _field(data.serial_number, 16, encoding)
    struct.pack_into("<II", out, 80, data.completeness, data.bms_software_version)
    return bytes(out)
