#!/usr/bin/env python3
#
# app/api/routers/health.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""``/health`` (token-free) and ``/api/v1/readiness`` (Requirement 16, 27.9, 27.10)."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.models import ReadinessResponse
from app.api.problems import ErrorCode, ProblemError, problem_responses
from app.api.runtime import READY_STATES, RuntimeDep
from app.errors import DeviceApiError
from app.security.dependencies import require_read
from app.security.tokens import Principal

public = APIRouter(tags=["health"])
business = APIRouter(prefix="/api/v1", tags=["health"])


@public.get("/health", summary="Liveness", responses=problem_responses(ErrorCode.NOT_READY))
async def health(runtime: RuntimeDep) -> dict[str, str]:
    """Answers 200 while the server accepts work and 503 once the shutdown has begun."""
    if runtime.shutting_down():
        raise DeviceApiError("not_ready")
    return {"status": "ok"}


@business.get(
    "/readiness",
    summary="Device readiness",
    responses=problem_responses(
        ErrorCode.MISSING_TOKEN, ErrorCode.INVALID_TOKEN, ErrorCode.RATE_LIMITED, ErrorCode.NOT_READY
    ),
)
async def readiness(runtime: RuntimeDep, _: Annotated[Principal, Depends(require_read)]) -> ReadinessResponse:
    devices = runtime.readiness()
    ready = (
        not runtime.shutting_down()
        and not runtime.graph_failed
        and (runtime.dispatch is None or runtime.dispatch.recovery_ready())
        and all(d.state in READY_STATES for d in devices)
    )
    if not ready:
        raise ProblemError(ErrorCode.NOT_READY, devices=devices)  # devices extension member (Requirement 25.19)
    return ReadinessResponse(ready=True, devices=devices)
