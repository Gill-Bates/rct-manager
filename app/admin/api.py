#!/usr/bin/env python3
#
# app/admin/api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""JSON API for the local browser administration interface."""

import asyncio
import contextlib
import hmac
import json
import logging
import re
import threading
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from ipaddress import ip_address
from secrets import token_urlsafe
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.admin.updates import check_for_updates
from app.api.restart import in_container, probe_listener
from app.catalog.base import is_numeric
from app.config import (
    DEVICE_ID_PATTERN,
    DISPLAY_NAME_MAX,
    MAX_DEVICES,
    Settings,
    TokenRole,
)
from app.dispatch.controller import ReconfigurationRejected
from app.dispatch.models import DispatchState
from app.energy.models import EnergyMode
from app.energy.readings import EnergyReadings, absent_readings
from app.errors import ConfigError, ReconfigurationBuildError
from app.gateway.battery_inventory import (
    _BATTERY_TOWER_PREFIXES,
    _battery_module_report,
    _battery_tower_present,
)
from app.gateway.cache import CacheFreshness
from app.gateway.rct_dispatch import RctDispatchGateway
from app.hostnames import strip_ipv6_brackets
from app.security.client_ip import ClientIpResolver
from app.security.dependencies import source_address
from app.security.ratelimit import admin_session_key

router = APIRouter(prefix="/admin/api", include_in_schema=False)
_COOKIE = "rct_admin_session"
_CSRF_COOKIE = "rct_admin_csrf"

# Community-observed bit meanings for battery.status2 (OID 0xDE3D20D, t_int32). RCT's own protocol
# document (docs/reference) has no bit table for this object; these flags come from user-reported
# RCT community observations (ioBroker RCT adapter) and are not an official vendor specification.
_BATTERY_STATUS2_BITS: dict[int, str] = {
    0x001: "Disconnected",
    0x002: "Syncing",
    0x004: "Connecting",
    0x008: "Calibrating",
    0x100: "Balancing",
    0x200: "Low voltage",
    0x400: "Calibrating",
    0x800: "Balance due",
}


def _battery_status2_label(value: int) -> str:
    """Best-effort, bitwise decoding into short badge texts; an unrecognized bit keeps its raw code."""
    if value == 0:
        # Zero means none of the known exceptional status bits is active. "Operational" conveys
        # that state without claiming that the charge level itself is normal.
        return "Operational"
    if value < 0:
        return f"Status {value}"
    labels = [label for bit, label in _BATTERY_STATUS2_BITS.items() if value & bit]
    known_mask = sum(bit for bit in _BATTERY_STATUS2_BITS if value & bit)
    if "Balancing" in labels and "Balance due" in labels:
        labels.remove("Balance due")  # active implies due; showing both is redundant
    labels = list(
        dict.fromkeys(labels)
    )  # charging and discharging calibration share one text
    if not labels:
        return f"Status {value}"
    if (
        value & ~known_mask
    ):  # bits outside the known table: keep the code visible, not hidden
        labels.append(f"code {value}")
    return " · ".join(labels)


def _humanize_enum_label(label: str) -> str:
    """``inverter_state`` enum labels are lower snake_case; this is purely cosmetic formatting."""
    return label.replace("_", " ").capitalize()


# The push exporter task reads these from runtime.settings only when it (re)starts, so a saved
# change swaps the exporter task in place; the process keeps running.
_EXPORT_RESTART_KEYS = frozenset(
    {
        "db_type",
        "metrics_export_enabled",
        "metrics_export_interval_seconds",
        "influxdb_hostname",
        "influxdb_port",
        "influxdb_tls_enabled",
        "influxdb_verify_tls",
        "influxdb_measurement_name",
        "influxdb_allow_plaintext_credentials",
        "influxdb_organization",
        "influxdb_bucket",
        "influxdb_token",
        "questdb_hostname",
        "questdb_port",
        "questdb_tls_enabled",
        "questdb_verify_tls",
        "questdb_measurement_name",
        "questdb_allow_plaintext_credentials",
        "questdb_username",
        "questdb_password",
        "questdb_downsampling",
        "questdb_raw_retention_days",
        "questdb_retention_days",
    }
)
_EDITABLE = (
    frozenset(
        {
            "auth_required",
            "session_max_days",
            "docs_public",
            "enable_metrics_endpoint",
            "enable_write_support",
            "devices",
            "bind_address",
            "bind_port",
            "log_level",
            "behind_reverse_proxy",
            "trusted_proxies",
            "forwarded_header",
            "metrics_require_token",
            "metrics_trusted_sources",
            "metrics_rate_limit_requests",
            "metrics_rate_limit_window_seconds",
            # Live Charge Envelope freshness, applied on restart (the DispatchConfig is built at startup).
            "dispatch_live_charge_limit_max_age_seconds",
        }
    )
    | _EXPORT_RESTART_KEYS
)
# The device/endpoint/scheduling graph is rebuilt in place by reconfigure_devices(), so a saved
# devices list is applied by that targeted rebuild.
_DEVICE_LIVE_KEYS = frozenset({"devices"})
# The one listener per process: applied by a debounced background re-exec after a graceful shutdown.
_LISTENER_KEYS = frozenset({"bind_address", "bind_port"})
_LIVE = (
    frozenset(
        {
            "auth_required",
            "session_max_days",
            "docs_public",
            "enable_metrics_endpoint",
            "behind_reverse_proxy",
            "metrics_require_token",
            "metrics_trusted_sources",
            "metrics_rate_limit_requests",
            "metrics_rate_limit_window_seconds",
            "enable_write_support",
            "log_level",
            "trusted_proxies",
            "forwarded_header",
        }
    )
    | _EXPORT_RESTART_KEYS
    | _DEVICE_LIVE_KEYS
)
_SECRET_EDITABLE = frozenset({"influxdb_token", "questdb_password"})
# Auth and proxy-trust settings: a PAT could switch auth off or widen trust, so cookie session only.
_SESSION_ONLY_SETTINGS = frozenset(
    {
        "auth_required",
        "session_max_days",
        "trusted_proxies",
        "bind_address",
        "behind_reverse_proxy",
        "forwarded_header",
        "metrics_require_token",
        "metrics_trusted_sources",
        "enable_write_support",
        "devices",
        "docs_public",
        "bind_port",
        "log_level",
    }
)
# Export target and credentials: a PAT could redirect metrics and stored credentials to another host.
# Still readable with a PAT. Retention settings stay here too, so a PAT cannot make the TSDB drop history.
_EXPORT_TARGET_SETTINGS = frozenset(
    {
        "db_type",
        "influxdb_hostname",
        "influxdb_port",
        "influxdb_tls_enabled",
        "influxdb_verify_tls",
        "influxdb_allow_plaintext_credentials",
        "influxdb_organization",
        "influxdb_bucket",
        "influxdb_token",
        "questdb_hostname",
        "questdb_port",
        "questdb_tls_enabled",
        "questdb_verify_tls",
        "questdb_allow_plaintext_credentials",
        "questdb_username",
        "questdb_password",
        "questdb_retention_days",
        "questdb_raw_retention_days",
        "questdb_downsampling",
    }
)
_SETTINGS_LOCK = threading.Lock()
# Serializes live device reconfigurations without holding _SETTINGS_LOCK while they run.
_RECONFIGURE_LOCK = threading.Lock()
_PARAMETERS_LOCK = threading.Lock()
_WRITE_DEFAULTS_APPLIED_KEY = "write_defaults_applied"
_RECONFIGURE_TIMEOUT_SECONDS = 120.0
_EXPORT_RESTART_TIMEOUT_SECONDS = 30.0
# Guards the "one live transition at a time" check-and-schedule in _run_on_loop.
_TRANSITION_GUARD = threading.Lock()
# Serializes transition-bearing admin requests across the whole check -> persist -> apply span, so a
# request that will hit a busy transition is rejected (409) BEFORE it writes anything, instead of
# persisting its desired state and only then failing with _TransitionBusy at scheduling time. It is
# an OUTER, request-level lock: handlers take it first, then the existing inner locks
# (_SETTINGS_LOCK / _RECONFIGURE_LOCK / _PARAMETERS_LOCK) and finally _TRANSITION_GUARD inside
# _run_on_loop. No path acquires _LIVE_CHANGE_LOCK while holding any inner lock, so the nesting
# order is fixed and deadlock-free.
_LIVE_CHANGE_LOCK = threading.Lock()
# Settings keys whose change needs a live transition on the event loop.
_TRANSITION_KEYS = _EXPORT_RESTART_KEYS | _DEVICE_LIVE_KEYS | {"enable_write_support"}
log = logging.getLogger(__name__)


