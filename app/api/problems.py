#!/usr/bin/env python3
#
# app/api/problems.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Problem_Details per RFC 9457 and the mapping from internal errors to error keys (Requirement 25)."""

import logging
import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.models import DeviceReadiness, MetricError
from app.errors import DeviceApiError, DeviceTimeout, DeviceUnreachable, ProtocolError
from app.protocol.values import ScalarValue

log = logging.getLogger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"
INTERNAL_ERROR_DETAIL = "An internal error occurred. Quote the correlation id when reporting it."


class ErrorCode(StrEnum):
    MISSING_TOKEN = "missing_token"
    INVALID_TOKEN = "invalid_token"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    WRITE_NOT_ALLOWED = "write_not_allowed"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    INVALID_REQUEST = "invalid_request"
    UNKNOWN_DEVICE = "unknown_device"
    UNKNOWN_METRIC = "unknown_metric"
    WRITE_DISABLED = "write_disabled"
    DOCS_NOT_AVAILABLE = "docs_not_available"
    METRIC_IS_ACTION = "metric_is_action"
    FRESH_NOT_AVAILABLE = "fresh_not_available_for_periodic_metric"
    BATCH_TOO_LARGE = "batch_too_large"
    FRESH_BATCH_TOO_LARGE = "fresh_batch_too_large"
    VALUE_OUT_OF_RANGE = "value_out_of_range"
    VALUE_TYPE_MISMATCH = "value_type_mismatch"
    VALUE_NOT_FINITE = "value_not_finite"
    VALUE_STEP_MISMATCH = "value_step_mismatch"
    INVALID_PARAMETER = "invalid_parameter"
    RATE_LIMITED = "rate_limited"
    DEVICE_BUDGET_EXHAUSTED = "device_budget_exhausted"
    INTERNAL_ERROR = "internal_error"
    DEVICE_UNREACHABLE = "device_unreachable"
    DEVICE_TIMEOUT = "device_timeout"
    PROTOCOL_ERROR = "protocol_error"
    DEVICE_UNAVAILABLE = "device_unavailable"
    WRITE_OUTCOME_UNKNOWN = "write_outcome_unknown"
    ACTION_OUTCOME_UNKNOWN = "action_outcome_unknown"
    QUEUE_FULL = "queue_full"
    DEVICE_MAINTENANCE = "device_maintenance"
    NOT_READY = "not_ready"
    QUEUE_TIMEOUT = "queue_timeout"
    DISPATCH_MODE_UNAVAILABLE = "dispatch_mode_unavailable"
    DISPATCH_LIMITS_MISSING = "dispatch_limits_missing"
    DISPATCH_RESTORE_REQUIRED = "dispatch_restore_required"
    DISPATCH_OPERATION_CONFLICT = "dispatch_operation_conflict"
    DISPATCH_NOT_FOUND = "dispatch_not_found"
    DISPATCH_STORE_UNAVAILABLE = "dispatch_store_unavailable"
    DISPATCH_UNVERIFIED = "dispatch_unverified"
    DISPATCH_CAPABILITY_CONFLICT = "dispatch_capability_conflict"
    DISPATCH_RECORD_CORRUPT = "dispatch_record_corrupt"
    DISPATCH_SNAPSHOT_STALE = "dispatch_snapshot_stale"
    ENERGY_MANAGER_DISARMED = "energy_manager_disarmed"
    ENERGY_WRITE_SUPPORT_REQUIRED = "energy_write_support_required"
    ENERGY_ACTION_UNAVAILABLE = "energy_action_unavailable"
    # Field-level keys: they appear only inside ``errors``, never as a top-level code.
    INVALID_FLOAT = "invalid_float"
    DECODE_LENGTH_MISMATCH = "decode_length_mismatch"


