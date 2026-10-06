#!/usr/bin/env python3
#
# app/api/runtime.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Runtime state shared with the routers through ``app.state.runtime``; ports only, no adapter types."""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request

from app.api.models import DeviceReadiness
from app.catalog.base import MetricCatalog
from app.clock import Clock
from app.config import DeviceEntry, Settings
from app.dispatch.base import BatteryDispatchPort
from app.errors import DeviceApiError, UnknownDevice, UnknownMetric
from app.gateway.base import DeviceGateway, DeviceState
from app.gateway.vendor import VendorDiagnostics
from app.observability.exporter import MetricsExporter
from app.scheduling.shutdown import ShutdownCoordinator

# A device is ready when it answers; a degraded device still delivers values.
READY_STATES = frozenset({DeviceState.OK, DeviceState.DEGRADED})
_NAME_ECHO = re.compile(r"[^\x20-\x7e]")


@dataclass(slots=True)
class Runtime:
    settings: Settings
    clock: Clock
    catalog: MetricCatalog
    gateway: DeviceGateway
    devices: dict[str, DeviceEntry]
    roles: dict[str, str]
    shutdown: ShutdownCoordinator | None = None
    exporter: MetricsExporter | None = None
    vendor: VendorDiagnostics | None = None
    tasks: list = field(default_factory=list)
    dispatch: BatteryDispatchPort | None = None
    export_task: asyncio.Task | None = None  # the running push-exporter task; an explicit handle (not list position)

    def shutting_down(self) -> bool:
        return self.shutdown is not None and self.shutdown.plan is not None

    def ensure_accepting(self) -> None:
        """Reject new device work once the shutdown began (Requirement 27.8)."""
        if self.shutting_down():
            raise DeviceApiError("not_ready")

    def device(self, device_id: str) -> DeviceEntry:
        entry = self.devices.get(device_id)
        if entry is None:
            raise UnknownDevice(device_id=device_id)
        return entry

    def metric(self, name: str) -> None:
        if not self.catalog.exists(name):
            raise UnknownMetric(name=name)

    def readiness(self) -> list[DeviceReadiness]:
        result = []
        for device_id in self.devices:
            status = self.gateway.device_status(device_id)
            result.append(DeviceReadiness(**{k: getattr(status, k) for k in DeviceReadiness.model_fields}))
        return result


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def echo_name(value: str) -> str:
    """Printable, length-limited form of a client supplied name for error texts."""
    return _NAME_ECHO.sub("?", value)[:64]
