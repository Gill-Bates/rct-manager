#!/usr/bin/env python3
#
# app/admin/energy_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Admin API for the Energy Manager (design 2.8).

A session-cookie GUI holds no bearer token and therefore cannot call ``/api/v1``, so the same
service is exposed here behind the admin session/CSRF guard, excluded from the OpenAPI schema. This
is also the only surface where the raw gate detail (``CapabilityName`` values, the reject detail) and
the per-device SoC-target derivation policy may appear: the public contract carries business
semantics only.

No business logic lives here. Every decision is taken by ``EnergyManager`` or by the dispatch port;
this module only authenticates, validates the body and projects the result. ``app.admin.api`` is
imported read-only (``require_admin``), exactly as ``app.admin.dispatch_api`` does it.
"""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.admin.api import require_admin
from app.api.routers.energy import EnergyCommandBody, EnergyStatusResponse
from app.dispatch.capabilities import CapabilityStatus
from app.dispatch.controller import CapabilityConflict
from app.dispatch.soc_policy import NOTE_MAX_LENGTH, SocTargetMode, SocTargetPolicy
from app.energy.base import EnergyAdminPort
from app.energy.models import EnergyAction, EnergyCommand
from app.gateway.base import DeviceState


def _require_admin_read(request: Request) -> dict | None:
    return require_admin(request)


def _require_admin_write(request: Request) -> dict | None:
    return require_admin(request, mutation=True)


router = APIRouter(prefix="/admin/api/energy", include_in_schema=False)
log = logging.getLogger(__name__)

# This instance has exactly one admin account; its session carries no username, so "admin" is the
# only possible actor — the same constant app/admin/dispatch_api.py records.
_ADMIN_ACTOR = "admin"


def _device_or_404(request: Request, device_id: str) -> None:
    if device_id not in request.app.state.runtime.devices:
        raise HTTPException(404, "Unknown device")


def _energy_or_503(request: Request) -> EnergyAdminPort:
    """The manager. Present whenever an admin store exists, so it can answer a command with a
    proper refusal instead of a missing attribute — the refusal codes come from the manager.
    """
    runtime = request.app.state.runtime
    if runtime.energy is None:
        raise HTTPException(503, "The Energy Manager is not configured")
    return runtime.energy


def _dispatch_or_503(request: Request):
    runtime = request.app.state.runtime
    if runtime.dispatch is None:
        raise HTTPException(503, "Battery dispatch is not configured")
    return runtime.dispatch


class ArmedBody(BaseModel):
    """Arming exists on the admin surface only: the public API cannot switch a device on."""

    model_config = ConfigDict(extra="forbid")

    armed: StrictBool  # no coercion from "true"/1: switching an inverter on is not a guess


class GateView(BaseModel):
    """One ``GateDecision``, field for field. ``unverified`` carries the raw capability names: this
    is the admin surface, which is exactly where they are allowed to appear.
    """

    action: EnergyAction
    allowed: bool
    engineering_mode: bool  # "active": released THROUGH the per-device switch
    unverified: list[str]
    reject_detail: str | None


class SocTargetPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: SocTargetMode
    below_margin_percent: float = Field(5.0, ge=0, le=50, allow_inf_nan=False)
    note: str | None = Field(None, max_length=NOTE_MAX_LENGTH)


class SocTargetPolicyView(BaseModel):
    mode: SocTargetMode
    below_margin_percent: float
    note: str | None

    @classmethod
    def from_domain(cls, policy: SocTargetPolicy) -> "SocTargetPolicyView":
        return cls(
            mode=policy.mode, below_margin_percent=policy.below_margin_percent, note=policy.note
        )


class LimitsView(BaseModel):
    max_charge_power_w: float
    max_discharge_power_w: float
    engineering_mode: bool


class CapabilityView(BaseModel):
    """One capability row for the Advanced section: status and the evidence the form prefills."""

    name: str
    status: CapabilityStatus
    verified_device_model: str | None
    verified_firmware: str | None
    soc_strategy_external_code: int | None
    enum_byte_width: int | None
    bool_byte_width: int | None
    battery_discharge_positive: bool
    grid_import_positive: bool
    note: str | None


class AdminEnergyDeviceStatus(EnergyStatusResponse):
    """The public status plus the admin-only blocks the GUI needs (design 2.8)."""

    device_name: str
    host: str
    connected: bool  # the inverter answers (state ok or degraded)
    connection_state: str
    gates: list[GateView]
    soc_target_policy: SocTargetPolicyView
    limits: LimitsView | None
    capabilities: list[CapabilityView]
    added_write_names: list[str]  # what arming contributed, display only
    armed_at: datetime | None
    armed_by: str | None


async def _admin_status(request: Request, device_id: str) -> AdminEnergyDeviceStatus:
    energy = _energy_or_503(request)
    dispatch = _dispatch_or_503(request)
    public = EnergyStatusResponse.from_domain(await energy.status(device_id))
    record = energy.armed_record(device_id)
    runtime = request.app.state.runtime
    limits = dispatch.device_limits(device_id)
    device = runtime.devices[device_id]
    state = runtime.gateway.device_status(device_id).state
    return AdminEnergyDeviceStatus(
        **public.model_dump(),
        device_name=device.display_name or runtime.gateway.reported_name(device_id) or device_id,
        host=device.host,
        connected=state in (DeviceState.OK, DeviceState.DEGRADED),
        connection_state=state.value,
        gates=[
            GateView(
                action=action,
                allowed=decision.allowed,
                engineering_mode=decision.engineering_mode,
                unverified=[name.value for name in decision.unverified],
                reject_detail=decision.reject_detail,
            )
            for action, decision in energy.gate_decisions(device_id)
        ],
        soc_target_policy=SocTargetPolicyView.from_domain(dispatch.soc_target_policy(device_id)),
        limits=None
        if limits is None
        else LimitsView(
            max_charge_power_w=limits.max_charge_power_w,
            max_discharge_power_w=limits.max_discharge_power_w,
            engineering_mode=limits.engineering_mode,
        ),
        capabilities=[
            CapabilityView(
                name=record.name.value,
                status=record.status,
                verified_device_model=record.verified_device_model,
                verified_firmware=record.verified_firmware,
                soc_strategy_external_code=record.soc_strategy_external_code,
                enum_byte_width=record.enum_byte_width,
                bool_byte_width=record.bool_byte_width,
                battery_discharge_positive=record.battery_discharge_positive,
                grid_import_positive=record.grid_import_positive,
                note=record.note,
            )
            for record in dispatch.capabilities(device_id)
        ],
        added_write_names=list(record.added_write_names),
        armed_at=record.armed_at,
        armed_by=record.armed_by,
    )


@router.get("/devices")
async def list_devices(
    request: Request, admin: Annotated[dict | None, Depends(_require_admin_read)]
) -> list[AdminEnergyDeviceStatus]:
    """One entry per configured device, readings included, so the GUI needs one poll per cycle."""
    del admin
    return [
        await _admin_status(request, device_id)
        for device_id in sorted(request.app.state.runtime.devices)
    ]


@router.post("/devices/{device_id}/command")
async def post_command(
    request: Request,
    device_id: str,
    body: EnergyCommandBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AdminEnergyDeviceStatus:
    del admin
    _device_or_404(request, device_id)
    runtime = request.app.state.runtime
    runtime.ensure_accepting()
    energy = _energy_or_503(request)
    await energy.command(
        device_id,
        EnergyCommand(body.action, body.target_soc_percent, body.max_power_w),
        actor=_ADMIN_ACTOR,
    )
    return await _admin_status(request, device_id)


@router.put("/devices/{device_id}/armed")
async def put_armed(
    request: Request,
    device_id: str,
    body: ArmedBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AdminEnergyDeviceStatus:
    del admin
    _device_or_404(request, device_id)
    runtime = request.app.state.runtime
    runtime.ensure_accepting()
    energy = _energy_or_503(request)
    await energy.set_armed(device_id, armed=body.armed, actor=_ADMIN_ACTOR)
    return await _admin_status(request, device_id)


@router.get("/devices/{device_id}/soc-target-policy")
def get_soc_target_policy(
    request: Request, device_id: str, admin: Annotated[dict | None, Depends(_require_admin_read)]
) -> SocTargetPolicyView:
    del admin
    _device_or_404(request, device_id)
    return SocTargetPolicyView.from_domain(_dispatch_or_503(request).soc_target_policy(device_id))


@router.put("/devices/{device_id}/soc-target-policy")
async def put_soc_target_policy(
    request: Request,
    device_id: str,
    body: SocTargetPolicyBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> SocTargetPolicyView:
    del admin
    _device_or_404(request, device_id)
    dispatch = _dispatch_or_503(request)
    try:
        policy = SocTargetPolicy(
            device_id=device_id,
            mode=body.mode,
            below_margin_percent=body.below_margin_percent,
            note=body.note,
        )
        await dispatch.set_soc_target_policy(device_id, policy)
    except CapabilityConflict as exc:
        raise HTTPException(
            409,
            {
                "detail": "dispatch_capability_conflict: an operation of this device is active",
                "operation_id": exc.operation_id,
                "mode": exc.mode.value,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log.warning(
        "Energy SoC target policy updated: device=%s mode=%s margin=%s admin=%s",
        device_id, policy.mode.value, policy.below_margin_percent, _ADMIN_ACTOR,
    )  # fmt: skip
    return SocTargetPolicyView.from_domain(policy)
