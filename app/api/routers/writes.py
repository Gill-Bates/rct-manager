#!/usr/bin/env python3
#
# app/api/routers/writes.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Write and action endpoints; always registered, refused per request while write support is off (Requirement 19).

Allowlist and value range are checked inside the gateway before the access serializer is touched,
so every 403, 404, 409 and 422 leaves the device without a transaction.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.api.models import ActionResult, WriteResult
from app.api.problems import ErrorCode, problem_responses
from app.api.runtime import Runtime, RuntimeDep
from app.dispatch.models import DispatchState
from app.energy.models import EnergyMode
from app.errors import DeviceApiError
from app.protocol.values import ScalarValue
from app.security.dependencies import require_write
from app.security.tokens import Principal

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/devices/{device_id}", tags=["writes"])


async def _dispatch_owns_register(runtime: Runtime, device_id: str, metric_name: str) -> bool:
    """True when the metric is a dispatch-control register and this device is not switched off/idle.

    Fail-closed ownership check for the generic write path: an energy mode other than OFF, or any
    dispatch state that is not a clean IDLE (including a pending restore), means the dispatch layer
    owns these registers and a parallel raw write must be refused. The register set is the one the
    energy allowlist and the admin revoke guard use (Runtime.dispatch_control_registers).
    """
    if metric_name not in runtime.dispatch_control_registers:
        return False
    if runtime.energy is not None and runtime.energy.mode(device_id) is not EnergyMode.OFF:
        return True
    if runtime.dispatch is None:
        return False
    status = await runtime.dispatch.status(device_id)
    return status.state is not DispatchState.IDLE or status.restore_required or status.operation_id is not None

ACTION_NOTE = (
    "The command was sent to the device. The device reports no execution result; check the effect on the device."
)
_COMMON = (
    ErrorCode.MISSING_TOKEN,
    ErrorCode.INVALID_TOKEN,
    ErrorCode.INSUFFICIENT_SCOPE,
    ErrorCode.WRITE_NOT_ALLOWED,
    ErrorCode.UNKNOWN_DEVICE,
    ErrorCode.UNKNOWN_METRIC,
    ErrorCode.INVALID_REQUEST,
    ErrorCode.VALUE_OUT_OF_RANGE,
    ErrorCode.VALUE_TYPE_MISMATCH,
    ErrorCode.VALUE_NOT_FINITE,
    ErrorCode.VALUE_STEP_MISMATCH,
    ErrorCode.RATE_LIMITED,
    ErrorCode.DEVICE_BUDGET_EXHAUSTED,
    ErrorCode.NOT_READY,
    ErrorCode.QUEUE_FULL,
    ErrorCode.QUEUE_TIMEOUT,
    ErrorCode.DEVICE_MAINTENANCE,
    ErrorCode.DEVICE_UNREACHABLE,
    ErrorCode.DEVICE_TIMEOUT,
    ErrorCode.INTERNAL_ERROR,
)


class ValueBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: ScalarValue


@router.put(
    "/metrics/{metric_name}",
    summary="Write one metric",
    responses=problem_responses(
        *_COMMON, ErrorCode.METRIC_IS_ACTION, ErrorCode.DISPATCH_REGISTER_LOCKED, ErrorCode.WRITE_OUTCOME_UNKNOWN
    ),
)
async def put_metric(
    device_id: str,
    metric_name: str,
    body: ValueBody,
    runtime: RuntimeDep,
    principal: Annotated[Principal, Depends(require_write)],
) -> WriteResult:
    runtime.device(device_id)
    runtime.metric(metric_name)
    runtime.ensure_accepting()
    if await _dispatch_owns_register(runtime, device_id, metric_name):
        log.warning(
            "Write refused: dispatch owns register token=%s device=%s metric=%s",
            principal.token_id,
            device_id,
            metric_name,
        )
        raise DeviceApiError("dispatch_register_locked", device_id=device_id, name=metric_name)
    log.info("Write request: token=%s device=%s metric=%s", principal.token_id, device_id, metric_name)
    try:
        outcome = await runtime.gateway.write_metric(device_id, metric_name, body.value)
    except DeviceApiError as exc:
        log.warning(
            "Write failed: token=%s device=%s metric=%s code=%s", principal.token_id, device_id, metric_name, exc.code
        )
        raise
    log.info(
        "Write finished: token=%s device=%s metric=%s confirmed=%s",
        principal.token_id,
        device_id,
        metric_name,
        outcome.confirmed,
    )
    return WriteResult(
        device_id=device_id,
        name=outcome.name,
        written_value=outcome.written_value,
        readback_value=outcome.readback_value,
        confirmed=outcome.confirmed,
        send_unconfirmed=outcome.send_unconfirmed,
        timestamp=outcome.timestamp,
    )


@router.post(
    "/actions/{action_name}",
    summary="Trigger an action",
    responses=problem_responses(*_COMMON, ErrorCode.ACTION_OUTCOME_UNKNOWN),
)
async def post_action(
    device_id: str,
    action_name: str,
    body: ValueBody,
    runtime: RuntimeDep,
    principal: Annotated[Principal, Depends(require_write)],
) -> ActionResult:
    runtime.device(device_id)
    runtime.metric(action_name)
    runtime.ensure_accepting()
    log.warning("Action request: token=%s device=%s action=%s", principal.token_id, device_id, action_name)
    try:
        outcome = await runtime.gateway.trigger_action(device_id, action_name, body.value)
    except DeviceApiError as exc:
        log.warning(
            "Action failed: token=%s device=%s action=%s code=%s", principal.token_id, device_id, action_name, exc.code
        )
        raise
    log.warning("Action sent: token=%s device=%s action=%s", principal.token_id, device_id, action_name)
    return ActionResult(
        device_id=device_id,
        name=outcome.name,
        requested_value=outcome.requested_value,
        readback_value=outcome.readback_value,
        action_note=ACTION_NOTE,
        timestamp=outcome.timestamp,
    )