E = ErrorCode
STATUS: dict[ErrorCode, int] = {
    E.MISSING_TOKEN: 401,
    E.INVALID_TOKEN: 401,
    E.INSUFFICIENT_SCOPE: 403,
    E.WRITE_NOT_ALLOWED: 403,
    E.NOT_FOUND: 404,
    E.METHOD_NOT_ALLOWED: 405,
    E.INVALID_REQUEST: 422,
    E.UNKNOWN_DEVICE: 404,
    E.UNKNOWN_METRIC: 404,  # 422 when the name came from the ``names`` query parameter
    E.WRITE_DISABLED: 404,
    E.DOCS_NOT_AVAILABLE: 404,
    E.METRIC_IS_ACTION: 409,
    E.FRESH_NOT_AVAILABLE: 409,
    E.BATCH_TOO_LARGE: 422,
    E.FRESH_BATCH_TOO_LARGE: 422,
    E.VALUE_OUT_OF_RANGE: 422,
    E.VALUE_TYPE_MISMATCH: 422,
    E.VALUE_NOT_FINITE: 422,
    E.VALUE_STEP_MISMATCH: 422,
    E.INVALID_PARAMETER: 422,
    E.RATE_LIMITED: 429,
    E.DEVICE_BUDGET_EXHAUSTED: 429,
    E.INTERNAL_ERROR: 500,
    E.DEVICE_UNREACHABLE: 502,
    E.DEVICE_TIMEOUT: 502,
    E.PROTOCOL_ERROR: 502,
    E.DEVICE_UNAVAILABLE: 502,
    E.WRITE_OUTCOME_UNKNOWN: 502,
    E.ACTION_OUTCOME_UNKNOWN: 502,
    E.QUEUE_FULL: 503,
    E.DEVICE_MAINTENANCE: 503,
    E.NOT_READY: 503,
    E.QUEUE_TIMEOUT: 504,
    E.DISPATCH_MODE_UNAVAILABLE: 409,
    E.DISPATCH_LIMITS_MISSING: 409,
    E.DISPATCH_RESTORE_REQUIRED: 409,
    E.DISPATCH_OPERATION_CONFLICT: 409,
    E.DISPATCH_NOT_FOUND: 404,
    E.DISPATCH_STORE_UNAVAILABLE: 503,
    E.DISPATCH_UNVERIFIED: 409,
    E.DISPATCH_CAPABILITY_CONFLICT: 409,
    E.DISPATCH_RECORD_CORRUPT: 503,
    E.DISPATCH_SNAPSHOT_STALE: 503,
    E.ENERGY_MANAGER_DISARMED: 409,
    E.ENERGY_WRITE_SUPPORT_REQUIRED: 409,
    E.ENERGY_ACTION_UNAVAILABLE: 409,
}

