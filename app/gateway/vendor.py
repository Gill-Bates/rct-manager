#!/usr/bin/env python3
#
# app/gateway/vendor.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Vendor-specific diagnostics port; only the diagnostic area may use it (Requirement 30)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.catalog.registry import RegistryEntry
from app.protocol.slave_data import SlaveData
from app.transport.endpoint import LockReason


@dataclass(frozen=True, slots=True)
class TransportInfo:
    endpoint_id: str
    host: str
    port: int
    device_ids: tuple[str, ...]
    network_ids: tuple[int, ...]
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


@dataclass(frozen=True, slots=True)
class SlaveDiscovery:
    slaves: tuple[SlaveData, ...]
    complete: bool
    error_code: str | None = None


class VendorDiagnostics(Protocol):
    def objects(self) -> list[RegistryEntry]: ...

    def transports(self) -> list[TransportInfo]: ...

    async def discover_slaves(self, device_id: str) -> SlaveDiscovery: ...
