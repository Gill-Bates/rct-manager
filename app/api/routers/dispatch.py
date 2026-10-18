#!/usr/bin/env python3
#
# app/api/routers/dispatch.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Battery dispatch API (REQ-020..REQ-029)."""

import contextlib
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.problems import ErrorCode, ProblemError, problem_responses
from app.api.runtime import Runtime, RuntimeDep
from app.clock import is_aware
from app.dispatch.models import (
    DispatchCommand,
    DispatchMode,
    DispatchPhase,
    DispatchState,
    DispatchStatus,
    PowerDirection,
    StopReason,
)
from app.energy.models import EnergyMode
from app.limits import MAX_POWER_W
from app.security.dependencies import require_write
from app.security.tokens import Principal

router = APIRouter(
    prefix="/api/v1/devices/{device_id}/battery/dispatch", tags=["battery-dispatch"]
)

_COMMON = (
    ErrorCode.MISSING_TOKEN,
    ErrorCode.INVALID_TOKEN,
    ErrorCode.INSUFFICIENT_SCOPE,
    ErrorCode.UNKNOWN_DEVICE,
    ErrorCode.INVALID_REQUEST,
    ErrorCode.VALUE_OUT_OF_RANGE,
    ErrorCode.VALUE_NOT_FINITE,
    ErrorCode.NOT_READY,
    ErrorCode.DEVICE_UNREACHABLE,
    ErrorCode.DEVICE_TIMEOUT,
    ErrorCode.PROTOCOL_ERROR,
    ErrorCode.WRITE_OUTCOME_UNKNOWN,
    ErrorCode.DISPATCH_MODE_UNAVAILABLE,
    ErrorCode.DISPATCH_LIMITS_MISSING,
    ErrorCode.DISPATCH_RESTORE_REQUIRED,
    ErrorCode.DISPATCH_OPERATION_CONFLICT,
    ErrorCode.DISPATCH_STORE_UNAVAILABLE,
    ErrorCode.DISPATCH_UNVERIFIED,
)


class DispatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: DispatchMode
    # Both fields depend on the mode: a hold has no SoC goal and no power budget, every other mode
    # has both. The bounds that do not depend on the mode stay on the fields.
    target_soc_percent: float | None = Field(None, ge=0, le=100)
    max_power_w: float = Field(ge=0, le=MAX_POWER_W, allow_inf_nan=False)
    valid_until: datetime
    expected_operation_id: str | None = Field(None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _mode_consistency(self) -> "DispatchBody":
        if self.mode is DispatchMode.HOLD:
            if self.target_soc_percent is not None:
                raise ValueError("target_soc_percent must be omitted for mode hold")
            if self.max_power_w != 0:
                raise ValueError("max_power_w must be 0 for mode hold")
        else:
            if self.target_soc_percent is None:
                raise ValueError("target_soc_percent is required unless mode is hold")
            if self.max_power_w <= 0:
                raise ValueError("max_power_w must be greater than 0")
        return self

    @field_validator("valid_until")
    @classmethod
    def _aware_until(cls, value: datetime) -> datetime:
        if not is_aware(value):
            raise ValueError("valid_until must include a timezone offset")
        return value

    @field_validator("expected_operation_id")
    @classmethod
    def _printable_id(cls, value: str | None) -> str | None:
        if value is not None and any(
            ord(char) < 32 or ord(char) > 126 for char in value
        ):
            raise ValueError("expected_operation_id must contain printable ASCII")
        return value


class DispatchStatusResponse(BaseModel):
    device_id: str
    operation_id: str | None
    mode: DispatchMode | None
    state: DispatchState
    phase: DispatchPhase
    control_state: Literal["controlled", "restored", "unknown"]
    restore_required: bool
    target_soc_percent: float | None
    max_power_w: float
    max_power_w_requested: float
    max_power_w_clamped: bool
    valid_until: datetime | None
    commanded_direction: PowerDirection
    commanded_power_w: float
    stop_reason: StopReason | None
    fault_code: str | None
    replaced: bool

    @classmethod
    def from_domain(cls, status: DispatchStatus) -> "DispatchStatusResponse":
        return cls(
            **{
                name: getattr(status, name)
                for name in cls.model_fields
                if name != "max_power_w_clamped"
            },
            max_power_w_clamped=status.max_power_w < status.max_power_w_requested,
        )


def _port(runtime: RuntimeDep):
    if runtime.dispatch is None:
        raise ProblemError(ErrorCode.DISPATCH_STORE_UNAVAILABLE)
    return runtime.dispatch


def _energy_ownership_guard(runtime: Runtime, device_id: str):
    """Hold the Energy Manager's per-device mode lock across a low-level dispatch mutation.

    Device-wide ownership mirrors the raw metric-write path in ``writes.py``: when an Energy
    Manager exists, an energy mode other than OFF means a surface (the admin GUI in MANUAL, an app
    in EXTERNAL) owns command authority through ``EnergyManager.command()``. The low-level dispatch
    API bypasses that path, so its mutations must take the same mode lock and refuse while the mode
    is not OFF. The lock is the manager's mode lock; the dispatch controller's own per-device lock
    (entered by ``submit``/``cancel``) is a separate object, so there is no re-entrant acquire.
    """
    return (
        runtime.energy.guard_raw_write(device_id)
        if runtime.energy is not None
        else contextlib.nullcontext()
    )


def _refuse_if_energy_owns_device(runtime: Runtime, device_id: str) -> None:
    """Refuse a low-level dispatch mutation while the Energy Manager owns the device (mode != OFF).

    Must be called INSIDE ``_energy_ownership_guard`` so the mode check and the dispatch operation
    are one critical section; a bare pre-check outside the lock would keep the set_mode race.
    """
    if (
        runtime.energy is not None
        and runtime.energy.mode(device_id) is not EnergyMode.OFF
    ):
        raise ProblemError(ErrorCode.DISPATCH_REGISTER_LOCKED)


@router.post(
    "",
    summary="Create or replace a battery dispatch operation",
    responses=problem_responses(*_COMMON, ErrorCode.DISPATCH_REGISTER_LOCKED),
)
async def post_dispatch(
    device_id: str,
    body: DispatchBody,
    runtime: RuntimeDep,
    principal: Annotated[Principal, Depends(require_write)],
) -> DispatchStatusResponse:
    del principal
    runtime.device(device_id)
    runtime.ensure_accepting()
    async with _energy_ownership_guard(runtime, device_id):
        _refuse_if_energy_owns_device(runtime, device_id)
        status = await _port(runtime).submit(
            device_id, DispatchCommand(**body.model_dump())
        )
    return DispatchStatusResponse.from_domain(status)


@router.get(
    "",
    summary="Read the battery dispatch state",
    responses=problem_responses(*_COMMON),
)
async def get_dispatch(
    device_id: str,
    runtime: RuntimeDep,
    principal: Annotated[Principal, Depends(require_write)],
) -> DispatchStatusResponse:
    del principal
    runtime.device(device_id)
    return DispatchStatusResponse.from_domain(await _port(runtime).status(device_id))


@router.delete(
    "",
    summary="Stop dispatch and restore the previous device state",
    responses=problem_responses(
        *_COMMON, ErrorCode.DISPATCH_NOT_FOUND, ErrorCode.DISPATCH_REGISTER_LOCKED
    ),
)
async def delete_dispatch(
    device_id: str,
    runtime: RuntimeDep,
    principal: Annotated[Principal, Depends(require_write)],
) -> DispatchStatusResponse:
    del principal
    runtime.device(device_id)
    runtime.ensure_accepting()
    async with _energy_ownership_guard(runtime, device_id):
        _refuse_if_energy_owns_device(runtime, device_id)
        status = await _port(runtime).cancel(device_id)
    return DispatchStatusResponse.from_domain(status)