_TEXT: dict[ErrorCode, tuple[str, str]] = {
    E.MISSING_TOKEN: ("Authentication required", "The Authorization header is missing."),
    E.INVALID_TOKEN: ("Invalid token", "The bearer token is not valid."),
    E.INSUFFICIENT_SCOPE: ("Insufficient scope", "The token role does not permit this endpoint."),
    E.WRITE_NOT_ALLOWED: ("Write not allowed", "This metric is not approved for writing."),
    E.NOT_FOUND: ("Not found", "The requested path is not registered."),
    E.METHOD_NOT_ALLOWED: ("Method not allowed", "The HTTP method is not allowed for this path."),
    E.INVALID_REQUEST: ("Invalid request", "The request body or a parameter is invalid."),
    E.UNKNOWN_DEVICE: ("Unknown device", "The device id is not configured."),
    E.UNKNOWN_METRIC: ("Unknown metric", "The metric name is not known."),
    E.WRITE_DISABLED: ("Write disabled", "Write support is disabled."),
    E.DOCS_NOT_AVAILABLE: ("Documentation not available", "The documentation is not released in this environment."),
    E.METRIC_IS_ACTION: ("Metric is an action", "This metric is an action; use the action endpoint."),
    E.FRESH_NOT_AVAILABLE: ("Fresh read not available", "This metric is delivered periodically; fresh is rejected."),
    E.BATCH_TOO_LARGE: ("Batch too large", "The request names more metrics than allowed."),
    E.FRESH_BATCH_TOO_LARGE: ("Fresh batch too large", "The fresh request names more metrics than allowed."),
    E.VALUE_OUT_OF_RANGE: ("Value out of range", "The value is outside the approved range."),
    E.VALUE_TYPE_MISMATCH: ("Value type mismatch", "The value does not match the metric type."),
    E.VALUE_NOT_FINITE: ("Value not finite", "The value is not a finite number."),
    E.VALUE_STEP_MISMATCH: ("Value step mismatch", "The value is not a multiple of the approved step."),
    E.INVALID_PARAMETER: ("Invalid parameter", "A query parameter has an invalid value."),
    E.RATE_LIMITED: ("Rate limited", "The request rate or the failed-authentication limit was exceeded."),
    E.DEVICE_BUDGET_EXHAUSTED: ("Device budget exhausted", "The device work budget is exhausted for this window."),
    E.INTERNAL_ERROR: ("Internal error", INTERNAL_ERROR_DETAIL),
    E.DEVICE_UNREACHABLE: ("Device unreachable", "The device connection could not be established."),
    E.DEVICE_TIMEOUT: ("Device timeout", "The device did not answer in time."),
    E.PROTOCOL_ERROR: ("Protocol error", "The device answer could not be decoded."),
    E.DEVICE_UNAVAILABLE: ("Device unavailable", "None of the requested metrics could be determined."),
    E.WRITE_OUTCOME_UNKNOWN: ("Write outcome unknown", "The write was sent but could not be confirmed."),
    E.ACTION_OUTCOME_UNKNOWN: ("Action outcome unknown", "The action was sent but its outcome is unclear."),
    E.QUEUE_FULL: ("Queue full", "The device queue is full; retry later."),
    E.DEVICE_MAINTENANCE: ("Device in maintenance", "The device is temporarily not being addressed."),
    E.NOT_READY: ("Not ready", "The service or a device is not ready."),
    E.QUEUE_TIMEOUT: ("Queue timeout", "The request waited too long in the device queue."),
    E.DISPATCH_MODE_UNAVAILABLE: ("Dispatch mode unavailable", "The requested dispatch mode is not available."),
    E.DISPATCH_LIMITS_MISSING: ("Dispatch limits missing", "Charge and discharge limits must be configured first."),
    E.DISPATCH_RESTORE_REQUIRED: ("Restore required", "The previous device state must be restored first."),
    E.DISPATCH_OPERATION_CONFLICT: ("Dispatch conflict", "The expected operation is no longer active."),
    E.DISPATCH_NOT_FOUND: ("Dispatch not found", "No battery dispatch operation is active."),
    E.DISPATCH_STORE_UNAVAILABLE: ("Dispatch unavailable", "The durable dispatch store is unavailable."),
    E.DISPATCH_UNVERIFIED: ("Dispatch unverified", "The hardware-specific external-control mode is not configured."),
    E.DISPATCH_CAPABILITY_CONFLICT: (
        "Dispatch capability conflict",
        "The capability has an active operation; retry with force to withdraw it anyway.",
    ),
    E.DISPATCH_RECORD_CORRUPT: (
        "Dispatch record corrupt",
        "The persisted dispatch state for this device could not be read; operator action is required.",
    ),
    E.DISPATCH_SNAPSHOT_STALE: (
        "Dispatch snapshot stale",
        "The device state could not be read freshly enough to start a new dispatch; retry.",
    ),
    E.ENERGY_MANAGER_DISARMED: (
        "Energy Manager is off",
        "Energy Manager is off - switch it on for this inverter first.",
    ),
    E.ENERGY_WRITE_SUPPORT_REQUIRED: (
        "Write access required",
        "Write access must be enabled before the Energy Manager can be switched on.",
    ),
    E.ENERGY_ACTION_UNAVAILABLE: (
        "Action unavailable",
        "This action is not available for this inverter right now.",
    ),
}

# Internal codes that differ from the published key; also keeps the field-only keys off the top level.
_ALIASES = {
    "budget_exhausted": E.DEVICE_BUDGET_EXHAUSTED,
    "shutdown": E.NOT_READY,
    "restore_in_progress": E.DEVICE_MAINTENANCE,
    "invalid_float": E.PROTOCOL_ERROR,
    "decode_length_mismatch": E.PROTOCOL_ERROR,
}
_BY_CLASS = (
    (DeviceTimeout, E.DEVICE_TIMEOUT),
    (DeviceUnreachable, E.DEVICE_UNREACHABLE),
    (ProtocolError, E.PROTOCOL_ERROR),
)


