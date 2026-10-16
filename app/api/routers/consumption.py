#!/usr/bin/env python3
#
# app/api/routers/consumption.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Read-only daily household-consumption API.

Reads the local store only and never starts device I/O; the inverter day history is fetched by the
separate backfill service. Like every router it must not import ``app.gateway.rct``.
"""

import asyncio
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from app.admin.consumption_backfill import WINDOW_DAYS, load_daily_consumption
from app.api.problems import ErrorCode, FieldError, ProblemError, problem_responses
from app.api.runtime import RuntimeDep
from app.security.dependencies import require_read
from app.security.tokens import Principal

router = APIRouter(prefix="/api/v1/devices/{device_id}/consumption", tags=["consumption"])

MAX_RANGE_DAYS = WINDOW_DAYS
_COMMON = (
    ErrorCode.MISSING_TOKEN,
    ErrorCode.INVALID_TOKEN,
    ErrorCode.INSUFFICIENT_SCOPE,
    ErrorCode.UNKNOWN_DEVICE,
    ErrorCode.INVALID_PARAMETER,
    ErrorCode.NOT_READY,
)
_SOURCES = (
    "Sources: `local_measured` (sufficiently complete local 15-minute measurement, profile available), "
    "`inverter_history` (day total from the inverter's daily logger; the distribution over the day is "
    "unknown, so `profile_available` is false), `unavailable` (no value; `energy_wh` is null). "
    "The running local day is always `unavailable`."
)
_DATE = "Local calendar date of the site (ISO 8601, YYYY-MM-DD) in the time zone reported in the response."


class DayResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    energy_wh: float | None
    source: str
    profile_available: bool
    measured_coverage: float


class DaysResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    timezone: str
    days: list[DayResponse]


def _bad(parameter: str, detail: str) -> ProblemError:
    return ProblemError(
        ErrorCode.INVALID_PARAMETER,
        errors=[FieldError(parameter=parameter, code="invalid_parameter", detail=detail)],
    )


async def _resolve(request: Request, runtime, device_id: str, first: date, last: date) -> DaysResponse:
    runtime.device(device_id)  # 404 for an unknown device
    store = getattr(request.app.state, "admin_store", None)
    if store is None:
        raise ProblemError(ErrorCode.NOT_READY)
    zone_name = getattr(getattr(request.app.state, "weather_config", None), "timezone", None) or "UTC"
    days = await asyncio.to_thread(
        load_daily_consumption, store, device_id, first, last, zone_name=zone_name, now=runtime.clock.now()
    )
    return DaysResponse(
        device_id=device_id,
        timezone=zone_name,
        days=[
            DayResponse(
                date=d.date, energy_wh=d.energy_wh, source=d.source.value,
                profile_available=d.profile_available, measured_coverage=round(d.measured_coverage, 4),
            )
            for d in days
        ],
    )


@router.get(
    "/day",
    summary="Daily consumption of one local calendar day",
    description=f"Local store only, no device I/O. {_SOURCES}",
    responses=problem_responses(*_COMMON),
)
async def get_day(
    device_id: str,
    request: Request,
    runtime: RuntimeDep,
    _: Annotated[Principal, Depends(require_read)],
    day: Annotated[date, Query(alias="date", description=_DATE)],
) -> DaysResponse:
    return await _resolve(request, runtime, device_id, day, day)


@router.get(
    "/days",
    summary="Daily consumption of a range of local calendar days",
    description=f"Local store only, no device I/O. At most {MAX_RANGE_DAYS} days per request. {_SOURCES}",
    responses=problem_responses(*_COMMON),
)
async def get_days(
    device_id: str,
    request: Request,
    runtime: RuntimeDep,
    _: Annotated[Principal, Depends(require_read)],
    first: Annotated[date, Query(alias="from", description=f"First day, inclusive. {_DATE}")],
    last: Annotated[date, Query(alias="to", description=f"Last day, inclusive. {_DATE}")],
) -> DaysResponse:
    if last < first:
        raise _bad("to", "to must not be before from.")
    if last - first >= timedelta(days=MAX_RANGE_DAYS):
        raise _bad("to", f"The range must not exceed {MAX_RANGE_DAYS} days.")
    return await _resolve(request, runtime, device_id, first, last)