def update_write_names(
    store,
    gateway,
    build_write_allowlist,
    mutator: Any,
    *,
    extra: dict[str, Any] | None = None,
    after_persist: Any = None,
) -> list[str]:
    """The one shared critical section for every write-names allowlist mutation (M8).

    Serializes read-check-write-allowlist-update between the ``/parameters`` route's revoke and the
    Energy Manager's add-only mode-switch widen, which used to run under two independent locks
    (``_PARAMETERS_LOCK`` here, ``EnergyManager._arm()``'s own ``asyncio.Lock`` there) and could
    interleave. ``mutator`` receives the currently persisted write_names and returns the new list
    (or raises to refuse); it runs under the lock, so its read of ``current`` is never stale by the
    time the new value is persisted. The energy manager side invokes this via
    ``asyncio.to_thread()`` so it shares this same (synchronous) lock instead of its own.
    """
    with _PARAMETERS_LOCK:
        current = list(store.get("write_names") or [])
        new = mutator(current)
        allowlist = build_write_allowlist(new)  # build first: persist only what works
        payload = {"write_names": new}
        if extra:
            payload.update(extra)
        store.put_many(payload)
        gateway.set_allowlist(allowlist)
        if after_persist is not None:
            after_persist(new)
        return new


def _store(request: Request):
    store = getattr(request.app.state, "admin_store", None)
    if store is None:
        raise HTTPException(503, "Administration is unavailable")
    return store


def admin_session(request: Request) -> dict | None:
    store = getattr(request.app.state, "admin_store", None)
    if store is None:
        return None
    return store.session(request.cookies.get(_COOKIE))


def _public_scheme(request: Request) -> str:
    """The scheme the browser used: X-Forwarded-Proto counts only from a trusted proxy peer."""
    peer = request.client.host if request.client else None
    forwarded = (
        request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    )
    if forwarded in (
        "http",
        "https",
    ) and request.app.state.security.client_ip.is_trusted_peer(peer):
        return forwarded
    return request.url.scheme


def _same_origin(request: Request) -> bool:
    base = f"{_public_scheme(request)}://{request.url.netloc}"
    origin = request.headers.get("origin")
    if origin:
        return origin == base
    referer = request.headers.get("referer")
    if referer:
        return referer.startswith(base + "/")
    # No Origin and no Referer: fail open only for same-origin GETs (safe methods carry no CSRF
    # risk and some same-origin navigations send neither header). State-changing methods must
    # fail closed (SEC-02): _csrf() is only ever called on mutations, so a missing-header request
    # there is treated as cross-origin.
    return request.method in ("GET", "HEAD", "OPTIONS")


def _csrf(request: Request, session: dict | None = None) -> None:
    cookie = request.cookies.get(_CSRF_COOKIE)
    header = request.headers.get("x-csrf-token")
    if (
        not _same_origin(request)
        or not cookie
        or not header
        or not hmac.compare_digest(cookie, header)
    ):
        raise HTTPException(403, "Invalid CSRF token")
    if session is not None and not _store(request).verify_csrf(session, header):
        raise HTTPException(403, "Invalid CSRF token")


def _bearer_admin(request: Request, privileged: bool) -> bool:
    """Authorize by a PAT (no cookie session); independent of the auth_required setting.

    ``privileged`` (mutations and the settings/token listings) needs the read/write role.
    """
    header = request.headers.get("authorization", "")
    scheme, _, secret = header.partition(" ")
    if scheme.lower() != "bearer" or not secret.strip():
        return False
    store = _store(request)
    ctx = request.app.state.security
    address = source_address(request, ctx)
    ctx.limiter.check_auth_blocked(address)
    ctx.limiter.check_request("admin:" + address)
    entry = store.authenticate_token(secret.strip())
    if entry is None:
        ctx.limiter.record_auth_failure(address)
        raise HTTPException(401, "Invalid token")
    # Tokens stay inert until the first-login password change completed.
    if store.password_change_pending():
        raise HTTPException(403, "Password change required")
    # A PAT must never bypass the mandatory one-time disclaimer acceptance either.
    if store.disclaimer_required():
        raise HTTPException(403, "Disclaimer acceptance required")
    if privileged and entry.role != TokenRole.READ_WRITE:
        raise HTTPException(403, "Read/write token required")
    return True


def require_admin(
    request: Request,
    *,
    mutation: bool = False,
    allow_password_change: bool = False,
    allow_disclaimer: bool = False,
    session_only: bool = False,
    sensitive: bool = False,
) -> dict | None:
    """Return the cookie session, or None for a valid PAT.

    ``sensitive`` marks a read that exposes settings or token metadata: it needs read/write like a mutation.

    ``session_only`` refuses a PAT outright: security-relevant changes (tokens, trust and
    authentication settings) must not be reachable with a leaked automation token.

    ``allow_disclaimer`` marks an endpoint that is part of the mandatory-disclaimer flow itself
    (logout, password change, the acceptance endpoint); every other endpoint stays locked while the
    disclaimer is unaccepted, so the modal cannot be bypassed by calling the API directly.
    """
    bearer = (
        not allow_password_change
        and not allow_disclaimer
        and "authorization" in request.headers
        and not request.cookies.get(_COOKIE)
    )
    if bearer and session_only:
        raise HTTPException(403, "Administration session required")
    if bearer and _bearer_admin(request, mutation or sensitive):
        return None
    session = admin_session(request)
    if session is None:
        raise HTTPException(401, "Login required")
    ctx = request.app.state.security
    ctx.limiter.check_admin_session(
        admin_session_key(request.cookies.get(_COOKIE), source_address(request, ctx))
    )
    if mutation:
        _csrf(request, session)
    if session["must_change_password"] and not allow_password_change:
        raise HTTPException(403, "Password change required")
    if session["disclaimer_required"] and not allow_disclaimer:
        raise HTTPException(403, "Disclaimer acceptance required")
    return session


def _secure_cookies(request: Request) -> bool:
    return bool(
        request.app.state.runtime.settings.behind_reverse_proxy
        or _public_scheme(request) == "https"
    )


def _set_cookies(response: Response, request: Request, token: str, csrf: str) -> None:
    secure = _secure_cookies(request)
    days = request.app.state.admin_desired_settings.session_max_days
    max_age = days * 86400 if days else None
    response.set_cookie(
        _COOKIE,
        token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )
    response.set_cookie(
        _CSRF_COOKIE,
        csrf,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=4096)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=4096)
    new_password: str = Field(min_length=8, max_length=1024)


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    role: str
    expires_at: datetime | None = None


class ParameterSelection(BaseModel):
    exposed_names: list[str] = Field(max_length=64)
    write_names: list[str]


@router.get("/session")
def session(request: Request, response: Response) -> dict:
    state = admin_session(request)
    csrf = request.cookies.get(_CSRF_COOKIE)
    if state is None or not csrf or not _store(request).verify_csrf(state, csrf):
        if state is not None:
            _store(request).delete_session(request.cookies.get(_COOKIE))
        csrf = token_urlsafe(32)
        response.set_cookie(
            _CSRF_COOKIE,
            csrf,
            httponly=True,
            secure=_secure_cookies(request),
            samesite="lax",
            path="/",
        )
        return {
            "authenticated": False,
            "must_change_password": False,
            "disclaimer_required": False,
            "disclaimer_accepted_at": None,
            "csrf_token": csrf,
        }
    return {
        "authenticated": True,
        "must_change_password": state["must_change_password"],
        "disclaimer_required": state["disclaimer_required"],
        "disclaimer_accepted_at": state["disclaimer_accepted_at"],
        "csrf_token": csrf,
    }