class FieldError(BaseModel):
    parameter: str
    code: str
    detail: str


class ProblemDetails(BaseModel):
    """RFC 9457 problem details, media type application/problem+json."""

    type: str
    title: str
    status: int
    detail: str
    instance: str
    code: ErrorCode
    correlation_id: str
    timestamp: datetime
    errors: list[FieldError] | list[MetricError] = Field(default_factory=list)
    readback_value: ScalarValue | None = None  # only on write_outcome_unknown and action_outcome_unknown
    devices: list[DeviceReadiness] | None = None  # only on not_ready of the readiness endpoint


def normalize_code(exc: DeviceApiError) -> ErrorCode:
    """Published error key for an internal error; unknown codes are internal errors."""
    alias = _ALIASES.get(exc.code)
    if alias is not None:
        return alias
    try:
        return ErrorCode(exc.code)
    except ValueError:
        # Detail codes such as "response_timeout" keep the class of the error.
        for kind, mapped in _BY_CLASS:
            if isinstance(exc, kind):
                return mapped
        return ErrorCode.INTERNAL_ERROR


def field_text(code: str) -> str:
    """Safe description for a key inside ``errors``."""
    try:
        return _TEXT[ErrorCode(code)][1]
    except (ValueError, KeyError):
        return "The request could not be completed."


