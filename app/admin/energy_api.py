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
imported read-only through ``app.admin.dispatch_api``, which owns the shared admin helpers.
"""

import logging
from dataclasses import replace
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.admin.dispatch_api import (
    _ADMIN_ACTOR,
    _conflict,
    _device_or_404,
    _dispatch_or_503,
    _require_admin_read,
    _require_admin_write,
)
from app.api.routers.energy import EnergyCommandBody, EnergyStatusResponse
from app.dispatch.capabilities import CapabilityName, CapabilityRecord, CapabilityStatus
from app.dispatch.controller import CapabilityConflict
from app.dispatch.soc_policy import NOTE_MAX_LENGTH, SocTargetMode, SocTargetPolicy
from app.energy.base import EnergyAdminPort
from app.energy.models import EnergyAction, EnergyCommand
from app.gateway.base import DeviceState

router = APIRouter(prefix="/admin/api/energy", include_in_schema=False)
log = logging.getLogger(__name__)


def _energy_or_503(request: Request) -> EnergyAdminPort:
    """The manager. Present whenever an admin store exists, so it can answer a command with a
    proper refusal instead of a missing attribute — the refusal codes come from the manager.
    """
    runtime = request.app.state.runtime
    if runtime.energy is None:
        raise HTTPException(503, "The Energy Manager is not configured")
    return runtime.energy


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
    write_frame_layout_verified: bool
    apply_sequence_verified: bool
    note: str | None


class HardwareVerificationBody(BaseModel):
    """Everything one hardware verification attests, so it is applied or refused as a whole."""

    model_config = ConfigDict(extra="forbid")

    verified_device_model: str = Field(min_length=1, max_length=128)
    verified_firmware: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=NOTE_MAX_LENGTH)
    soc_strategy_external_code: int = Field(ge=0, le=255)
    enum_byte_width: int = Field(ge=1, le=4)
    bool_byte_width: int = Field(ge=1, le=4)
    write_frame_layout_verified: StrictBool
    apply_sequence_verified: StrictBool
    battery_discharge_positive: StrictBool
    grid_import_positive: StrictBool


_VERIFIED_CAPABILITIES = (
    CapabilityName.WRITE_PATH,
    CapabilityName.BATTERY_POWER_SIGN,
    CapabilityName.GRID_POWER_SIGN,
)


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
    approved_write_names: list[str]  # the live write allowlist, state-independent (Setup checklist)
    armed_at: datetime | None
    armed_by: str | None
    restore_attempts: int = 0  # failed automatic restore attempts since the last clean restore
    next_restore_at: datetime | None = None


async def _admin_status(request: Request, device_id: str) -> AdminEnergyDeviceStatus:
    energy = _energy_or_503(request)
    dispatch = _dispatch_or_503(request)
    public = EnergyStatusResponse.from_domain(await energy.status(device_id))
    record = energy.armed_record(device_id)
    runtime = request.app.state.runtime
    limits = dispatch.device_limits(device_id)
    restore_attempts, next_restore_at = await dispatch.restore_retry_info(device_id)
    device = runtime.devices.get(device_id)
    if device is None:  # removed by a reconfiguration while this request was awaiting
        raise HTTPException(404, "Unknown device")
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
                write_frame_layout_verified=record.write_frame_layout_verified,
                apply_sequence_verified=record.apply_sequence_verified,
                note=record.note,
            )
            for record in dispatch.capabilities(device_id)
        ],
        added_write_names=list(record.added_write_names),
        approved_write_names=list(energy.approved_write_names()),
        armed_at=record.armed_at,
        armed_by=record.armed_by,
        restore_attempts=restore_attempts,
        next_restore_at=next_restore_at,
    )


@router.get("/devices")
async def list_devices(
    request: Request, admin: Annotated[dict | None, Depends(_require_admin_read)]
) -> list[AdminEnergyDeviceStatus]:
    """One entry per configured device, readings included, so the GUI needs one poll per cycle."""
    del admin
    result = []
    for device_id in sorted(request.app.state.runtime.devices):  # sorted() copies: safe across the awaits
        try:
            result.append(await _admin_status(request, device_id))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            # removed by a concurrent reconfiguration: leave it out of the listing
    return result


async def _status_after_action(request: Request, device_id: str) -> AdminEnergyDeviceStatus:
    """The action already happened: a failing status projection must not turn it into a 500.

    The caller could retry a hardware action that did run, so the success is reported anyway.
    """
    try:
        return await _admin_status(request, device_id)
    except Exception:
        log.exception("Status projection failed after an executed energy action (device=%s)", device_id)
        return JSONResponse(  # type: ignore[return-value]
            {"executed": True, "status_available": False, "device_id": device_id}, status_code=202
        )


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
    return await _status_after_action(request, device_id)


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
    return await _status_after_action(request, device_id)


@router.put("/devices/{device_id}/hardware-verification")
async def put_hardware_verification(
    request: Request,
    device_id: str,
    body: HardwareVerificationBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AdminEnergyDeviceStatus:
    """Verify the write path and both sign conventions in one transaction (all or none)."""
    del admin
    _device_or_404(request, device_id)
    dispatch = _dispatch_or_503(request)
    missing = [
        flag for flag in ("write_frame_layout_verified", "apply_sequence_verified") if not getattr(body, flag)
    ]
    if not body.note.strip():
        missing.append("note")
    if missing:
        raise HTTPException(400, f"missing evidence for write_path_convention: {', '.join(missing)}")
    now = request.app.state.runtime.clock.now()
    # A sign record's note is evidence this endpoint has no input field for; carry it over instead
    # of dropping it when the records are rewritten. body.note documents the write path only.
    # Carried only when it cannot be mis-attributed: either it was never stamped under a specific
    # model/firmware (a bare note, nothing to contradict), or it was stamped under the SAME
    # model/firmware this verification attests again. A note recorded under a different hardware
    # identity (e.g. firmware 1.0) must not be re-stamped as if observed under this one (firmware
    # 2.0) — it is dropped instead of silently following the new attestation.
    kept_notes = {
        record.name: record.note
        for record in dispatch.capabilities(device_id)
        if (record.verified_device_model is None and record.verified_firmware is None)
        or (record.verified_device_model, record.verified_firmware)
        == (body.verified_device_model, body.verified_firmware)
    }
    stamp = {
        "status": CapabilityStatus.VERIFIED,
        "verified_device_model": body.verified_device_model,
        "verified_firmware": body.verified_firmware,
        "verified_at": now,
        "verified_by": _ADMIN_ACTOR,
    }
    try:
        records = [
            CapabilityRecord(
                device_id=device_id,
                name=CapabilityName.WRITE_PATH,
                soc_strategy_external_code=body.soc_strategy_external_code,
                enum_byte_width=body.enum_byte_width,
                bool_byte_width=body.bool_byte_width,
                write_frame_layout_verified=body.write_frame_layout_verified,
                apply_sequence_verified=body.apply_sequence_verified,
                note=body.note,
                **stamp,
            ),
            CapabilityRecord(
                device_id=device_id,
                name=CapabilityName.BATTERY_POWER_SIGN,
                battery_discharge_positive=body.battery_discharge_positive,
                note=kept_notes.get(CapabilityName.BATTERY_POWER_SIGN),
                **stamp,
            ),
            CapabilityRecord(
                device_id=device_id,
                name=CapabilityName.GRID_POWER_SIGN,
                grid_import_positive=body.grid_import_positive,
                note=kept_notes.get(CapabilityName.GRID_POWER_SIGN),
                **stamp,
            ),
        ]
        await dispatch.set_capabilities(device_id, records)
    except CapabilityConflict as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log.warning(
        "Energy hardware verification saved: device=%s model=%s firmware=%s admin=%s",
        device_id, body.verified_device_model, body.verified_firmware, _ADMIN_ACTOR,
    )  # fmt: skip
    return await _admin_status(request, device_id)


@router.delete("/devices/{device_id}/hardware-verification")
async def delete_hardware_verification(
    request: Request,
    device_id: str,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AdminEnergyDeviceStatus:
    """Revoke the verification: only the status changes, the recorded evidence stays."""
    del admin
    _device_or_404(request, device_id)
    dispatch = _dispatch_or_503(request)
    current = {record.name: record for record in dispatch.capabilities(device_id)}
    revoked = [
        replace(current[name], status=CapabilityStatus.UNVERIFIED, verified_at=None, verified_by=None)
        for name in _VERIFIED_CAPABILITIES
    ]
    try:
        await dispatch.set_capabilities(device_id, revoked)
    except CapabilityConflict as exc:
        raise _conflict(exc) from exc
    log.warning("Energy hardware verification revoked: device=%s admin=%s", device_id, _ADMIN_ACTOR)
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
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log.warning(
        "Energy SoC target policy updated: device=%s mode=%s margin=%s admin=%s",
        device_id, policy.mode.value, policy.below_margin_percent, _ADMIN_ACTOR,
    )  # fmt: skip
    return SocTargetPolicyView.from_domain(policy)
