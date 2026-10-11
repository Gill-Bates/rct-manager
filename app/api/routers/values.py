#!/usr/bin/env python3
#
# app/api/routers/values.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Read endpoints for metric values (Requirement 10.6 to 10.22).

Every check runs before the first transaction; batches read sequentially through the endpoint queue.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.models import MetricCollection, MetricError, MetricValue
from app.api.problems import (
    ErrorCode,
    FieldError,
    ProblemError,
    field_text,
    normalize_code,
    problem_responses,
)
from app.api.runtime import Runtime, RuntimeDep, echo_name
from app.catalog.base import NeutralValueType
from app.errors import DeviceApiError
from app.gateway.base import MetricReading
from app.security.dependencies import require_read
from app.security.tokens import Principal

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/devices/{device_id}/metrics", tags=["values"])

_FRESH_HELP = (
    "Request a device read instead of the normal cache lookup. "
    "The value is fresh as of the transaction that delivered it "
    "(time-bounded freshness); it carries no guarantee beyond that moment. Each fresh read counts against the "
    "device work budget. Requires an explicit names list on the collection endpoint. "
    "If the read fails with a recoverable device error, a retained cache value may be returned instead: "
    "source=cache, freshness=cached, and stale_reason explains the error. Check these fields and age_seconds "
    "before treating the value as newly observed. Periodically registered metrics may reject fresh reads "
    "with fresh_not_available_for_periodic_metric when the configured mode is reject."
)
Fresh = Annotated[bool, Query(description=_FRESH_HELP)]
_COMMON = (ErrorCode.MISSING_TOKEN, ErrorCode.INVALID_TOKEN, ErrorCode.RATE_LIMITED, ErrorCode.UNKNOWN_DEVICE)
_DEVICE_ERRORS = (
    ErrorCode.NOT_READY,
    ErrorCode.QUEUE_FULL,
    ErrorCode.QUEUE_TIMEOUT,
    ErrorCode.DEVICE_MAINTENANCE,
    ErrorCode.DEVICE_UNREACHABLE,
    ErrorCode.DEVICE_TIMEOUT,
    ErrorCode.PROTOCOL_ERROR,
    ErrorCode.DEVICE_BUDGET_EXHAUSTED,
    ErrorCode.FRESH_NOT_AVAILABLE,
    ErrorCode.INVALID_PARAMETER,
    ErrorCode.INTERNAL_ERROR,
)


def _to_value(runtime: Runtime, reading: MetricReading) -> MetricValue:
    is_enum = runtime.catalog.describe(reading.name).value_type is NeutralValueType.ENUM
    return MetricValue(
        name=reading.name,
        value=reading.value,
        unit=reading.unit,
        timestamp=reading.measured_at,
        age_seconds=reading.age_seconds,
        stale=reading.stale,
        source=reading.source,
        stale_reason=reading.stale_reason,
        freshness=reading.freshness,
        enum_value=int(reading.value) if is_enum and isinstance(reading.value, int) else None,
        enum_label=reading.enum_label,
    )


