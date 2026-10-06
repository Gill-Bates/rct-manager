#!/usr/bin/env python3
#
# app/admin/dispatch_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Admin API for the per-device dispatch capabilities and the engineering-mode switch (4.6).

Not part of the public, documented REST contract: the router is excluded from the OpenAPI schema
and sits behind the same admin session/CSRF guard as the rest of the admin API. ``app.admin.api``
is only imported from here (``require_admin``), never changed.

Entering a capability as ``verified`` is the only way to lift the per-device dispatch gate for the
mode it guards; the server, not the caller, stamps who did it and when.
"""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.admin.api import require_admin
from app.dispatch.capabilities import (
    NOTE_MAX_LENGTH,
    CapabilityName,
    CapabilityRecord,
    CapabilityStatus,
)
from app.dispatch.controller import CapabilityConflict
from app.dispatch.models import DeviceLimits


def _require_admin_read(request: Request) -> dict | None:
    return require_admin(request)


def _require_admin_write(request: Request) -> dict | None:
    return require_admin(request, mutation=True)

router = APIRouter(prefix="/admin/api/dispatch", include_in_schema=False)
log = logging.getLogger(__name__)

# This instance has exactly one admin account (app.admin.store.AdminStore); its session carries no
# username. "admin" is the only possible actor and is recorded as such, not invented per request.
_ADMIN_ACTOR = "admin"

# Required evidence per capability before `status=verified` may be entered (4.1/4.6). WRITE_PATH's
# V-17 (soc_target_unit) is intentionally not required here: 4.1/4.6 make it a condition only
# while SoC-target writing is enabled, which this instance does not expose a write path for yet.
_REQUIRED_FOR_VERIFIED: dict[CapabilityName, tuple[str, ...]] = {
    CapabilityName.WRITE_PATH: (
        "soc_strategy_external_code",
        "enum_byte_width",
        "bool_byte_width",
        "write_frame_layout_verified",
        "apply_sequence_verified",
        # The strategy code is a vendor-specific raw value with no project-wide meaning, so it is
        # only interpretable together with the evidence an operator writes down for it.
        "note",
    ),
    CapabilityName.BATTERY_POWER_SIGN: ("battery_discharge_positive",),
    CapabilityName.GRID_POWER_SIGN: ("grid_import_positive",),
    CapabilityName.EXPORT_LIMIT: ("export_limit_zero_blocks_export",),
    CapabilityName.SETPOINT_VOLATILITY: (),
}
_TRUTHY_FLAGS = frozenset({"write_frame_layout_verified", "apply_sequence_verified"})
# A value that is present but blank is no evidence: `note` carries the human-readable backing for
# the strategy code, so an empty or whitespace-only string counts as missing.
_NON_BLANK_FIELDS = frozenset({"note"})
# `note` is the one required field that is not capability-specific — every row may carry one — so it
# is excluded from the governance map derived below.
_CAPABILITY_AGNOSTIC_FIELDS = frozenset({"note"})
# Single source of truth for "which capability does this evidence field govern", derived from
# _REQUIRED_FOR_VERIFIED so the response projection and the PUT rejection cannot drift apart. The
# adapter (app.gateway.rct_dispatch) reads each of these fields from exactly that one capability;
# the same value stored on any other capability is inert.
_GOVERNED_BY: dict[str, CapabilityName] = {
    field_name: name
    for name, field_names in _REQUIRED_FOR_VERIFIED.items()
    for field_name in field_names
    if field_name not in _CAPABILITY_AGNOSTIC_FIELDS
}


def _missing_evidence(name: CapabilityName, record: CapabilityRecord) -> list[str]:
    missing = []
    for field_name in _REQUIRED_FOR_VERIFIED[name]:
        value = getattr(record, field_name)
        blank = field_name in _NON_BLANK_FIELDS and isinstance(value, str) and not value.strip()
        if value is None or blank or (field_name in _TRUTHY_FLAGS and not value):
            missing.append(field_name)
    return missing


def _governed(record: CapabilityRecord, field_name: str):
    """The field's value if this capability governs it, ``None`` otherwise (inert elsewhere)."""
    if _GOVERNED_BY[field_name] is not record.name:
        return None
    return getattr(record, field_name)