# Idle interval between SSE heartbeats (``: ping`` comment lines). Keeps reverse proxies and idle
# timeouts from dropping an otherwise-silent stream; bounded so the generator never blocks forever.
_SSE_HEARTBEAT_SECONDS = 15.0


def _sse_frame(event) -> str:
    """Serialise one hub Event to an SSE frame: id + named event type + JSON data line."""
    payload = json.dumps(event.data, ensure_ascii=False)
    return f"id: {event.id}\nevent: {event.type}\ndata: {payload}\n\n"


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    """Central, domain-agnostic SSE feed of in-process events (text/event-stream).

    Authenticated like the rest of the admin read API: ``require_admin`` accepts the same-origin
    ``rct_admin_session`` cookie that the browser's ``EventSource`` sends automatically (a valid PAT
    also passes), so there is no token in the URL and no new secret. The endpoint owns only transport
    and subscription lifecycle; it serialises whatever typed events the hub carries (Phase 1: the
    ``tsdb.log`` events published by the logging handler) without any domain knowledge.

    ``Last-Event-ID`` is read and ignored for durability (Phase 1 does no reliable replay); a
    reconnecting client simply receives whatever is still in the per-type backlog.
    """
    require_admin(request)
    hub = request.app.state.event_hub
    _ = request.headers.get(
        "last-event-id"
    )  # accepted cleanly; no durable replay in Phase 1

    async def generator() -> AsyncIterator[str]:
        async with hub.subscribe() as (backlog, queue):
            for (
                event
            ) in backlog:  # replay the recent per-type backlog, then stream live
                yield _sse_frame(event)
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=_SSE_HEARTBEAT_SECONDS
                    )
                except TimeoutError:
                    yield ": ping\n\n"  # heartbeat; keeps proxies/idle timeouts from dropping the stream
                    continue
                yield _sse_frame(event)

    # RequestContextMiddleware already forces Cache-Control: no-store on every response (stronger than
    # no-cache, correct for a live stream), so it is not repeated here. X-Accel-Buffering disables
    # proxy buffering so events are flushed to the browser immediately.
    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # tell nginx not to buffer the stream
        },
    )


@router.get("/check-updates")
def check_updates(request: Request, response: Response, force: bool = False) -> dict:
    """Check the latest RCT REST API release for a signed-in administrator."""
    require_admin(request)
    response.headers["Cache-Control"] = "no-store" if force else "private, max-age=300"
    return check_for_updates(force=force)


@router.post("/login")
def login(body: Login, request: Request, response: Response) -> dict:
    _csrf(request)
    ctx = request.app.state.security
    address = source_address(request, ctx)
    ctx.limiter.check_auth_blocked(address)
    ctx.limiter.check_request("admin-login:" + address)
    if not _store(request).verify_password(body.username, body.password):
        ctx.limiter.record_auth_failure(address)
        raise HTTPException(401, "Invalid credentials")
    _store(request).delete_session(request.cookies.get(_COOKIE))
    token, csrf = _store(request).new_session()
    _set_cookies(response, request, token, csrf)
    state = _store(request).session(token)
    return {
        "authenticated": True,
        "must_change_password": state["must_change_password"],
        "disclaimer_required": state["disclaimer_required"],
        "disclaimer_accepted_at": state["disclaimer_accepted_at"],
        "csrf_token": csrf,
    }


@router.post("/change-password")
def change_password(body: PasswordChange, request: Request, response: Response) -> dict:
    require_admin(
        request, mutation=True, allow_password_change=True, allow_disclaimer=True
    )
    ctx = request.app.state.security
    address = source_address(request, ctx)
    ctx.limiter.check_auth_blocked(address)
    if body.current_password == body.new_password:
        raise HTTPException(400, "New password must differ")
    if not _store(request).change_password(body.current_password, body.new_password):
        ctx.limiter.record_auth_failure(address)
        raise HTTPException(401, "Invalid credentials")
    token, csrf = _store(request).new_session()
    _set_cookies(response, request, token, csrf)
    state = _store(request).session(token)
    return {
        "authenticated": True,
        "must_change_password": False,
        "disclaimer_required": state["disclaimer_required"],
        "disclaimer_accepted_at": state["disclaimer_accepted_at"],
        "csrf_token": csrf,
    }


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    require_admin(
        request, mutation=True, allow_password_change=True, allow_disclaimer=True
    )
    _store(request).delete_session(request.cookies.get(_COOKIE))
    response.delete_cookie(_COOKIE, path="/")
    response.delete_cookie(_CSRF_COOKIE, path="/")
    return {"authenticated": False}


@router.post("/accept-disclaimer")
def accept_disclaimer(request: Request) -> dict:
    # Session-only (no PAT), CSRF-protected and exempt from the disclaimer gate itself; the server
    # generates the timestamp. Idempotent: a repeated accept keeps the first stored timestamp.
    require_admin(
        request,
        mutation=True,
        allow_password_change=True,
        allow_disclaimer=True,
        session_only=True,
    )
    accepted_at = _store(request).accept_disclaimer()
    return {"accepted": True, "accepted_at": accepted_at}


def _settings_view(settings: Settings) -> dict[str, Any]:
    result = {}
    for key in _EDITABLE - _SECRET_EDITABLE:
        value = getattr(settings, key)
        if key == "devices":
            result[key] = [device.model_dump(mode="json") for device in value]
        elif key in {"trusted_proxies", "metrics_trusted_sources"}:
            result[key] = [str(item) for item in value]
        elif key == "bind_address":
            result[key] = str(value)
        else:
            result[key] = value
    for key in _SECRET_EDITABLE:
        result[f"{key}_configured"] = getattr(settings, key) is not None
    return result


def _pat_safe_view(settings: Settings, session: dict | None) -> dict[str, Any]:
    """Authentication and proxy-trust values are shown to a session only, never to a PAT."""
    view = _settings_view(settings)
    if session is None:
        for key in _SESSION_ONLY_SETTINGS:
            view.pop(key, None)
    return view


def _settings_persisted(settings: Settings) -> dict[str, Any]:
    result = {
        key: value
        for key, value in _settings_view(settings).items()
        if not key.endswith("_configured")
    }
    for key in _SECRET_EDITABLE:
        secret = getattr(settings, key)
        if secret is not None:
            result[key] = secret.get_secret_value()
    return result


@router.get("/settings")
def get_settings(request: Request) -> dict:
    session = require_admin(request, sensitive=True)
    return {
        "settings": _pat_safe_view(request.app.state.admin_desired_settings, session),
        "live": sorted(_LIVE),
        # The UI warns that a listener port change does not follow the Docker port mapping.
        "in_container": in_container(),
    }


# This is a plausibility check for an address-shaped value, not DNS or full IP validation.
_HOST = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*|\[[0-9A-Fa-f:.]+\]|[0-9A-Fa-f:.]+)$"
)


def _network_id(item: dict[str, Any]) -> int | None:
    """Empty input means "directly attached"; the stored form must match the parsed-config one."""
    value = item.get("network_id")
    if value is None or value == "":
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 2**32 - 1
    ):
        raise HTTPException(400, "The network id must be a whole number")
    return value


def _normalize_host(host: str) -> str:
    """Brackets are URL syntax, not part of the host: the transport needs the bare address."""
    return strip_ipv6_brackets(host)


def _display_name(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or len(value) > DISPLAY_NAME_MAX
        or any(not c.isprintable() for c in value)
    ):
        raise HTTPException(
            400,
            f"The display name must be printable text of at most {DISPLAY_NAME_MAX} characters",
        )
    return value


