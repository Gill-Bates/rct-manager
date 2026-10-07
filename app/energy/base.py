#!/usr/bin/env python3
#
# app/energy/base.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The port the routers see. ``app/api/runtime.py`` keeps its "ports only, no adapter types" rule by
typing its ``energy`` field against this protocol instead of importing ``EnergyManager``; a router
can therefore be tested against a stub manager.
"""

from typing import Protocol

from app.dispatch.capabilities import GateDecision
from app.energy.models import (
    ArmedRecord,
    EnergyAction,
    EnergyCommand,
    EnergyDeviceStatus,
)


class EnergyManagerPort(Protocol):
    async def status(self, device_id: str) -> EnergyDeviceStatus: ...
    async def command(
        self, device_id: str, command: EnergyCommand, *, actor: str | None
    ) -> EnergyDeviceStatus: ...
    async def set_armed(
        self, device_id: str, *, armed: bool, actor: str | None
    ) -> EnergyDeviceStatus: ...
    def armed(self, device_id: str) -> bool: ...


class EnergyAdminPort(EnergyManagerPort, Protocol):
    """What the session-authenticated admin surface may additionally read.

    The two accessors are separated from the public port on purpose: the raw gate decision and the
    armed row's bookkeeping are admin-only (design 2.8), and the public router must not be able to
    reach them by accident.
    """

    def armed_record(self, device_id: str) -> ArmedRecord: ...
    def gate_decisions(self, device_id: str) -> tuple[tuple[EnergyAction, GateDecision], ...]: ...