class ProblemError(Exception):
    """Raised by routers for client errors that need more than the error key (status, errors, devices)."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        status: int | None = None,
        detail: str | None = None,
        errors: list[FieldError] | list[MetricError] | None = None,
        devices: list[DeviceReadiness] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.status = status
        self.detail = detail
        self.errors = errors or []
        self.devices = devices
        self.headers = headers or {}


def correlation_of(request: Request) -> str:
    return request.scope.get("correlation_id", "-")


def _now(request: Request) -> datetime:
    runtime = getattr(request.app.state, "runtime", None)
    return runtime.clock.now() if runtime is not None else datetime.now(UTC)


def _base_uri(request: Request) -> str:
    runtime = getattr(request.app.state, "runtime", None)
    return runtime.settings.problem_type_base_uri if runtime is not None else "urn:device-api:problem"


def build_problem(
    request: Request,
    code: ErrorCode,
    *,
    status: int | None = None,
    detail: str | None = None,
    errors: list[FieldError] | list[MetricError] | None = None,
    devices: list[DeviceReadiness] | None = None,
    readback_value: ScalarValue | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    title, default_detail = _TEXT[code]
    status = status or STATUS[code]
    body: dict[str, Any] = {
        "type": f"{_base_uri(request)}:{code.value}",
        "title": title,
        "status": status,
        "detail": INTERNAL_ERROR_DETAIL if status == 500 else (detail or default_detail),
        "instance": request.url.path,
        "code": code.value,
        "correlation_id": correlation_of(request),
        "timestamp": _now(request).isoformat().replace("+00:00", "Z"),
    }
    if errors:
        body["errors"] = [e.model_dump(mode="json") for e in errors]
    if code in (E.WRITE_OUTCOME_UNKNOWN, E.ACTION_OUTCOME_UNKNOWN):
        body["readback_value"] = readback_value
    if devices is not None:
        body["devices"] = [d.model_dump(mode="json") for d in devices]
    out = dict(headers or {})
    if status == 401:
        out["WWW-Authenticate"] = "Bearer"
    return JSONResponse(body, status_code=status, headers=out, media_type=PROBLEM_MEDIA_TYPE)


def _retry_headers(exc: DeviceApiError) -> dict[str, str]:
    retry = exc.context.get("retry_after")
    if isinstance(retry, int | float) and math.isfinite(retry):
        return {"Retry-After": str(max(1, math.ceil(retry)))}
    return {}


async def _device_error(request: Request, exc: DeviceApiError) -> JSONResponse:
    code = normalize_code(exc)
    if code is E.INTERNAL_ERROR:
        log.error("Unmapped internal error code %s", exc.code, exc_info=exc)
    readback = exc.context.get("readback_value")
    return build_problem(request, code, readback_value=readback, headers=_retry_headers(exc))


async def _problem_error(request: Request, exc: ProblemError) -> JSONResponse:
    return build_problem(
        request, exc.code, status=exc.status, detail=exc.detail, errors=exc.errors, devices=exc.devices,
        headers=exc.headers,
    )  # fmt: skip


def _is_write_path(request: Request) -> bool:
    """Method- and path-exact match of the endpoints that only exist with write support."""
    path = request.url.path.rstrip("/").split("/")
    if path[:4] != ["", "api", "v1", "devices"]:
        return False
    # Bounded, never dropped: the energy status sits at depth 6, every other pattern at depth 7.
    # This runs inside the global HTTP exception handler, where an IndexError on /api/v1/devices or
    # /api/v1/devices/main would turn a plain 404 into an unhandled 500, so each clause below
    # checks its own length too.
    if not 6 <= len(path) <= 7:
        return False
    return (
        (len(path) == 7 and request.method == "PUT" and path[5] == "metrics")
        or (len(path) == 7 and request.method == "POST" and path[5] == "actions")
        or (
            len(path) == 7
            and path[5:7] == ["battery", "dispatch"]
            and request.method in {"POST", "GET", "DELETE"}
        )
        or (len(path) == 6 and path[5] == "energy" and request.method == "GET")
        or (len(path) == 7 and path[5] == "energy" and path[6] == "command")
    )


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    keep = {k: v for k, v in (exc.headers or {}).items() if k.lower() == "allow"}
    if request.url.path.startswith("/admin/api/"):
        # The admin GUI shows `detail` itself; the API problem texts (e.g. "bearer token") do not apply here.
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=keep or None)
    runtime = getattr(request.app.state, "runtime", None)
    write_off = runtime is not None and not runtime.settings.enable_write_support
    if exc.status_code in (404, 405) and write_off and _is_write_path(request):
        return build_problem(request, E.WRITE_DISABLED)
    match exc.status_code:
        case 404:
            return build_problem(request, E.NOT_FOUND, headers=keep)
        case 405:
            return build_problem(request, E.METHOD_NOT_ALLOWED, headers=keep)
        case 401:
            return build_problem(request, E.INVALID_TOKEN)
        case 403:
            return build_problem(request, E.INSUFFICIENT_SCOPE)
        case _ if exc.status_code < 500:
            return build_problem(request, E.INVALID_REQUEST, status=exc.status_code)
        case _:
            return build_problem(request, E.INTERNAL_ERROR, status=exc.status_code)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    top_code = E.INVALID_REQUEST
    fields: list[FieldError] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        is_query = bool(loc and loc[0] == "query")
        if is_query:
            top_code = E.INVALID_PARAMETER
        # Each field keeps its own class; the order of the errors must not change it.
        field_code = E.INVALID_PARAMETER if is_query else E.INVALID_REQUEST
        name = ".".join(str(p) for p in loc[1:]) or (str(loc[0]) if loc else "request")
        # Only the validator message is echoed, never the rejected input.
        fields.append(FieldError(parameter=name, code=field_code.value, detail=str(err.get("msg", "invalid value"))))
    return build_problem(request, top_code, errors=fields)


def register_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DeviceApiError, _device_error)
    app.add_exception_handler(ProblemError, _problem_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)


def problem_responses(*codes: ErrorCode) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` entries naming the possible error keys per status (Requirement 25.21)."""
    by_status: dict[int, list[str]] = {}
    for code in codes:
        by_status.setdefault(STATUS[code], []).append(code.value)
    return {
        status: {
            "model": ProblemDetails,
            "description": "Problem details. Possible codes: " + ", ".join(sorted(keys)),
            "content": {PROBLEM_MEDIA_TYPE: {}},
        }
        for status, keys in by_status.items()
    }
