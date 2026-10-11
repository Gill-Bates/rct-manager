#!/usr/bin/env python3
#
# app/api/models_vendor.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Models of the vendor-specific diagnostic area; deliberately separate from ``models.py`` (Requirement 30.10)."""

from datetime import datetime

from pydantic import BaseModel

from app.protocol.types import DataType
from app.transport.endpoint import LockReason


class VendorObjectDescriptor(BaseModel):
    name: str
    object_id: str  # hex form, e.g. "0x400F015B"
    protocol_data_type: DataType
    effective_byte_width: int | None
    idempotent_write: bool


class VendorTransportDescriptor(BaseModel):
    endpoint_id: str
    host: str
    port: int
    device_ids: list[str]
    network_ids: list[int]
    discarded_bytes: int
    crc_errors: int
    framing_errors: int
    connection_epoch: int
    unexpected_frames: int
    locked: bool
    lock_reason: LockReason | None
    last_frame_at: datetime | None
    periodic_registrations: dict[str, int]
    periodic_available: dict[str, bool]
    periodic_setup_failures: dict[str, int]
    periodic_last_failure: dict[str, str | None]


class VendorSlaveDescriptor(BaseModel):
    network_id: int
    name: str
    ac_power_w: float
    battery_power_w: float
    battery_soc_ratio: float
    fault_index: int
    device_state: int
    external_power_w: float
    software_version: str
    serial_number: str
    bms_software_version: int
    battery_supported: bool  # equipment bit 0 (Requirement 18.14)
    battery_connected: bool  # bit 1
    dc_supported: bool  # bit 2
    external_power: bool  # bit 3


class VendorSlaveCollection(BaseModel):
    device_id: str
    slaves: list[VendorSlaveDescriptor]
    complete: bool
    error_code: str | None = None
