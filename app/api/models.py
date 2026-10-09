#!/usr/bin/env python3
#
# app/api/models.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Vendor-neutral response models (Requirement 30.1, 30.19, 30.21).

No field carries an object id, network id, transport address or port, protocol data type,
frame counter, lock state or periodic registration count.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.catalog.base import NeutralValueType
from app.gateway.base import DeviceState, StaleReason
from app.protocol.values import ScalarValue

__all__ = [
    "ActionResult",
    "DeviceDescriptor",
    "DeviceReadiness",
    "DeviceState",
    "MetricCollection",
    "MetricDescriptor",
    "MetricError",
    "MetricValue",
    "NeutralValueType",
    "ReadinessResponse",
    "StaleReason",
    "WriteResult",
]


class MetricDescriptor(BaseModel):
    name: str
    unit: str
    value_type: NeutralValueType
    writable: bool
    preselected: bool


class DeviceDescriptor(BaseModel):
    device_id: str
    display_name: str
    role: Literal["master", "slave", "standalone"]
    ready: bool


class MetricValue(BaseModel):
    name: str
    value: ScalarValue | None
    unit: str
    timestamp: datetime  # timezone-aware UTC
    age_seconds: float
    stale: bool
    source: Literal["device", "cache"]
    stale_reason: StaleReason | None = None
    freshness: Literal["observed", "cached"] | None = Field(
        default=None,
        description=(
            "For fresh=true: observed means a value delivered by the requested device transaction; "
            "cached means a retained cache value returned after a recoverable device error. "
            "Null for ordinary reads. Inspect source, age_seconds and stale_reason as well."
        ),
    )
    enum_value: int | None = None
    enum_label: str | None = None


class MetricError(BaseModel):
    name: str
    code: str
    detail: str


class MetricCollection(BaseModel):
    device_id: str
    metrics: list[MetricValue]
    errors: list[MetricError] = Field(default_factory=list)


class WriteResult(BaseModel):
    device_id: str
    name: str
    written_value: ScalarValue
    readback_value: ScalarValue | None
    confirmed: bool = Field(description="True when readback matches the value encoded for the write.")
    send_unconfirmed: bool = Field(
        default=False,
        description=(
            "True when no successful WRITE response was received, but readback still matched. "
            "A successful write is confirmed by readback even when the device does not acknowledge WRITE."
        ),
    )
    timestamp: datetime


class ActionResult(BaseModel):
    device_id: str
    name: str
    requested_value: ScalarValue
    readback_value: ScalarValue | None
    action_confirmed: Literal[False] = False  # the protocol has no execution feedback (Requirement 9.15)
    action_note: str
    timestamp: datetime


class DeviceReadiness(BaseModel):
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
    periodic_available: bool | None = None
    periodic_setup_failures: int = 0


class ReadinessResponse(BaseModel):
    ready: bool
    devices: list[DeviceReadiness]
