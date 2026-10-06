#!/usr/bin/env python3
#
# app/protocol/types.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Protocol constants and enumerations."""

from enum import IntEnum, StrEnum


class Command(IntEnum):
    READ = 0x01
    WRITE = 0x02
    LONG_WRITE = 0x03
    RESPONSE = 0x05
    LONG_RESPONSE = 0x06
    READ_PERIODICALLY = 0x08
    READ_M = 0x41
    WRITE_M = 0x42
    LONG_WRITE_M = 0x43
    RESPONSE_M = 0x45
    LONG_RESPONSE_M = 0x46
    READ_PERIODICALLY_M = 0x48


PLANT_BIT = 0x40  # bit 6 marks the plant-network variant
LONG_COMMANDS = frozenset({Command.LONG_WRITE, Command.LONG_RESPONSE, Command.LONG_WRITE_M, Command.LONG_RESPONSE_M})
WRITE_COMMANDS = frozenset({Command.WRITE, Command.LONG_WRITE, Command.WRITE_M, Command.LONG_WRITE_M})
START_BYTE = 0x2B
STOP_BYTE = 0x2D
BOOTLOADER_MAGIC = b"\x50\xf7\x05\xab"


class DataType(StrEnum):
    BOOL = "t_bool"
    UINT8 = "t_uint8"
    INT8 = "t_int8"
    UINT16 = "t_uint16"
    INT16 = "t_int16"
    UINT32 = "t_uint32"
    INT32 = "t_int32"
    FLOAT = "t_float"
    ENUM = "t_enum"
    STRING = "t_string"
    STRUCT = "t_struct"


class StructKind(StrEnum):
    SLAVE_DATA = "slave_data"  # the only admissible struct kind


class FrameKind(StrEnum):
    TRANSACTION_RESPONSE = "transaction_response"
    PERIODIC_VALUE = "periodic_value"
    UNEXPECTED = "unexpected"
