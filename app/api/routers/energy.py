#!/usr/bin/env python3
#
# app/api/routers/energy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Energy Manager API (design 2.8).

Business semantics only: no register name, no raw strategy code, no byte width and no sign
convention appears in a request or a response model here — that is what makes the public contract
independent of the inverter behind it. The raw gate detail and the SoC-target derivation policy live
on the session-authenticated admin surface instead.

Registered under the same ``enable_write_support`` gate and the same ``require_write`` dependency as
the dispatch router, so a read-only token cannot command a battery and a deployment without write
support keeps answering ``404 write_disabled``.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.api.problems import ErrorCode, ProblemError, problem_responses
from app.api.runtime import RuntimeDep
from app.dispatch.models import PowerDirection
from app.energy.models import (
    TARGET_SOC_MAX_PERCENT,
    TARGET_SOC_MIN_PERCENT,
    ActionReason,
    EnergyAction,
    EnergyCommand,
    EnergyDeviceStatus,
    EnergyState,
)
from app.energy.readings import DeviceReading, EnergyReadings
from app.security.dependencies import require_write
from app.security.tokens import Principal

router = APIRouter(prefix="/api/v1/devices/{device_id}/energy", tags=["energy-manager"])

MAX_POWER_W = 50_000.0

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
    ErrorCode.DISPATCH_LIMITS_MISSING,
    ErrorCode.DISPATCH_RESTORE_REQUIRED,
    ErrorCode.DISPATCH_SNAPSHOT_STALE,
    ErrorCode.DISPATCH_STORE_UNAVAILABLE,
    ErrorCode.DISPATCH_UNVERIFIED,
    ErrorCode.ENERGY_MANAGER_DISARMED,
    ErrorCode.ENERGY_WRITE_SUPPORT_REQUIRED,
    ErrorCode.ENERGY_ACTION_UNAVAILABLE,
)


class EnergyCommandBody(BaseModel):
    """One business action. ``target_soc_percent`` is the Energy Manager's stop goal, never a
    device-level register value — how a device derives that is an operator policy on the admin
    surface.
    """

    model_config = ConfigDict(extra="forbid")

    action: EnergyAction
    target_soc_percent: float | None = Field(
        None, ge=TARGET_SOC_MIN_PERCENT, le=TARGET_SOC_MAX_PERCENT, allow_inf_nan=False
    )
    # Absent means "use the limit configured for this inverter"; an explicit value is clamped down
    # to that limit, which the response reports as power_limit_clamped.
    max_power_w: float | None = Field(None, gt=0, le=MAX_POWER_W, allow_inf_nan=False)

    @model_validator(mode="after")
    def _action_consistency(self) -> "EnergyCommandBody":
        if self.action in (EnergyAction.CHARGE, EnergyAction.DISCHARGE):
            if self.target_soc_percent is None:
                raise ValueError("target_soc_percent is required for charge and discharge")
        else:
            if self.target_soc_percent is not None:
                raise ValueError(f"target_soc_percent must be omitted for action {self.action.value}")
            if self.max_power_w is not None:
                raise ValueError(f"max_power_w must be omitted for action {self.action.value}")
        return self


class ArmedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    armed: StrictBool  # no coercion from "true"/1: switching an inverter on is not a guess


class ReadingResponse(BaseModel):
    value: float | None
    age_seconds: float | None
    stale: bool

    @classmethod
    def from_domain(cls, reading: DeviceReading) -> "ReadingResponse":
        return cls(value=reading.value, age_seconds=reading.age_seconds, stale=reading.stale)


class ReadingsResponse(BaseModel):
    battery_soc_percent: ReadingResponse
    grid_power_w: ReadingResponse  # positive = import
    pv_power_w: ReadingResponse
    house_load_w: ReadingResponse

    @classmethod
    def from_domain(cls, readings: EnergyReadings) -> "ReadingsResponse":
        return cls(
            **{
                name: ReadingResponse.from_domain(getattr(readings, name))
                for name in cls.model_fields
            }
        )


