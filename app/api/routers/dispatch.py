#!/usr/bin/env python3
#
# app/api/routers/dispatch.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Battery dispatch API (REQ-020..REQ-029)."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.problems import ErrorCode, ProblemError, problem_responses
from app.api.runtime import RuntimeDep
from app.dispatch.models import (
    DispatchCommand,
    DispatchMode,
    DispatchPhase,
    DispatchState,
    DispatchStatus,
    PowerDirection,
    StopReason,
)
from app.security.dependencies import require_write
from app.security.tokens import Principal

router = APIRouter(prefix="/api/v1/devices/{device_id}/battery/dispatch", tags=["battery-dispatch"])

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
    max_power_w: float = Field(ge=0)
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
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("valid_until must include a timezone offset")
        return value

    @field_validator("expected_operation_id")
    @classmethod
    def _printable_id(cls, value: str | None) -> str | None:
        if value is not None and any(ord(char) < 32 or ord(char) > 126 for char in value):
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
            **{name: getattr(status, name) for name in cls.model_fields if name != "max_power_w_clamped"},
            max_power_w_clamped=status.max_power_w < status.max_power_w_requested,
        )


def _port(runtime: RuntimeDep):
    if runtime.dispatch is None:
        raise ProblemError(ErrorCode.DISPATCH_STORE_UNAVAILABLE)
    return runtime.dispatch


@router.post(
    "",
    response_model=DispatchStatusResponse,
    summary="Create or replace a battery dispatch operation",
    responses=problem_responses(*_COMMON),
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
    status = await _port(runtime).submit(device_id, DispatchCommand(**body.model_dump()))
    return DispatchStatusResponse.from_domain(status)


@router.get(
    "",
    response_model=DispatchStatusResponse,
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
    response_model=DispatchStatusResponse,
    summary="Stop dispatch and restore the previous device state",
    responses=problem_responses(*_COMMON, ErrorCode.DISPATCH_NOT_FOUND),
)
async def delete_dispatch(
    device_id: str,
    runtime: RuntimeDep,
    principal: Annotated[Principal, Depends(require_write)],
) -> DispatchStatusResponse:
    del principal
    runtime.device(device_id)
    runtime.ensure_accepting()
    return DispatchStatusResponse.from_domain(await _port(runtime).cancel(device_id))