def _normalize_devices(raw: Any) -> list[dict[str, Any]]:
    """Complete a host+port list: keep sent ids and names, assign stable ids to new devices."""
    if not isinstance(raw, list) or len(raw) > MAX_DEVICES:
        raise HTTPException(
            400, f"Devices must be a list of at most {MAX_DEVICES} inverters"
        )
    seen_addresses: set[tuple[str, int, int | None]] = set()
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(400, "Invalid inverter entry")
        host = str(item.get("host") or "").strip()
        port = item.get("port", 8899)
        if not host or not _HOST.fullmatch(host):
            raise HTTPException(
                400, "Enter a plain IP address or host name, without scheme or path"
            )
        host = _normalize_host(host)
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise HTTPException(400, "The port must be between 1 and 65535")
        network_id = _network_id(item)
        address = (host.lower(), port, network_id)  # slaves share the master's endpoint
        if address in seen_addresses:
            raise HTTPException(
                400, f"The inverter address {host}:{port} is listed twice"
            )
        seen_addresses.add(address)
        device_id = item.get("device_id") or None
        if device_id is not None and not isinstance(device_id, str):
            raise HTTPException(400, "The device id must be text")
        if device_id is not None and not DEVICE_ID_PATTERN.fullmatch(device_id):
            raise HTTPException(
                400,
                "The device id must start with a letter or digit and use only letters, digits, "
                "'_', '.' or '-' (at most 64 characters)",
            )
        entry = {
            "host": host,
            "port": port,
            "device_id": device_id,
            "display_name": _display_name(item.get("display_name")),
            "network_id": network_id,
        }
        result.append(entry)
    used = {e["device_id"] for e in result if e["device_id"]}
    if len(used) != len([e for e in result if e["device_id"]]):
        raise HTTPException(400, "Duplicate device id")
    for entry in result:
        if entry["device_id"]:
            continue
        candidate = "main" if "main" not in used else None
        number = 2
        while candidate is None or candidate in used:
            candidate = f"inverter-{number}"
            number += 1
        entry["device_id"] = candidate
        used.add(candidate)
    return result


class _ReconfigureFailed(HTTPException):
    """A failed live reconfiguration; ``rollback`` says whether the old graph is still running."""

    def __init__(self, status_code: int, detail: str, *, rollback: bool) -> None:
        super().__init__(status_code, detail)
        self.rollback = rollback


def _reconfigure_devices(
    request: Request, updated: Settings, previous: Settings
) -> None:
    """Rebuild the device/endpoint/scheduling graph in place from the just-saved device list.

    Same cross-thread scheduling as ``_restart_export``: ``put_settings`` runs in a worker thread,
    while the graph and its tasks live on the event loop.

    Failures are reported, never swallowed. ``rollback=True`` means the old graph is still the
    running one (dispatch restore refused, or the new graph could not be built), so the caller
    must revert the saved device list. Any later failure or a timeout leaves the live graph in an
    unknown state; the saved list stays and the operator is told so.
    """
    try:
        # The timeout only stops waiting: cancelling mid-teardown would be worse than a slow answer.
        _run_on_loop(request, "reconfigure_devices", _RECONFIGURE_TIMEOUT_SECONDS)
    except _TransitionBusy as exc:
        raise _ReconfigureFailed(409, _BUSY_DETAIL, rollback=False) from exc
    except ReconfigurationRejected as exc:
        raise _ReconfigureFailed(
            409,
            f"Device {exc.device_id!r} has an active battery dispatch that could not be safely "
            "restored for this change; cancel or wait out that operation and try again.",
            rollback=True,
        ) from exc
    except ReconfigurationBuildError as exc:
        log.error(
            "The new device list could not be built; the previous one keeps running: %s",
            exc,
        )
        raise _ReconfigureFailed(
            409,
            "The new device list could not be applied; the previous configuration is still active.",
            rollback=True,
        ) from exc
    except TimeoutError as exc:
        log.error(
            "Reconfiguring devices did not finish within %.0f s",
            _RECONFIGURE_TIMEOUT_SECONDS,
        )
        # The waiter gives up but the coroutine keeps running; keep central ownership of its future
        # so a later rollback-capable failure (build/restore phase, old graph still live) still
        # reverts the persisted device list. Without this the saved list would stay on the new
        # configuration while the untouched live graph runs the old one, with no caller left to
        # revert it.
        _defer_devices_rollback(request, updated, previous)
        raise _ReconfigureFailed(
            504,
            "Applying the device list is still in progress; check the device status before saving again.",
            rollback=False,
        ) from exc
    except Exception as exc:
        log.exception("Reconfiguring devices after a settings change failed")
        raise _ReconfigureFailed(
            500,
            "Applying the device list failed; devices may be offline; save again to retry.",
            rollback=False,
        ) from exc


def _restart_export(request: Request) -> None:
    """Swap the running push-exporter task for a fresh one built from the just-saved settings.

    ``put_settings`` runs sync (FastAPI offloads it to a worker thread), while the exporter task
    and its cancellation live on the event loop, so the restart coroutine is scheduled onto that
    loop and awaited from here instead of being run in-thread.
    """
    try:
        # A no-op until the lifespan has installed the hook on app.state.
        _run_on_loop(request, "restart_export", _EXPORT_RESTART_TIMEOUT_SECONDS)
    except _TransitionBusy as exc:
        raise HTTPException(409, _BUSY_DETAIL) from exc
    except TimeoutError as exc:
        log.error(
            "Restarting the metrics export did not finish within %.0f s",
            _EXPORT_RESTART_TIMEOUT_SECONDS,
        )
        raise HTTPException(
            504,
            "The export settings are saved, but restarting the export is still in progress.",
        ) from exc
    except Exception as exc:
        log.exception("Restarting the metrics export after a settings change failed")
        raise HTTPException(
            500,
            "The export settings are saved, but the export could not be restarted; the previous export may still be running.",
        ) from exc


def _changed_session_only(request: Request, body: dict[str, Any]) -> set[str]:
    """Session-only keys whose requested value differs from the current one.

    Re-sending an unchanged value (a client echoing the whole settings dict) is not a change.
    """
    current = _settings_view(request.app.state.admin_desired_settings)
    changed = {
        key
        for key in body
        if key in _SESSION_ONLY_SETTINGS and current.get(key) != body[key]
    }
    # Secrets are never in the view, and "" (keep) was dropped by the caller, so any remaining
    # presence is a change: a value replaces the credential, null deletes it.
    changed |= {key for key in body if key in _SECRET_EDITABLE}
    changed |= {
        key
        for key in body
        if key in _EXPORT_TARGET_SETTINGS - _SECRET_EDITABLE
        and current.get(key) != body[key]
    }
    # Switching the scrape endpoint on is a privilege change only where it would serve metrics
    # without a token (token requirement off, or trusted scrape sources configured). Switching it
    # off, or on behind the token requirement, stays available to a PAT.
    if body.get("enable_metrics_endpoint") is True and not current.get(
        "enable_metrics_endpoint"
    ):
        require_token = body.get(
            "metrics_require_token", current.get("metrics_require_token")
        )
        if not require_token or current.get("metrics_trusted_sources"):
            changed.add("enable_metrics_endpoint")
    return changed


@router.put("/settings")
def put_settings(body: dict[str, Any], request: Request) -> dict:
    session = require_admin(request, mutation=True)
    if not body or set(body) - _EDITABLE:
        raise HTTPException(400, "Unknown or empty setting")
    body = {
        k: v for k, v in body.items() if not (k in _SECRET_EDITABLE and v == "")
    }  # "" keeps the secret
    if not body:
        raise HTTPException(400, "Unknown or empty setting")
    if "devices" in body:
        body["devices"] = _normalize_devices(body["devices"])
    if session is None and _changed_session_only(request, body):
        raise HTTPException(403, "Administration session required")
    needs_transition = bool(_TRANSITION_KEYS & set(body))
    # A transition-bearing request holds _LIVE_CHANGE_LOCK across the whole check -> persist -> apply
    # span: the 409 re-check inside the lock refuses a busy transition before anything is persisted,
    # closing the window where two parallel requests both pass an early check, one persists its
    # desired state and then still fails with _TransitionBusy at scheduling time. Non-transition
    # requests skip the lock so autosaves of unrelated fields are not serialized behind a 120 s
    # device reconfiguration.
    with contextlib.ExitStack() as _live_change:
        if needs_transition:
            _live_change.enter_context(_LIVE_CHANGE_LOCK)
            # Refused before anything is persisted: a transition that outlived its wait is still
            # running. This is the early 409, now re-checked under the serialization lock.
            if _transition_running(request):
                raise HTTPException(409, _BUSY_DETAIL)
        return _put_settings_locked(body, request, session)