class TargetSocWindowResponse(BaseModel):
    min: float
    max: float


class ActionAvailabilityResponse(BaseModel):
    action: EnergyAction
    available: bool
    reason: ActionReason | None


class EnergyStatusResponse(BaseModel):
    device_id: str
    armed: bool
    state: EnergyState
    action: EnergyAction | None
    target_soc_percent: float | None
    power_limit_w: float | None  # null while idle and while holding: no power budget applies
    power_limit_clamped: bool
    commanded_power_w: float
    commanded_direction: PowerDirection
    until: datetime | None
    stop_reason: str | None
    time_limited: bool
    target_soc_window: TargetSocWindowResponse
    readings: ReadingsResponse
    actions: list[ActionAvailabilityResponse]

    @classmethod
    def from_domain(cls, status: EnergyDeviceStatus) -> "EnergyStatusResponse":
        return cls(
            device_id=status.device_id,
            armed=status.armed,
            state=status.state,
            action=status.action,
            target_soc_percent=status.target_soc_percent,
            power_limit_w=status.power_limit_w,
            power_limit_clamped=status.power_limit_clamped,
            commanded_power_w=status.commanded_power_w,
            commanded_direction=status.commanded_direction,
            until=status.until,
            stop_reason=status.stop_reason,
            time_limited=status.time_limited,
            target_soc_window=TargetSocWindowResponse(
                min=status.target_soc_window.min, max=status.target_soc_window.max
            ),
            readings=ReadingsResponse.from_domain(status.readings),
            actions=[
                ActionAvailabilityResponse(
                    action=item.action, available=item.available, reason=item.reason
                )
                for item in status.actions
            ],
        )


def _manager(runtime: RuntimeDep):
    if runtime.energy is None:
        raise ProblemError(ErrorCode.DISPATCH_STORE_UNAVAILABLE)
    return runtime.energy


@router.get(
    "",
    response_model=EnergyStatusResponse,
    summary="Read the Energy Manager state of one inverter",
    responses=problem_responses(*_COMMON),
)
async def get_energy(
    device_id: str,
    runtime: RuntimeDep,
    principal: Annotated[Principal, Depends(require_write)],
) -> EnergyStatusResponse:
    del principal
    runtime.device(device_id)
    # Exempt from ensure_accepting() on purpose, like GET .../battery/dispatch: reading a state
    # while the service drains is harmless and useful.
    return EnergyStatusResponse.from_domain(await _manager(runtime).status(device_id))


@router.post(
    "/command",
    response_model=EnergyStatusResponse,
    summary="Charge, discharge, hold, or return to automatic operation",
    responses=problem_responses(*_COMMON),
)
async def post_energy_command(
    device_id: str,
    body: EnergyCommandBody,
    runtime: RuntimeDep,
    principal: Annotated[Principal, Depends(require_write)],
) -> EnergyStatusResponse:
    runtime.device(device_id)
    runtime.ensure_accepting()
    status = await _manager(runtime).command(
        device_id,
        EnergyCommand(body.action, body.target_soc_percent, body.max_power_w),
        actor=principal.token_id,
    )
    return EnergyStatusResponse.from_domain(status)


@router.put(
    "/armed",
    response_model=EnergyStatusResponse,
    summary="Switch the Energy Manager on or off for one inverter",
    responses=problem_responses(*_COMMON),
)
async def put_energy_armed(
    device_id: str,
    body: ArmedBody,
    runtime: RuntimeDep,
    principal: Annotated[Principal, Depends(require_write)],
) -> EnergyStatusResponse:
    runtime.device(device_id)
    runtime.ensure_accepting()
    status = await _manager(runtime).set_armed(device_id, armed=body.armed, actor=principal.token_id)
    return EnergyStatusResponse.from_domain(status)
