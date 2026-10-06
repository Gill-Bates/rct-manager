#!/usr/bin/env python3
#
# app/api/routers/metrics.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""``GET /metrics`` in the Prometheus text format; a projection that never touches a device (Requirement 20)."""

import asyncio
from ipaddress import ip_address

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.api.problems import ErrorCode, problem_responses
from app.api.runtime import RuntimeDep
from app.errors import AuthenticationError
from app.observability.exporter import CONTENT_TYPE
from app.security.dependencies import Context, source_address

router = APIRouter(tags=["metrics"])


def _trusted_peer(request: Request, runtime_trusted: list) -> bool:
    peer = request.client.host if request.client else None
    try:
        return peer is not None and any(ip_address(peer) in net for net in runtime_trusted)
    except ValueError:
        return False


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    response_class=PlainTextResponse,
    responses=problem_responses(ErrorCode.MISSING_TOKEN, ErrorCode.INVALID_TOKEN, ErrorCode.RATE_LIMITED),
)
async def scrape(request: Request, runtime: RuntimeDep, ctx: Context) -> PlainTextResponse:
    """Export service, transport and device metrics in Prometheus text format.

    Scraping performs no device reads. Device values are exported only for numeric,
    preselected registry entries with a retained, non-expired cache value. Text values
    are omitted. Values in the cache grace period include a metric age series.

    Registry units determine metric name suffixes, for example W becomes `_watts`
    and Wh becomes `_watt_hours`: `energy_e_load_day` is exported as
    `rct_energy_e_load_day_watt_hours`. Adding or changing a unit renames the series;
    collector queries and dashboards must use the new name. Values, including negative
    feed-in energy and subnormal floats, are exported without changing their sign or scale.
    Labeled enum values use `state` series with 1 for the active state and 0 otherwise.
    """
    settings = runtime.settings
    if not settings.enable_metrics_endpoint:
        raise HTTPException(status_code=404)
    address = source_address(request, ctx)
    caller = address
    # Only the scrape trust list (peer address, no forwarded header) skips the token; the rate counter is separate.
    if settings.metrics_require_token and not _trusted_peer(request, settings.metrics_trusted_sources):
        ctx.limiter.check_auth_blocked(address)
        try:
            # authenticate_required, so AUTH_REQUIRED=false cannot void METRICS_REQUIRE_TOKEN.
            principal = await asyncio.to_thread(
                ctx.tokens.authenticate_required, request.headers.get("authorization")
            )
        except AuthenticationError:
            ctx.limiter.record_auth_failure(address)
            raise
        caller = principal.token_id or address
        request.scope["token_id"] = principal.token_id
    ctx.limiter.check_scrape(caller)
    assert runtime.exporter is not None
    return PlainTextResponse(runtime.exporter.render(), media_type=CONTENT_TYPE, headers={"Cache-Control": "no-store"})
