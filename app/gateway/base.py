#!/usr/bin/env python3
#
# app/gateway/base.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""DeviceGateway port and vendor-neutral DTOs (Requirement 30.17, 30.19, 30.21).

No frame counters, lock cause, object id or network id appears here.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from app.protocol.values import ScalarValue
from app.scheduling.budget import BudgetHandle


class StaleReason(StrEnum):
    DEVICE_TIMEOUT = "device_timeout"
    DEVICE_UNREACHABLE = "device_unreachable"
    PROTOCOL_ERROR = "protocol_error"
    QUEUE_TIMEOUT = "queue_timeout"
    DEVICE_MAINTENANCE = "device_maintenance"


class DeviceState(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    MAINTENANCE = "maintenance"
    STARTING = "starting"


@dataclass(frozen=True, slots=True)
class MetricReading:
    name: str
    value: ScalarValue
    unit: str
    measured_at: datetime
    age_seconds: float
    source: Literal["device", "cache"]
    stale: bool
    stale_reason: StaleReason | None = None
    freshness: Literal["observed", "cached"] | None = None
    enum_label: str | None = None


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    name: str
    written_value: ScalarValue
    readback_value: ScalarValue | None
    confirmed: bool
    send_unconfirmed: bool
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    name: str
    requested_value: ScalarValue
    readback_value: ScalarValue | None
    timestamp: datetime
    action_confirmed: Literal[False] = False  # the protocol has no execution feedback (Requirement 9.15)


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    device_id: str
    state: DeviceState
    last_success_at: datetime | None
    last_heartbeat_at: datetime | None
    consecutive_failures: int
    queue_length: int
    foreign_access_suspected: bool
    liveness_source: Literal["transaction", "periodic", "heartbeat"] | None
    transactions: int
    failures: int
    cache_hits: int
    cache_misses: int


class DeviceGateway(Protocol):
    """Vendor-neutral device access; implementations never leak protocol details."""

    def refund_budget(self, device_id: str, reservation: BudgetHandle | None) -> None:
        """Release whatever a request-local reservation did not spend; a no-op for ``None``."""
        ...

    def reserve_budget(self, device_id: str, count: int = 1) -> BudgetHandle | None:
        """Reserve a request-local batch of budget units; raises BudgetExhausted."""
        ...

    async def read_metric(
        self, device_id: str, name: str, *, fresh: bool, charge: BudgetHandle | None = None
    ) -> MetricReading: ...

    async def write_metric(
        self, device_id: str, name: str, value: ScalarValue, *, system: bool = False
    ) -> WriteOutcome: ...

    async def trigger_action(self, device_id: str, name: str, value: ScalarValue) -> ActionOutcome: ...

    def device_status(self, device_id: str) -> DeviceStatus: ...

    def reported_name(self, device_id: str) -> str | None:
        """The device's own name, if read since startup; None otherwise."""
        ...