def _misplaced_evidence(name: CapabilityName, body: "CapabilityUpdate") -> list[str]:
    """Evidence fields the caller set explicitly on a capability that does not govern them.

    Storing such a value would echo it back from GET while the adapter ignores it entirely, so the
    PUT is refused instead. Only explicitly sent fields count (``model_fields_set``): an omitted
    field is the caller saying nothing, not setting a value.
    """
    return sorted(
        field_name
        for field_name in body.model_fields_set & _GOVERNED_BY.keys()
        if _GOVERNED_BY[field_name] is not name
    )


def _device_or_404(request: Request, device_id: str) -> None:
    runtime = request.app.state.runtime
    if device_id not in runtime.devices:
        raise HTTPException(404, "Unknown device")


def _dispatch_or_503(request: Request):
    runtime = request.app.state.runtime
    if runtime.dispatch is None:
        raise HTTPException(503, "Battery dispatch is not configured")
    return runtime.dispatch


class CapabilityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CapabilityStatus
    battery_discharge_positive: bool = True
    grid_import_positive: bool = True
    soc_strategy_external_code: int | None = Field(None, ge=0, le=255)
    enum_byte_width: int | None = Field(None, ge=1, le=4)
    bool_byte_width: int | None = Field(None, ge=1, le=4)
    write_frame_layout_verified: bool = False
    apply_sequence_verified: bool = False
    sequence_order_relevant: bool | None = None
    export_limit_zero_blocks_export: bool | None = None
    volatile: bool | None = None
    refresh_interval_seconds: float | None = Field(None, ge=0)
    verified_device_model: str | None = Field(None, min_length=1, max_length=128)
    verified_firmware: str | None = Field(None, min_length=1, max_length=64)
    note: str | None = Field(None, max_length=NOTE_MAX_LENGTH)


class CapabilityResponse(BaseModel):
    """GET/PUT shape of one device capability.

    Evidence fields are **capability-specific**: each row carries only the evidence the adapter
    actually reads from that capability and ``null`` everywhere else. ``null`` therefore means "this
    field does not govern this capability", not "unset" — it never means the adapter works without a
    value. Where a field is populated it reflects the currently configured value regardless of
    verification status, so always read it together with ``status``: when ``status`` is
    ``unverified``, it is the shipped assumption default the adapter works with, not a measured fact
    about this device's hardware.
    """

    device_id: str
    name: CapabilityName
    status: CapabilityStatus
    battery_discharge_positive: bool | None = Field(
        default=None,
        description="BATTERY_POWER_SIGN evidence, governed by `battery_power_sign_convention` only:"
        " whether a positive power_mng_battery_power_extern write discharges the battery on this"
        " device. `null` on every other capability, which this field does not govern.",
    )
    grid_import_positive: bool | None = Field(
        default=None,
        description="GRID_POWER_SIGN evidence, governed by `grid_power_sign_convention` only:"
        " whether a positive grid_power reading means import on this device. `null` on every other"
        " capability, which this field does not govern.",
    )
    soc_strategy_external_code: int | None = Field(
        default=None,
        description="WRITE_PATH evidence, governed by `write_path_convention` only: the raw"
        " power_mng_soc_strategy register value this device uses for external control. This is a"
        " vendor-specific code with no project-wide meaning (the RCT register catalog carries no"
        " enum label table for it), so it is only interpretable together with `note`: setting"
        " `write_path_convention` to `verified` is refused unless a non-empty `note` is supplied."
        " `null` on every other capability, which this field does not govern.",
    )
    verified_device_model: str | None
    verified_firmware: str | None
    verified_at: datetime | None
    verified_by: str | None
    note: str | None

    @classmethod
    def from_domain(cls, record: CapabilityRecord) -> "CapabilityResponse":
        return cls(
            device_id=record.device_id,
            name=record.name,
            status=record.status,
            battery_discharge_positive=_governed(record, "battery_discharge_positive"),
            grid_import_positive=_governed(record, "grid_import_positive"),
            soc_strategy_external_code=_governed(record, "soc_strategy_external_code"),
            verified_device_model=record.verified_device_model,
            verified_firmware=record.verified_firmware,
            verified_at=record.verified_at,
            verified_by=record.verified_by,
            note=record.note,
        )