def _put_settings_locked(
    body: dict[str, Any], request: Request, session: dict | None
) -> dict:
    runtime = request.app.state.runtime
    if _LISTENER_KEYS & set(body):
        _check_listener(
            runtime.settings, request.app.state.admin_desired_settings, body
        )
    write_generation = 0
    with (
        _SETTINGS_LOCK
    ):  # keeps concurrent autosaves from publishing an older merge last
        previous = request.app.state.admin_desired_settings
        try:
            updated = _store(request).merge_operator_settings(previous, body)
        except ValidationError as exc:
            raise HTTPException(400, "Invalid setting") from exc
        request.app.state.admin_desired_settings = updated
        changed_export_keys = {
            key
            for key in _EXPORT_RESTART_KEYS & set(body)
            if getattr(previous, key) != getattr(updated, key)
        }
        devices_changed = "devices" in body and previous.devices != updated.devices
        runtime.settings = runtime.settings.model_copy(
            update={key: getattr(updated, key) for key in _LIVE}
        )
        request.app.state.security.tokens.set_auth_required(updated.auth_required)
        if "log_level" in body:
            _apply_log_level(previous.log_level, updated.log_level)
        if {"trusted_proxies", "forwarded_header"} & set(body):
            # One reference swap: a request sees the old or the new trust list, never a mix.
            request.app.state.security.client_ip = ClientIpResolver(
                updated.trusted_proxies, updated.forwarded_header
            )
        # Same lock, same moment as runtime.settings: the write routers refuse from here on.
        request.app.state.security.write_enabled = updated.enable_write_support
        if updated.enable_write_support != previous.enable_write_support:
            write_generation = request.app.state.write_support_generation = (
                getattr(request.app.state, "write_support_generation", 0) + 1
            )
        if {"metrics_rate_limit_requests", "metrics_rate_limit_window_seconds"} & set(
            body
        ):
            request.app.state.security.limiter.set_scrape_limit(
                updated.metrics_rate_limit_requests,
                updated.metrics_rate_limit_window_seconds,
            )
    write_warnings: dict[str, Any] = {}
    if updated.enable_write_support and not previous.enable_write_support:
        try:
            _enable_write_support(request, write_generation)
        except ConfigError as exc:
            _revert_write_support(request, write_generation)
            raise HTTPException(
                409,
                "The write allowlist could not be loaded; write access stays off. "
                "The other settings of this request were saved.",
            ) from exc
    elif previous.enable_write_support and not updated.enable_write_support:
        pending = _disable_write_support(request)
        if pending:
            write_warnings = {"write_restore_pending": pending}
    # The slow live reloads run outside _SETTINGS_LOCK so they cannot stall other settings requests.
    listener_changed = bool(_LISTENER_KEYS & set(body)) and (
        (updated.bind_address, updated.bind_port)
        != (runtime.settings.bind_address, runtime.settings.bind_port)
    )
    export_failure: HTTPException | None = None
    try:
        if changed_export_keys:
            try:
                _restart_export(request)
            except HTTPException as exc:
                export_failure = exc  # the device reconfiguration below must still run
        if devices_changed:
            with _RECONFIGURE_LOCK:
                try:
                    _reconfigure_devices(request, updated, previous)
                except _ReconfigureFailed as exc:
                    if exc.rollback:
                        _revert_devices(request, updated, previous)
                    raise
        if export_failure is not None:
            raise export_failure
    finally:
        # After the live reloads, so the graceful shutdown never overlaps a graph rebuild.
        if listener_changed:
            request.app.state.background_restart.request(
                f"listener {updated.bind_address}:{updated.bind_port}",
                getattr(request.app.state, "loop", None),
            )
    return {
        "settings": _pat_safe_view(updated, session),
        "live": sorted(_LIVE),
        "applying": listener_changed,
        **write_warnings,
    }


def _apply_log_level(previous: str, level: str) -> None:
    """Set the root logger level at runtime; logged at the old level too so the audit line survives."""
    log.info("Log level changing from %s to %s", previous, level)
    logging.getLogger().setLevel(level)
    log.info("Log level is now %s", level)


def _check_listener(running: Settings, desired: Settings, body: dict[str, Any]) -> None:
    """Refuse a listener that cannot be bound, before anything is saved or applied."""
    address = str(body.get("bind_address", desired.bind_address))
    port = body.get("bind_port", desired.bind_port)
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        return  # the model validation of the merge reports the malformed value
    try:
        ip_address(address)
    except ValueError:
        return  # the model validation of the merge reports the malformed value
    if (address, port) == (str(running.bind_address), running.bind_port):
        return
    reason = probe_listener(
        address,
        port,
        port_unchanged=port == running.bind_port,
        current_address=str(running.bind_address),
    )
    if reason is not None:
        raise HTTPException(409, f"{reason} The previous listener stays active.")


class _TransitionBusy(RuntimeError):
    """A previous live transition is still running on the event loop."""


_BUSY_DETAIL = (
    "A previous change is still being applied; wait for it to finish and try again."
)


def _transition_running(request: Request) -> bool:
    running = getattr(request.app.state, "live_transition", None)
    return running is not None and not running.done()


def _run_on_loop(request: Request, name: str, timeout: float) -> Any:
    """Run the app-state coroutine function ``name`` on the event loop from this worker thread.

    One transition at a time: a timeout only ends the wait, the coroutine keeps running, so the
    future stays in app state and further transitions are refused until it has finished.
    """
    func = getattr(request.app.state, name, None)
    loop = getattr(request.app.state, "loop", None)
    if func is None or loop is None:
        return None
    with _TRANSITION_GUARD:
        if _transition_running(request):
            raise _TransitionBusy(name)
        future = asyncio.run_coroutine_threadsafe(func(), loop)
        request.app.state.live_transition = future
    return future.result(timeout=timeout)


def _enable_write_support(request: Request, generation: int) -> None:
    """Make a just-enabled write switch effective: allowlist, default approvals, dispatch backing."""
    # Late import: app_factory imports this module.
    from app.api.app_factory import (
        _load_default_write_entries,
    )

    state = request.app.state
    # Reloaded because it is skipped at boot while writes are off; a broken file refuses the enable.
    state.default_write_entries = _load_default_write_entries(
        state.runtime.settings, state.runtime.catalog
    )
    _approve_default_writes(request)
    with _PARAMETERS_LOCK:
        names = list(_store(request).get("write_names") or [])
        state.runtime.gateway.set_allowlist(state.build_write_allowlist(names))
    try:
        _run_on_loop(request, "enable_dispatch", _RECONFIGURE_TIMEOUT_SECONDS)
    except _TransitionBusy:
        _revert_write_support(request, generation)
        raise HTTPException(409, _BUSY_DETAIL) from None
    except TimeoutError as exc:
        # The waiter gave up but enable_dispatch keeps running (see _run_on_loop). Reverting the
        # switch now would leave the saved/global write switch off while a still-building dispatch
        # may finish and activate. Keep central ownership of the future instead: revert only on a
        # later genuine failure, keep the enabled state on a late success.
        log.error(
            "Enabling battery dispatch did not finish within %.0f s",
            _RECONFIGURE_TIMEOUT_SECONDS,
        )
        _defer_write_support_reconcile(request, generation)
        raise HTTPException(
            504,
            "Write support is still being enabled; check the status before changing it again.",
        ) from exc
    except Exception:
        # Writes would be accepted by the router but have no dispatch to run on: fail closed.
        log.exception("Building battery dispatch for the enabled write support failed")
        _revert_write_support(request, generation)
        raise HTTPException(
            500,
            "Battery dispatch could not be started; write access stays off. "
            "The other settings of this request were saved.",
        ) from None


