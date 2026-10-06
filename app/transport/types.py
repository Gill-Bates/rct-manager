#!/usr/bin/env python3
#
# app/transport/types.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Types shared by the transport layer and the scheduling layer."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from app.config import DeviceKey
from app.errors import DeviceApiError
from app.protocol.frames import Frame
from app.protocol.types import PLANT_BIT, Command

if TYPE_CHECKING:
    from app.scheduling.budget import BudgetHandle


class TransactionOrigin(StrEnum):
    CALLER = "caller"  # counts against the work budget
    HEARTBEAT = "heartbeat"  # exempt (Requirement 6.12)
    SYSTEM_WRITE = "system_write"  # exempt
    SYSTEM_READ = "system_read"  # exempt: periodic-value refresh, bounded by its own per-cycle limit
    SHUTDOWN = "shutdown"  # exempt


@dataclass(slots=True)
class TransactionRequest:
    device_key: DeviceKey
    frame: Frame
    origin: TransactionOrigin
    kind: Literal["read", "write"]
    enqueued_at: datetime | None = None
    cache_key: tuple[str, str] | None = None  # set for cache-eligible reads
    recheck_cache: bool = False  # Requirement 15.13
    idempotent: bool = False  # registry flag; conservative default blocks automatic write retries
    is_action: bool = False  # action variables are never retried automatically
    abandoned: bool = False
    charge: "BudgetHandle | None" = None  # released by the serializer unless the transaction starts


@dataclass(frozen=True, slots=True)
class SendOutcome:
    """Result of one attempt to hand a request frame to the write channel."""

    committed: bool  # True as soon as the first byte was handed over
    sent_at: datetime | None  # UTC timestamp, set iff committed
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class TransactionResult:
    """One attempt: the response frame if any, the commit state and the error if it failed."""

    outcome: SendOutcome
    frame: Frame | None = None
    error: DeviceApiError | None = None
    skipped_by_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and (self.frame is not None or self.skipped_by_cache)

    @property
    def committed(self) -> bool:
        return self.outcome.committed


def make_frame(network_id: int | None, command: Command, object_id: int, payload: bytes = b"") -> Frame:
    """Build a standard frame, or the plant variant when the device has a network id."""
    header = 4 if network_id is None else 8
    if int(command) & ~PLANT_BIT == Command.WRITE and header + len(payload) > 255:
        command = Command.LONG_WRITE
    if network_id is None:
        return Frame(Command(int(command) & ~PLANT_BIT), object_id, payload)
    return Frame(Command(int(command) | PLANT_BIT), object_id, payload, network_id)