class CopyFrom(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_device_id: str = Field(min_length=1)
    # The caller states what the target device is, so the comparison does not depend on the target
    # already carrying a verified record (design 4.1: "mit den Angaben des Zielgeräts").
    target_device_model: str = Field(min_length=1, max_length=128)
    target_firmware: str = Field(min_length=1, max_length=64)


class DeviceLimitsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_charge_power_w: float = Field(gt=0)
    max_discharge_power_w: float = Field(gt=0)
    engineering_mode: bool = False


class DeviceLimitsResponse(BaseModel):
    device_id: str
    max_charge_power_w: float
    max_discharge_power_w: float
    engineering_mode: bool


@router.get("/devices")
def list_devices(request: Request, admin: Annotated[dict | None, Depends(_require_admin_read)]) -> list[str]:
    del admin
    return sorted(request.app.state.runtime.devices)


@router.get("/devices/{device_id}/capabilities")
def get_capabilities(
    request: Request, device_id: str, admin: Annotated[dict | None, Depends(_require_admin_read)]
) -> list[CapabilityResponse]:
    del admin
    _device_or_404(request, device_id)
    dispatch = _dispatch_or_503(request)
    return [CapabilityResponse.from_domain(record) for record in dispatch.capabilities(device_id)]


@router.put("/devices/{device_id}/capabilities/{name}")
async def put_capability(
    request: Request,
    device_id: str,
    name: CapabilityName,
    body: CapabilityUpdate,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> CapabilityResponse:
    del admin
    _device_or_404(request, device_id)
    dispatch = _dispatch_or_503(request)
    verified_by = _ADMIN_ACTOR

    misplaced = _misplaced_evidence(name, body)
    if misplaced:
        raise HTTPException(422, f"evidence not governed by {name.value}: {', '.join(misplaced)}")

    # This PUT replaces the record; it is not a partial update. An omitted field falls through to
    # the CapabilityRecord default, so a previously stored non-default value is RESET, not kept: a
    # caller that wants to preserve an evidence value has to send it again. exclude_unset is only
    # here so model_fields_set can tell "omitted" from "sent" for the misplaced-evidence check
    # above; it does not turn the replace into a merge.
    evidence = body.model_dump(exclude={"status"}, exclude_unset=True)

    if body.status is CapabilityStatus.VERIFIED:
        if not body.verified_device_model or not body.verified_firmware:
            raise HTTPException(400, "verified_device_model and verified_firmware are required")
        candidate = CapabilityRecord(device_id=device_id, name=name, **evidence)
        missing = _missing_evidence(name, candidate)
        if missing:
            raise HTTPException(400, f"missing evidence for {name.value}: {', '.join(missing)}")

    now = request.app.state.runtime.clock.now()
    record = CapabilityRecord(
        device_id=device_id,
        name=name,
        verified_at=now if body.status is CapabilityStatus.VERIFIED else None,
        verified_by=verified_by if body.status is CapabilityStatus.VERIFIED else None,
        **evidence,
        status=body.status,
    )
    # The active-operation guard (D2) lives in DispatchController.set_capability(), under the same
    # per-device lock as submit()/tick() — closes the former TOCTOU window (B1) and applies
    # unconditionally, regardless of the new status (closes the VERIFIED->VERIFIED bypass, B2).
    try:
        await dispatch.set_capability(device_id, record)
    except CapabilityConflict as exc:
        raise HTTPException(
            409,
            {
                "detail": "dispatch_capability_conflict: an operation of this device is active",
                "operation_id": exc.operation_id,
                "mode": exc.mode.value,
            },
        ) from exc
    log.warning(
        "Dispatch capability updated: device=%s name=%s status=%s model=%s firmware=%s admin=%s",
        device_id, name.value, body.status.value, body.verified_device_model, body.verified_firmware, verified_by,
    )  # fmt: skip
    return CapabilityResponse.from_domain(record)


@router.post("/devices/{device_id}/capabilities:copy-from")
async def copy_from(
    request: Request,
    device_id: str,
    body: CopyFrom,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> list[CapabilityResponse]:
    del admin
    _device_or_404(request, device_id)
    _device_or_404(request, body.source_device_id)
    dispatch = _dispatch_or_503(request)
    actor = _ADMIN_ACTOR

    source_records = {record.name: record for record in dispatch.capabilities(body.source_device_id)}
    copied: list[CapabilityResponse] = []
    for name, source in source_records.items():
        if source.status is not CapabilityStatus.VERIFIED:
            continue
        if source.verified_device_model != body.target_device_model or source.verified_firmware != body.target_firmware:
            log.warning(
                "Dispatch capability copy refused: device=%s name=%s source=%s (model/firmware mismatch)",
                device_id, name.value, body.source_device_id,
            )  # fmt: skip
            continue
        new_record = CapabilityRecord.from_dict(
            {**source.to_dict(), "device_id": device_id, "verified_by": f"copy:{body.source_device_id}:{actor}"}
        )
        # Abort-and-report (D3/B2 extension): a 409 on one capability stops the whole copy instead
        # of returning a partial 200 with some capabilities silently skipped — a caller must be
        # able to tell "copied everything" from "copied some, blocked on one" from the status
        # code alone, not by inspecting the response body closely.
        try:
            await dispatch.set_capability(device_id, new_record)
        except CapabilityConflict as exc:
            raise HTTPException(
                409,
                {
                    "detail": f"dispatch_capability_conflict: an operation of {device_id} is active",
                    "operation_id": exc.operation_id,
                    "mode": exc.mode.value,
                },
            ) from exc
        copied.append(CapabilityResponse.from_domain(new_record))
        log.warning(
            "Dispatch capability copied: device=%s name=%s source=%s model=%s firmware=%s admin=%s",
            device_id, name.value, body.source_device_id, source.verified_device_model,
            source.verified_firmware, actor,
        )  # fmt: skip
    return copied


@router.put("/devices/{device_id}")
async def put_device_limits(
    request: Request,
    device_id: str,
    body: DeviceLimitsUpdate,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> DeviceLimitsResponse:
    del admin
    _device_or_404(request, device_id)
    dispatch = _dispatch_or_503(request)
    limits = DeviceLimits(
        max_charge_power_w=body.max_charge_power_w,
        max_discharge_power_w=body.max_discharge_power_w,
        engineering_mode=body.engineering_mode,
    )
    try:
        await dispatch.set_device_limits(device_id, limits)
    except CapabilityConflict as exc:
        raise HTTPException(
            409,
            {
                "detail": "dispatch_capability_conflict: an operation of this device is active",
                "operation_id": exc.operation_id,
                "mode": exc.mode.value,
            },
        ) from exc
    log.warning(
        "Dispatch device limits updated: device=%s charge_w=%s discharge_w=%s engineering_mode=%s",
        device_id, limits.max_charge_power_w, limits.max_discharge_power_w, limits.engineering_mode,
    )  # fmt: skip
    return DeviceLimitsResponse(device_id=device_id, **body.model_dump())