def _disable_write_support(request: Request) -> list[str]:
    """Hand the devices back to automatic operation after the switch went off; returns pending ids."""
    try:
        return (
            _run_on_loop(request, "disable_dispatch", _RECONFIGURE_TIMEOUT_SECONDS)
            or []
        )
    except Exception:
        log.exception("Restoring the inverters after disabling write access failed")
        return list(request.app.state.runtime.devices)


def _revert_write_support(request: Request, generation: int) -> None:
    """Switch write access back off after the live enable failed (it was off before).

    Skipped when a newer save has changed the switch since: that save owns the value now.
    """
    _revert_write_support_on_state(request.app.state, _store(request), generation)


def _approve_default_writes(request: Request) -> None:
    """Approve the registers manual dispatch needs, once, the first time write access is enabled.

    Add-only and applied a single time (persisted flag), so a register the operator later cleared
    on purpose is not silently approved again by a later off/on toggle.
    """
    state = request.app.state
    with _PARAMETERS_LOCK:
        store = _store(request)
        if store.get(_WRITE_DEFAULTS_APPLIED_KEY):
            return
        current = list(store.get("write_names") or [])
        wanted = [
            n
            for n in RctDispatchGateway.REQUIRED_WRITES
            if n in state.default_write_entries
        ]
        merged = current + [name for name in wanted if name not in current]
        allowlist = state.build_write_allowlist(
            merged
        )  # build first: persist only what works
        store.put_many({"write_names": merged, _WRITE_DEFAULTS_APPLIED_KEY: True})
        state.runtime.gateway.set_allowlist(allowlist)


def _revert_devices_on_state(
    state, store, updated: Settings, previous: Settings
) -> None:
    """Core device-list rollback, independent of a live HTTP request.

    Only the devices field is rolled back so the admin store, admin_desired_settings and
    runtime.settings agree with what the gateway has loaded; other fields of the same request stay
    applied. A newer save that already replaced the list is left alone. Takes ``state``/``store``
    rather than a request so the deferred post-timeout rollback (see ``_reconfigure_devices``) can
    run it without a caller still holding the request.
    """
    runtime = state.runtime
    with _SETTINGS_LOCK:
        current = state.admin_desired_settings
        if current.devices != updated.devices:
            return
        reverted = store.merge_operator_settings(
            current, {"devices": [d.model_dump(mode="json") for d in previous.devices]}
        )
        state.admin_desired_settings = reverted
        runtime.settings = runtime.settings.model_copy(
            update={"devices": previous.devices}
        )


def _revert_devices(request: Request, updated: Settings, previous: Settings) -> None:
    """Put the old device list back after the live graph rejected the new one."""
    _revert_devices_on_state(request.app.state, _store(request), updated, previous)


# Exceptions for which the old graph is still the live one, so reverting the saved device list
# keeps store and live state consistent (same classification as the synchronous rollback above).
_DEVICE_ROLLBACK_EXCEPTIONS = (ReconfigurationRejected, ReconfigurationBuildError)


def _defer_devices_rollback(
    request: Request, updated: Settings, previous: Settings
) -> None:
    """After a reconfiguration timed out, keep ownership of its still-running future centrally.

    The HTTP waiter is gone, but the coroutine runs on. If it eventually fails in the pre-teardown
    build/restore phase (old graph still live), revert the persisted device list so the store does
    not stay on the new configuration while the live graph runs the old one. A clean completion or
    a post-teardown failure needs no revert (the new graph is live, or readiness is already red).
    """
    state = request.app.state
    future = getattr(state, "live_transition", None)
    loop = getattr(state, "loop", None)
    store = getattr(state, "admin_store", None)
    if future is None or loop is None or store is None:
        return

    def _on_done(fut) -> None:
        try:
            exc = fut.exception()
        except (
            asyncio.CancelledError
        ):  # the coroutine was cancelled: nothing to roll back
            return
        if not isinstance(exc, _DEVICE_ROLLBACK_EXCEPTIONS):
            return
        log.error(
            "A device reconfiguration that outlived its HTTP wait failed rollback-capable; "
            "reverting the saved device list to the previous configuration"
        )
        # Offloaded: _revert_devices_on_state takes _SETTINGS_LOCK and writes SQLite, which must
        # not run on the event loop thread the done-callback fires on.
        loop.run_in_executor(
            None, _revert_devices_on_state, state, store, updated, previous
        )

    future.add_done_callback(_on_done)


def _revert_write_support_on_state(state, store, generation: int) -> None:
    """Core write-support rollback, independent of a live HTTP request.

    Takes ``state``/``store`` rather than a request so the deferred post-timeout reconcile (see
    ``_defer_write_support_reconcile``) can run it after the HTTP caller is gone. Generation-guarded
    like ``_revert_write_support``: a newer save that already changed the switch owns the value now.
    """
    with _SETTINGS_LOCK:
        if getattr(state, "write_support_generation", 0) != generation:
            return
        reverted = store.merge_operator_settings(
            state.admin_desired_settings, {"enable_write_support": False}
        )
        state.admin_desired_settings = reverted
        state.runtime.settings = state.runtime.settings.model_copy(
            update={"enable_write_support": False}
        )
        state.security.write_enabled = False


def _defer_write_support_reconcile(request: Request, generation: int) -> None:
    """After an enable timed out, keep ownership of the still-running ``enable_dispatch`` future.

    The HTTP waiter is gone, but the coroutine runs on. A clean completion keeps the enabled state
    the operator asked for; a later genuine failure reverts the switch so the gateway is not left
    accepting writes it has no dispatch to run on. A cancellation needs no revert.
    """
    state = request.app.state
    future = getattr(state, "live_transition", None)
    loop = getattr(state, "loop", None)
    store = getattr(state, "admin_store", None)
    if future is None or loop is None or store is None:
        return

    def _on_done(fut) -> None:
        try:
            exc = fut.exception()
        except (
            asyncio.CancelledError
        ):  # the coroutine was cancelled: nothing to roll back
            return
        if exc is None:  # late success: keep the enabled state
            return
        log.error(
            "A write-support enable that outlived its HTTP wait failed; reverting the write switch"
        )
        # Offloaded: _revert_write_support_on_state takes _SETTINGS_LOCK and writes SQLite, which
        # must not run on the event loop thread the done-callback fires on.
        loop.run_in_executor(
            None, _revert_write_support_on_state, state, store, generation
        )

    future.add_done_callback(_on_done)


def _exposed_names(runtime, store) -> list[str]:
    exposed = store.get("exposed_names")
    if exposed is None:
        exposed = (
            runtime.settings.metrics_exposed_names or runtime.catalog.preselected()
        )
    return exposed


def _tsdb_view(runtime) -> dict:
    """TSDB export status for the dashboard tile; additive, independent of the device list."""
    settings = runtime.settings
    stats = runtime.stats
    configured = settings.db_type is not None
    export_enabled = bool(stats.export_enabled) if stats else False
    last_success_unix = stats.export_last_success_unix if stats else None
    last_success_at = (
        datetime.fromtimestamp(last_success_unix, tz=UTC).isoformat()
        if last_success_unix is not None
        else None
    )
    if not configured:
        healthy = None  # neutral: "healthy" has no meaning when no TSDB is configured
    elif not export_enabled:
        healthy = (
            False  # configured but the export loop isn't running: nothing is being sent
        )
    else:
        # Generous but simple freshness window: 3x the configured push interval absorbs ordinary
        # scheduling jitter and one retry backoff doubling (same "allow slack past the nominal
        # period" idea as the ttl + grace pattern in app/cache.py).
        healthy = (
            last_success_unix is not None
            and (time.time() - last_success_unix)
            <= 3 * settings.metrics_export_interval_seconds
        )
    return {
        "configured": configured,
        "db_type": settings.db_type.value if configured else None,
        "export_enabled": export_enabled,
        "last_success_at": last_success_at,
        "healthy": healthy,
    }


