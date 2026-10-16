#!/usr/bin/env python3
#
# app/api/routers/plan.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Read-only charging-plan API (design Part 3).

Serves the current :class:`EnergyPlan` for a device: its reasoned slots (action, target SoC, max
power, reason), ``created_at``, ``valid_until``, the active slot for now, and the data age of each
input. Every read goes through :class:`InMemoryPlanScheduler` via the device's
:class:`PlanningService`, so an expired plan is reported as **absent**, never as a valid empty plan.

This router is strictly read-only: it never commands a battery and — like every router — it must
not import ``app.gateway.rct`` (``tests/test_structure_smoke.py::test_routers_do_not_import_the_rct_adapter``).
Automatic dispatch is the separate, default-OFF handoff and is not reachable here.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.problems import ErrorCode, problem_responses
from app.api.runtime import RuntimeDep
from app.optimizer.models import EnergyAction, EnergyPlanSlot, ForecastBasis, ForecastConfidence
from app.optimizer.service import DataAge, PlanningService, PlanView
from app.security.dependencies import require_read
from app.security.tokens import Principal

router = APIRouter(prefix="/api/v1/devices/{device_id}/plan", tags=["charging-plan"])

_COMMON = (
    ErrorCode.MISSING_TOKEN,
    ErrorCode.INVALID_TOKEN,
    ErrorCode.INSUFFICIENT_SCOPE,
    ErrorCode.UNKNOWN_DEVICE,
    ErrorCode.NOT_READY,
)


class PlanSlotResponse(BaseModel):
    """One proposed, non-executing slot."""

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    action: EnergyAction
    target_soc_percent: float | None
    max_power_w: float | None
    reason: str

    @classmethod
    def of(cls, slot: EnergyPlanSlot) -> "PlanSlotResponse":
        return cls(
            start=slot.start,
            end=slot.end,
            action=slot.action,
            target_soc_percent=slot.target_soc_percent,
            max_power_w=slot.max_power_w,
            reason=slot.reason,
        )


class DataAgeResponse(BaseModel):
    """The age in seconds of each input at the last recompute (``None`` when the input was absent)."""

    model_config = ConfigDict(extra="forbid")

    spot_seconds: float | None
    solar_seconds: float | None
    consumption_seconds: float | None
    battery_seconds: float | None

    @classmethod
    def of(cls, age: DataAge) -> "DataAgeResponse":
        return cls(
            spot_seconds=age.spot_seconds,
            solar_seconds=age.solar_seconds,
            consumption_seconds=age.consumption_seconds,
            battery_seconds=age.battery_seconds,
        )


class PlanResponse(BaseModel):
    """The current plan, or ``available=False`` when none is valid (absent or expired)."""

    model_config = ConfigDict(extra="forbid")

    available: bool
    created_at: datetime | None = None
    valid_until: datetime | None = None
    active_slot: PlanSlotResponse | None = None
    slots: tuple[PlanSlotResponse, ...] = ()
    data_age: DataAgeResponse | None = None
    forecast_basis: ForecastBasis | None = Field(
        default=None,
        description=(
            "How the plan's consumption input was obtained. `bootstrap_baseline`: no usable history, "
            "a configured flat baseline load. `history_fallback`: thin history, a median of stored "
            "history. `adaptive_history`: sufficient history, the adaptive profile. Stable values; "
            "absent when the plan carries no such information."
        ),
        examples=["adaptive_history"],
    )
    forecast_confidence: ForecastConfidence | None = Field(
        default=None,
        description=(
            "Confidence in the consumption forecast. `low`: baseline or fallback forecast. "
            "`normal`: sufficient, well-covered history."
        ),
        examples=["normal"],
    )


def _services(request: Request) -> dict[str, PlanningService]:
    return getattr(request.app.state, "planning_services", {}) or {}


@router.get(
    "",
    summary="Current charging plan",
    responses=problem_responses(*_COMMON),
)
async def get_plan(
    device_id: str,
    request: Request,
    runtime: RuntimeDep,
    _: Annotated[Principal, Depends(require_read)],
) -> PlanResponse:
    """The current plan for a device, read-only. An expired or absent plan reports ``available=False``."""
    runtime.device(device_id)  # 404 for an unknown device, like every device route
    service = _services(request).get(device_id)
    view: PlanView | None = service.current_view() if service is not None else None
    if view is None:
        return PlanResponse(available=False)
    plan = view.plan
    active = plan_active_slot(service, runtime)
    return PlanResponse(
        available=True,
        created_at=plan.created_at,
        valid_until=plan.valid_until,
        active_slot=PlanSlotResponse.of(active) if active is not None else None,
        slots=tuple(PlanSlotResponse.of(slot) for slot in plan.slots),
        data_age=DataAgeResponse.of(view.data_age),
        forecast_basis=view.forecast_basis,
        forecast_confidence=view.forecast_confidence,
    )


def plan_active_slot(service: PlanningService, runtime: RuntimeDep) -> EnergyPlanSlot | None:
    """The slot covering ``now`` via the scheduler, so an expired plan yields no active slot."""
    return service.scheduler.active_slot(runtime.clock.now())