def _select_names(runtime: Runtime, names: str | None, fresh: bool) -> list[str]:
    """Resolve and validate the requested names; raises before any device work."""
    default_set = names is None
    if names is None and fresh:
        # The default set can exceed the fresh limit; never truncate it silently.
        limit = runtime.settings.max_fresh_metrics_per_request
        raise ProblemError(
            ErrorCode.INVALID_REQUEST,
            detail=f"fresh=true requires an explicit names list of at most {limit} metrics.",
            errors=[FieldError(parameter="names", code="required_with_fresh", detail="names is required.")],
        )
    if names is None:
        selected = list(runtime.catalog.preselected())
    else:
        selected = list(dict.fromkeys(item.strip() for item in names.split(",")))
        if not all(selected):
            raise ProblemError(
                ErrorCode.INVALID_PARAMETER,
                errors=[FieldError(parameter="names", code="invalid_parameter", detail="Empty metric name in list.")],
            )
    settings = runtime.settings
    # The registry-defined default set is bounded at startup (64) and not an operator batch.
    if not default_set and len(selected) > settings.max_metrics_per_request:
        raise ProblemError(
            ErrorCode.BATCH_TOO_LARGE,
            detail=f"At most {settings.max_metrics_per_request} metrics per request are allowed.",
            errors=[FieldError(parameter="names", code="batch_too_large", detail="Too many metric names.")],
        )
    if fresh and len(selected) > settings.max_fresh_metrics_per_request:
        raise ProblemError(
            ErrorCode.FRESH_BATCH_TOO_LARGE,
            detail=f"At most {settings.max_fresh_metrics_per_request} metrics per fresh request are allowed.",
            errors=[FieldError(parameter="names", code="fresh_batch_too_large", detail="Too many metric names.")],
        )
    unknown = [n for n in selected if not runtime.catalog.exists(n)]
    if unknown:
        raise ProblemError(
            ErrorCode.UNKNOWN_METRIC,
            status=422,
            errors=[
                FieldError(parameter="names", code="unknown_metric", detail=f"Unknown metric: {echo_name(n)}")
                for n in unknown
            ],
        )
    return selected


@router.get(
    "/{metric_name}",
    summary="Read one metric",
    responses=problem_responses(*_COMMON, ErrorCode.UNKNOWN_METRIC, *_DEVICE_ERRORS),
)
async def get_metric(
    device_id: str,
    metric_name: str,
    runtime: RuntimeDep,
    _: Annotated[Principal, Depends(require_read)],
    fresh: Fresh = False,
) -> MetricValue:
    runtime.device(device_id)
    runtime.metric(metric_name)
    runtime.ensure_accepting()
    reading = await runtime.gateway.read_metric(device_id, metric_name, fresh=fresh)
    return _to_value(runtime, reading)


@router.get(
    "",
    summary="Read several metrics",
    responses=problem_responses(
        *_COMMON,
        ErrorCode.UNKNOWN_METRIC,
        ErrorCode.BATCH_TOO_LARGE,
        ErrorCode.FRESH_BATCH_TOO_LARGE,
        ErrorCode.DEVICE_UNAVAILABLE,
        *_DEVICE_ERRORS,
    ),
)
async def get_metrics(
    device_id: str,
    runtime: RuntimeDep,
    _: Annotated[Principal, Depends(require_read)],
    names: Annotated[
        str | None, Query(description="Comma-separated metric names; the preselected metrics when omitted.")
    ] = None,
    fresh: Fresh = False,
) -> MetricCollection:
    """Partial success answers 200 with ``errors``; only a batch without any value fails with 502."""
    runtime.device(device_id)
    selected = _select_names(runtime, names, fresh)
    runtime.ensure_accepting()
    # This request's own reservation; never shared with any other concurrent batch (Requirement 6.10).
    reservation = runtime.gateway.reserve_budget(device_id, len(selected)) if fresh else None
    metrics: list[MetricValue] = []
    errors: list[MetricError] = []
    try:
        for name in selected:
            try:
                reading = await runtime.gateway.read_metric(device_id, name, fresh=fresh, charge=reservation)
            except DeviceApiError as exc:
                code = normalize_code(exc)
                if code is ErrorCode.INTERNAL_ERROR:
                    log.error("Unexpected error code while reading a metric: %s", exc.code)
                errors.append(MetricError(name=name, code=code.value, detail=field_text(code.value)))
                continue
            metrics.append(_to_value(runtime, reading))
    finally:
        # Cancellation, shutdown or an unexpected error must not leave reservations behind.
        if fresh:
            runtime.gateway.refund_budget(device_id, reservation)
    if selected and not metrics:
        raise ProblemError(ErrorCode.DEVICE_UNAVAILABLE, errors=errors)
    return MetricCollection(device_id=device_id, metrics=metrics, errors=errors)