def _device_card_label(catalog, name: str, value: Any) -> str | None:
    """Human-readable text for a device-card badge; None when the value is not an int."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if name in ("battery_status2", "battery_placeholder_0_status2"):
        return _battery_status2_label(value)
    entry = catalog.object_entry(name)
    label = entry.enum_labels.get(value)
    return _humanize_enum_label(label) if label else None


# Card roles that exist once per tower, with the catalog suffix that carries them.
_BATTERY_TOWER_METRIC_SUFFIXES = {
    "soc": "soc",
    "temperature": "temperature",
    "status": "status2",
}
# Card roles with no per-tower equivalent in the catalog: battery_placeholder_0_cycles and
# battery_placeholder_0_soc_target do not exist, and power_mng_bat_next_calib_date belongs to the
# power manager, not to one tower. They are therefore reported once, for the first tower, rather
# than invented for the second - a per-tower name the device does not have cannot be read.
_BATTERY_DEVICE_METRIC_NAMES = {
    "cycles": "battery_cycles",
    "soc_target": "battery_soc_target",
    "next_calibration": "power_mng_bat_next_calib_date",
}


def _battery_metric_names(
    runtime, prefix: str, *, include_device_wide: bool
) -> dict[str, str]:
    """Catalog names for one tower's card roles; only names the catalog actually carries."""
    names = {}
    for role, suffix in _BATTERY_TOWER_METRIC_SUFFIXES.items():
        name = f"{prefix}_{suffix}"
        if runtime.catalog.exists(name):
            names[role] = name
    if include_device_wide:
        names.update(
            {
                role: name
                for role, name in _BATTERY_DEVICE_METRIC_NAMES.items()
                if runtime.catalog.exists(name)
            }
        )
    return names


DEVICE_CARD_METRIC_NAMES = (
    "inverter_state",
    "battery_status2",
    "battery_placeholder_0_status2",
    "battery_soc_target",
    "power_mng_bat_next_calib_date",
    "heat_sink_temperature",  # inverter-side actual temperature, not the sink_temp power-reduction target
    "battery_temperature",  # battery pack temperature, distinct from the inverter's heat_sink_temperature
    "battery_cycles",  # battery.cycles - aggregate pack charge-cycle counter
    # Second tower's own readings: without them the battery_placeholder_0 card had nothing but
    # shared values to show and repeated the first tower's numbers.
    "battery_placeholder_0_soc",
    "battery_placeholder_0_temperature",
)
# Live readings of the optional second tower. A single-tower device may leave these registers
# unanswered, and an unanswered periodic/refresh READ drops the whole TCP connection, so they are
# polled only after the module-serial topology has confirmed the tower (see second_tower_confirmed).
SECOND_TOWER_METRIC_NAMES = (
    "battery_placeholder_0_status2",
    "battery_placeholder_0_soc",
    "battery_placeholder_0_temperature",
)
_SECOND_TOWER_PREFIX = "battery_placeholder_0"


def second_tower_confirmed(runtime, device_id: str) -> bool:
    """True once the stabilized module-serial topology shows a second battery tower.

    Same decision the dashboard uses (trusted last-known-good report, never a raw mid-scan read), so
    a transient or incomplete inventory cannot activate second-tower polling.
    """
    report = _battery_module_report(runtime, device_id, _SECOND_TOWER_PREFIX)
    return _battery_tower_present(
        runtime, device_id, _SECOND_TOWER_PREFIX, report["populated_module_slots"]
    )


def _energy_flow_view(readings: EnergyReadings | None) -> dict:
    """Maps the cache-only, sign-normalized EnergyReadings onto the dashboard projection.

    Always a five-key object (falls back to absent_readings()), independent of dispatch/Energy
    Manager state, so a device with no cache simply yields all-null/all-stale sub-fields.
    """
    source = readings if readings is not None else absent_readings()
    return {
        field_name: {
            "value": reading.value,
            "stale": reading.stale,
            "age_seconds": reading.age_seconds,
        }
        for field_name, reading in (
            ("pv_power_w", source.pv_power_w),
            ("grid_power_w", source.grid_power_w),
            ("house_load_w", source.house_load_w),
            ("battery_power_w", source.battery_power_w),
            ("battery_soc_percent", source.battery_soc_percent),
        )
    }


@router.get("/devices")
def devices(request: Request) -> dict:
    require_admin(request)
    runtime = request.app.state.runtime
    devices_now = tuple(
        runtime.devices.values()
    )  # one reference read: the dict is swapped, never mutated
    result = []
    # Card values are independent of the Prometheus export selection.
    preferred = (
        "solar_a_power",
        "solar_b_power",
        "household_load_power",
        "grid_power",
        "battery_soc",
        "ac_power",
    )
    selected = [name for name in preferred if runtime.catalog.exists(name)]
    selected += [
        name for name in DEVICE_CARD_METRIC_NAMES if runtime.catalog.exists(name)
    ]
    for item in devices_now:
        status = runtime.gateway.device_status(item.device_id)
        readings = []
        for name in selected:
            reading = runtime.gateway.cached_reading(item.device_id, name)
            if reading is None:
                continue
            value, freshness = reading
            entry = runtime.catalog.object_entry(name)
            label = _device_card_label(runtime.catalog, name, value)
            metric = {
                "name": name,
                "value": value,
                "unit": entry.unit,
                "stale": freshness is CacheFreshness.GRACE,
            }
            if label is not None:
                metric["label"] = label
            readings.append(metric)
        # One entry per physically present tower, each carrying its OWN metric names. The client used
        # to read battery_soc/battery_temperature for every tower, which made two towers show
        # identical values; the mapping is resolved here because only the server knows which
        # per-tower names the catalog actually has (battery_placeholder_0_cycles, for one, has none).
        # Vendor register names are fine on this admin-only surface; /api/v1 keeps its business
        # semantics and never sees them.
        batteries = []
        for prefix in _BATTERY_TOWER_PREFIXES:
            # Trusted (last-known-good) report FIRST, tower-presence decision from ITS populated
            # slots - never from the current poll's raw read - so a transiently missing/incomplete
            # serial cannot make an established tower (especially the second one) vanish.
            module_report = _battery_module_report(runtime, item.device_id, prefix)
            if not _battery_tower_present(
                runtime, item.device_id, prefix, module_report["populated_module_slots"]
            ):
                continue
            tower = {
                "id": prefix,
                "title": f"Battery {len(batteries) + 1}",
                "metrics": _battery_metric_names(
                    runtime, prefix, include_device_wide=not batteries
                ),
            }
            tower.update(module_report)
            batteries.append(tower)
        reported = runtime.gateway.reported_name(item.device_id)
        # Read-only, cache-only, dispatch-independent: present for every device unconditionally, so
        # the dashboard flow graphic works with write support/dispatch/Energy Manager disabled.
        flow = (
            runtime.energy_readings.readings(item.device_id)
            if runtime.energy_readings
            else None
        )
        result.append(
            {
                "id": item.device_id,
                "name": item.display_name or reported or item.device_id,
                "name_is_fallback": not (item.display_name or reported),
                "serial_number": runtime.gateway.reported_serial(item.device_id),
                "status": status.state.value,
                "host": item.host,
                "port": item.port,
                "last_success_at": (
                    status.last_success_at.isoformat()
                    if status.last_success_at
                    else None
                ),
                "queue_length": status.queue_length,
                "metrics": readings,
                "batteries": batteries,
                "energy_flow": _energy_flow_view(flow),
            }
        )
    return {"devices": result, "tsdb": _tsdb_view(runtime)}


def _parameter_view(request: Request) -> dict:
    runtime = request.app.state.runtime
    store = _store(request)
    entries = list(runtime.catalog.entries())
    exposed = _exposed_names(runtime, store)
    write = store.get("write_names") or []
    return {
        # help_text is "" for every parameter whose meaning the catalog does not document; the GUI
        # then renders no help line instead of an empty one.
        "available": [
            {
                "name": entry.name,
                "description": entry.description,
                "unit": entry.unit,
                "help_text": entry.help_text,
                "writable": entry.name in request.app.state.default_write_entries,
                "exportable": is_numeric(entry.value_type),
            }
            for entry in entries
        ],
        "exposed_names": list(exposed),
        "write_names": list(write),
    }


