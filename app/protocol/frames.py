#!/usr/bin/env python3
#
# app/protocol/frames.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Frame encoding and decoding for standard and plant frames."""

from dataclasses import dataclass

from app.errors import ProtocolError
from app.protocol.crc import crc16_ccitt
from app.protocol.escaping import escape_body
from app.protocol.types import LONG_COMMANDS, PLANT_BIT, START_BYTE, Command


class FrameError(ProtocolError):
    """A received frame body is malformed or fails its CRC."""

    code = "frame_error"


class UnknownCommand(FrameError):
    code = "unknown_command"

    def __init__(self, command: int) -> None:
        super().__init__(command=command)
        self.command = command


class CrcMismatch(FrameError):
    code = "crc_mismatch"

    def __init__(self, object_id: int, expected: int, received: int) -> None:
        super().__init__(object_id=object_id, expected=expected, received=received)
        self.object_id = object_id
        self.expected = expected
        self.received = received


@dataclass(frozen=True, slots=True)
class Frame:
    command: Command
    object_id: int
    payload: bytes = b""
    plant_address: int | None = None  # set -> plant frame, 4-byte address field

    @property
    def is_plant(self) -> bool:
        return self.plant_address is not None


def _body_parts(command: int, plant: bool) -> tuple[int, int]:
    """Return (length-field width, bytes of address plus object id) shared by both directions."""
    return (2 if command in LONG_COMMANDS else 1), (8 if plant else 4)


def encode_frame(frame: Frame) -> bytes:
    """Serialize: start byte, escaped body, escaped CRC."""
    command = int(frame.command)
    if bool(command & PLANT_BIT) != frame.is_plant:
        raise ValueError("plant address must be set exactly for plant commands")
    width, header = _body_parts(command, frame.is_plant)
    length = header + len(frame.payload)
    if length >= 1 << (8 * width):
        raise ValueError("payload too long for the length field")
    body = bytes([command]) + length.to_bytes(width, "big")
    if frame.plant_address is not None:
        body += frame.plant_address.to_bytes(4, "big")
    body += frame.object_id.to_bytes(4, "big") + frame.payload
    crc = crc16_ccitt(body).to_bytes(2, "big")
    return bytes([START_BYTE]) + escape_body(body) + escape_body(crc)


def decode_frame(body: bytes, *, measured_length: bool = False) -> Frame:
    """Decode one unescaped frame body. Raises FrameError on CRC or length mismatch.

    With ``measured_length`` the payload boundary is taken from ``len(body)`` instead of the
    length field, for a device that declares a length which does not match the frame end
    (measured on an RCT Power DC 10.0: 16 of 18 long responses, short responses never). The
    received length bytes stay part of the CRC input unchanged, so the CRC still has to verify
    over the declared bytes; only the boundary moves.
    """
    if len(body) < 1:
        raise FrameError("frame_too_short")
    raw_command = body[0]
    try:
        command = Command(raw_command)
    except ValueError:
        raise UnknownCommand(raw_command) from None
    plant = bool(raw_command & PLANT_BIT)
    width, header = _body_parts(raw_command, plant)
    if len(body) < 1 + width:
        raise FrameError("frame_too_short")
    length = int.from_bytes(body[1 : 1 + width], "big")
    too_short = len(body) < 1 + width + header + 2
    declared_mismatch = not measured_length and len(body) != 1 + width + length + 2
    if length < header or too_short or declared_mismatch:
        raise FrameError("length_mismatch", length=length, size=len(body))
    pos = 1 + width
    address = None
    if plant:
        address = int.from_bytes(body[pos : pos + 4], "big")
        pos += 4
    object_id = int.from_bytes(body[pos : pos + 4], "big")
    payload = body[pos + 4 : len(body) - 2]
    expected = crc16_ccitt(body[:-2])
    received = int.from_bytes(body[-2:], "big")
    if expected != received:
        raise CrcMismatch(object_id, expected, received)
    return Frame(command, object_id, payload, address)
