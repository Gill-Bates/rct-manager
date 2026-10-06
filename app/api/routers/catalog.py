#!/usr/bin/env python3
#
# app/api/routers/catalog.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Catalog endpoints: metric list and configured devices (Requirement 10.2, 10.5)."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.models import DeviceDescriptor, MetricDescriptor
from app.api.problems import ErrorCode, problem_responses
from app.api.runtime import READY_STATES, RuntimeDep
from app.security.dependencies import require_read
from app.security.tokens import Principal

router = APIRouter(prefix="/api/v1", tags=["catalog"])
_AUTH_ERRORS = (ErrorCode.MISSING_TOKEN, ErrorCode.INVALID_TOKEN, ErrorCode.RATE_LIMITED)


@router.get("/metrics", summary="List metrics", responses=problem_responses(*_AUTH_ERRORS))
async def list_metrics(runtime: RuntimeDep, _: Annotated[Principal, Depends(require_read)]) -> list[MetricDescriptor]:
    result = []
    for name in runtime.catalog.names():
        d = runtime.catalog.describe(name)
        result.append(
            MetricDescriptor(
                name=d.name, unit=d.unit, value_type=d.value_type, writable=d.writable, preselected=d.preselected
            )
        )
    return result


@router.get("/devices", summary="List devices", responses=problem_responses(*_AUTH_ERRORS))
async def list_devices(runtime: RuntimeDep, _: Annotated[Principal, Depends(require_read)]) -> list[DeviceDescriptor]:
    return [
        DeviceDescriptor(
            device_id=device_id,
            display_name=entry.display_name or device_id,
            role=runtime.roles[device_id],
            ready=runtime.gateway.device_status(device_id).state in READY_STATES,
        )
        for device_id, entry in runtime.devices.items()
    ]