@router.get("/parameters")
def get_parameters(request: Request) -> dict:
    require_admin(request)
    return _parameter_view(request)


def _dispatch_in_use(request: Request) -> bool:
    runtime = request.app.state.runtime
    energy = runtime.energy
    if energy is not None and any(
        energy.mode(device_id) is not EnergyMode.OFF
        for device_id in tuple(runtime.devices)
    ):
        return True
    dispatch_store = getattr(request.app.state, "dispatch_store", None)
    if dispatch_store is None:
        return False
    return any(
        record.state is not DispatchState.IDLE
        or record.restore_required
        or record.intent is not None
        for record in dispatch_store.all()
    )


@router.put("/parameters")
def put_parameters(body: ParameterSelection, request: Request) -> dict:
    session = require_admin(request, mutation=True)
    runtime = request.app.state.runtime
    allowed = {
        entry.name
        for entry in runtime.catalog.entries()
        if is_numeric(entry.value_type)
    }
    write_allowed = set(request.app.state.default_write_entries)
    if (
        len(set(body.exposed_names)) != len(body.exposed_names)
        or set(body.exposed_names) - allowed
    ):
        raise HTTPException(400, "Invalid exposed metrics")
    if (
        len(set(body.write_names)) != len(body.write_names)
        or set(body.write_names) - write_allowed
    ):
        raise HTTPException(400, "Invalid writable metrics")
    store = _store(request)
    before = list(_exposed_names(runtime, store))
    selection_changed = list(body.exposed_names) != before

    def _mutate(current: list[str]) -> list[str]:
        # The in-use check and the revoke must not be separated by another save: both run inside
        # the shared lock update_write_names() holds, same as before this was factored out (M8).
        if session is None and set(body.write_names) - set(current):
            # Widening the write allowlist is a privilege change: cookie session only.
            raise HTTPException(403, "Administration session required")
        revoked = set(RctDispatchGateway.REQUIRED_WRITES) & set(current) - set(
            body.write_names
        )
        if revoked and _dispatch_in_use(request):
            # A restore writes these registers; revoking them now would leave it rejected forever.
            raise HTTPException(
                409,
                "Required dispatch writes cannot be revoked while a device is not switched off or is dispatching",
            )
        return body.write_names

    def _after(new_write_names: list[str]) -> None:
        if runtime.exporter is not None:
            runtime.exporter.set_exposed(body.exposed_names)

    # A selection change is a live transition: hold _LIVE_CHANGE_LOCK across the 409 re-check,
    # the persist (update_write_names stores exposed_names), and the apply (_apply_exposed_names ->
    # _reconfigure_devices), so a request that will hit a busy transition is refused before it
    # persists. The lock is the same OUTER lock put_settings uses; its busy rejection in
    # _apply_exposed_names runs with rollback=False, which is exactly why no persistence may happen
    # on a busy transition. Non-changing saves skip the lock.
    with contextlib.ExitStack() as _live_change:
        if selection_changed:
            _live_change.enter_context(_LIVE_CHANGE_LOCK)
            # Refused before anything is persisted: a transition that outlived its wait is still
            # running. This is the early 409, now re-checked under the serialization lock.
            if _transition_running(request):
                raise HTTPException(409, _BUSY_DETAIL)
        update_write_names(
            store,
            runtime.gateway,
            request.app.state.build_write_allowlist,
            _mutate,
            extra={"exposed_names": body.exposed_names},
            after_persist=_after,
        )
        if selection_changed:
            _apply_exposed_names(request, before)
    return _parameter_view(request)


def _apply_exposed_names(request: Request, before: list[str]) -> None:
    """Rebuild the collection graph so the saved metric selection is the one being collected.

    The rebuild reads the persisted selection. If the old graph is still the live one afterwards
    (build or restore refused), the previous selection is saved back so store and collector agree.
    """
    runtime = request.app.state.runtime
    settings = request.app.state.admin_desired_settings
    with _RECONFIGURE_LOCK:
        try:
            _reconfigure_devices(request, settings, settings)
        except _ReconfigureFailed as exc:
            if exc.rollback:
                with _PARAMETERS_LOCK:
                    _store(request).put("exposed_names", before)
                    if runtime.exporter is not None:
                        runtime.exporter.set_exposed(before)
            raise


@router.get("/tokens")
def get_tokens(request: Request) -> dict:
    require_admin(request, sensitive=True, session_only=True)
    return {"tokens": _store(request).list_tokens()}


@router.post("/tokens", status_code=201)
def post_token(body: TokenCreate, request: Request) -> dict:
    require_admin(request, mutation=True, session_only=True)
    try:
        record, token = _store(request).create_token(
            body.name, body.role, body.expires_at
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**record, "token": token}


@router.delete("/tokens/{token_id}")
def delete_token(token_id: str, request: Request) -> dict:
    require_admin(request, mutation=True, session_only=True)
    if not _store(request).delete_token(token_id):
        raise HTTPException(404, "Token not found")
    return {"deleted": True}


# Server-side allowlist of widget ids the browser may place on the dashboard; the layout is admin-UI
# config (not device configuration), so it is stored under its own key, never through PUT /settings.
_DASHBOARD_WIDGETS = frozenset(
    {
        "device-count",
        "connected-count",
        "metric-count",
        "pv-power",
        "house-power",
        "grid-power",
        "battery-soc",
        "days-to-calibration",
        "tsdb-status",
        "devices",
        "spot-price-current",
        "spot-market-status",
        "spot-price-curve",
        "solar-forecast-status",
        "pv-forecast-curve",
        "charging-schedule",
        "battery-usable-kwh",
        "battery-remaining-percent",
        "battery-cycles",
        "battery-last-calibration",
        "battery-commissioning-date",
        "battery-degradation",
    }
)
_DASHBOARD_LAYOUT_KEY = "dashboard_layout"
# The dashboard grid's vertical bound in rows, mirroring MAX_Y in dashboard.js. A widget must fit
# within it (y + h <= limit): a layout that cannot be resolved below this bound is rejected here,
# exactly as the client's overlap resolver refuses to push a box past it.
_DASHBOARD_MAX_Y = 1000


class DashboardWidgetLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    x: int = Field(ge=0, le=11)
    y: int = Field(ge=0, le=_DASHBOARD_MAX_Y)
    w: int = Field(ge=1, le=12)
    h: int = Field(ge=1, le=100)
    visible: bool = True


class DashboardLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, le=1)
    widgets: list[DashboardWidgetLayout] = Field(max_length=32)


def _validate_dashboard_layout(body: DashboardLayout) -> None:
    ids = [widget.id for widget in body.widgets]
    unknown = sorted(set(ids) - _DASHBOARD_WIDGETS)
    if unknown:
        raise HTTPException(422, f"Unknown widget id(s): {', '.join(unknown)}")
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Duplicate widget id")
    for widget in body.widgets:
        if widget.x + widget.w > 12:
            raise HTTPException(
                422, f"Widget {widget.id} extends past the 12-column grid"
            )
        if widget.y + widget.h > _DASHBOARD_MAX_Y:
            raise HTTPException(
                422,
                f"Widget {widget.id} extends past the {_DASHBOARD_MAX_Y}-row vertical bound",
            )


@router.get("/dashboard-layout")
def get_dashboard_layout(request: Request) -> dict:
    require_admin(request)
    stored = _store(request).get(_DASHBOARD_LAYOUT_KEY)
    return {"layout": stored}


@router.put("/dashboard-layout")
def put_dashboard_layout(body: DashboardLayout, request: Request) -> dict:
    require_admin(request, mutation=True)
    _validate_dashboard_layout(body)
    layout = body.model_dump()
    _store(request).put(_DASHBOARD_LAYOUT_KEY, layout)
    return {"layout": layout}


@router.delete("/dashboard-layout")
def delete_dashboard_layout(request: Request) -> dict:
    require_admin(request, mutation=True)
    _store(request).delete(_DASHBOARD_LAYOUT_KEY)
    return {"layout": None}
