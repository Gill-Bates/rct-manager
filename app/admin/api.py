#!/usr/bin/env python3
#
# app/admin/api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""JSON API for the local browser administration interface."""

import asyncio
import hmac
import logging
import re
import threading
import time
from datetime import UTC, datetime
from secrets import token_urlsafe
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.admin.store import SESSION_SECONDS
from app.admin.updates import check_for_updates
from app.cache import CacheFreshness
from app.catalog.base import is_numeric
from app.config import DISPLAY_NAME_MAX, Settings
from app.dispatch.controller import ReconfigurationRejected
from app.dispatch.models import DispatchState
from app.energy.models import EnergyMode
from app.energy.readings import EnergyReadings, absent_readings
from app.errors import ConfigError, ReconfigurationBuildError
from app.gateway.rct_dispatch import RctDispatchGateway
from app.security.dependencies import source_address

router = APIRouter(prefix="/admin/api", include_in_schema=False)
_COOKIE = "rct_admin_session"
_CSRF_COOKIE = "rct_admin_csrf"

# Community-observed bit meanings for battery.status2 (OID 0xDE3D20D, t_int32). RCT's own protocol
# document (docs/reference) has no bit table for this object; these flags come from user-reported
# RCT community observations (ioBroker RCT adapter) and are not an official vendor specification.
_BATTERY_STATUS2_BITS: dict[int, str] = {
    0x001: "Disconnected",
    0x002: "Synchronizing",
    0x004: "Connecting",
    0x008: "Calibration charging",
    0x100: "Balancing active",
    0x200: "Low voltage",
    0x400: "Calibration discharging",
    0x800: "Balancing required",
}


def _battery_status2_label(value: int) -> str:
    """Best-effort, bitwise decoding; an unrecognized bit falls back to the raw code (see above)."""
    if value == 0:
        return "Normal"
    if value < 0:
        return f"Status code {value}"
    labels = [label for bit, label in _BATTERY_STATUS2_BITS.items() if value & bit]
    known_mask = sum(bit for bit in _BATTERY_STATUS2_BITS if value & bit)
    if "Balancing active" in labels and "Balancing required" in labels:
        labels.remove("Balancing required")  # active implies required; showing both is redundant
    if not labels:
        return f"Status code {value}"
    if value & ~known_mask:  # bits outside the known table: keep the code visible, not hidden
        labels.append(f"code {value}")
    return " · ".join(labels)


def _humanize_enum_label(label: str) -> str:
    """``inverter_state`` enum labels are lower snake_case; this is purely cosmetic formatting."""
    return label.replace("_", " ").capitalize()


# The push exporter task reads these from runtime.settings only when it (re)starts, so a saved
# change needs the exporter restarted (not a full process restart) to take effect.
_EXPORT_RESTART_KEYS = frozenset({
    "db_type", "metrics_export_enabled", "metrics_export_interval_seconds", "influxdb_hostname", "influxdb_port",
    "influxdb_tls_enabled", "influxdb_verify_tls", "influxdb_measurement_name",
    "influxdb_allow_plaintext_credentials", "influxdb_organization", "influxdb_bucket", "influxdb_token",
    "questdb_hostname", "questdb_port", "questdb_tls_enabled", "questdb_verify_tls",
    "questdb_measurement_name", "questdb_allow_plaintext_credentials", "questdb_username", "questdb_password",
    "questdb_downsampling", "questdb_raw_retention_days", "questdb_retention_days",
})
_EDITABLE = frozenset({
    "auth_required", "docs_public", "enable_metrics_endpoint", "enable_write_support",
    "devices", "bind_address", "bind_port", "log_level", "behind_reverse_proxy",
    "trusted_proxies", "forwarded_header",
    "metrics_require_token", "metrics_trusted_sources",
    "metrics_rate_limit_requests", "metrics_rate_limit_window_seconds",
}) | _EXPORT_RESTART_KEYS
# The device/endpoint/scheduling graph is rebuilt in place by reconfigure_devices(), so a saved
# devices list needs that targeted rebuild (not a full process restart) to take effect.
_DEVICE_LIVE_KEYS = frozenset({"devices"})
_LIVE = frozenset({
    "auth_required", "docs_public", "enable_metrics_endpoint", "behind_reverse_proxy",
    "metrics_require_token", "metrics_trusted_sources", "metrics_rate_limit_requests",
    "metrics_rate_limit_window_seconds", "enable_write_support",
}) | _EXPORT_RESTART_KEYS | _DEVICE_LIVE_KEYS
_SECRET_EDITABLE = frozenset({"influxdb_token", "questdb_password"})
# Authentication and proxy-trust settings: changing them with a PAT would let a leaked token
# switch authentication off or widen who is trusted, so they need the cookie session.
_SESSION_ONLY_SETTINGS = frozenset({
    "auth_required", "trusted_proxies", "bind_address", "behind_reverse_proxy", "forwarded_header",
    "metrics_require_token", "metrics_trusted_sources", "enable_write_support", "devices", "docs_public",
    "bind_port", "log_level",
})
# Export destination and credential keys: a PAT that could change them could send metrics (and the
# stored credentials) to an attacker-controlled host. Unlike _SESSION_ONLY_SETTINGS they stay visible
# to a PAT on read. The retention days are here too: a PAT must not be able to shorten them and
# thereby make the TSDB drop history.
_EXPORT_TARGET_SETTINGS = frozenset({
    "db_type", "influxdb_hostname", "influxdb_port", "influxdb_tls_enabled", "influxdb_verify_tls",
    "influxdb_allow_plaintext_credentials", "influxdb_organization", "influxdb_bucket", "influxdb_token",
    "questdb_hostname", "questdb_port", "questdb_tls_enabled", "questdb_verify_tls",
    "questdb_allow_plaintext_credentials", "questdb_username", "questdb_password",
    "questdb_retention_days", "questdb_raw_retention_days",
})
_SETTINGS_LOCK = threading.Lock()
# Serializes live device reconfigurations without holding _SETTINGS_LOCK while they run.
_RECONFIGURE_LOCK = threading.Lock()
_PARAMETERS_LOCK = threading.Lock()
_WRITE_DEFAULTS_APPLIED_KEY = "write_defaults_applied"
_RECONFIGURE_TIMEOUT_SECONDS = 120.0
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
    Energy Manager's add-only arming widen, which used to run under two independent locks
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
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if forwarded in ("http", "https") and request.app.state.security.client_ip.is_trusted_peer(peer):
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
    if not _same_origin(request) or not cookie or not header or not hmac.compare_digest(cookie, header):
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
    if privileged and entry.role != "read/write":
        raise HTTPException(403, "Read/write token required")
    return True


def require_admin(
    request: Request,
    *,
    mutation: bool = False,
    allow_password_change: bool = False,
    session_only: bool = False,
    sensitive: bool = False,
) -> dict | None:
    """Return the cookie session, or None for a valid PAT.

    ``sensitive`` marks a read that exposes settings or token metadata: it needs read/write like a mutation.

    ``session_only`` refuses a PAT outright: security-relevant changes (tokens, trust and
    authentication settings) must not be reachable with a leaked automation token.
    """
    bearer = not allow_password_change and "authorization" in request.headers and not request.cookies.get(_COOKIE)
    if bearer and session_only:
        raise HTTPException(403, "Administration session required")
    if bearer and _bearer_admin(request, mutation or sensitive):
        return None
    session = admin_session(request)
    if session is None:
        raise HTTPException(401, "Login required")
    ctx = request.app.state.security
    ctx.limiter.check_request("admin:" + source_address(request, ctx))
    if mutation:
        _csrf(request, session)
    if session["must_change_password"] and not allow_password_change:
        raise HTTPException(403, "Password change required")
    return session


def _secure_cookies(request: Request) -> bool:
    return bool(request.app.state.runtime.settings.behind_reverse_proxy or _public_scheme(request) == "https")


def _set_cookies(response: Response, request: Request, token: str, csrf: str) -> None:
    secure = _secure_cookies(request)
    response.set_cookie(_COOKIE, token, httponly=True, secure=secure, samesite="lax", max_age=SESSION_SECONDS, path="/")
    response.set_cookie(_CSRF_COOKIE, csrf, httponly=True, secure=secure, samesite="lax", max_age=SESSION_SECONDS, path="/")


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
        response.set_cookie(_CSRF_COOKIE, csrf, httponly=True, secure=_secure_cookies(request), samesite="lax", path="/")
        return {"authenticated": False, "must_change_password": False, "csrf_token": csrf}
    return {"authenticated": True, "must_change_password": state["must_change_password"], "csrf_token": csrf}


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
    return {"authenticated": True, "must_change_password": state["must_change_password"], "csrf_token": csrf}


@router.post("/change-password")
def change_password(body: PasswordChange, request: Request, response: Response) -> dict:
    require_admin(request, mutation=True, allow_password_change=True)
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
    return {"authenticated": True, "must_change_password": False, "csrf_token": csrf}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    require_admin(request, mutation=True, allow_password_change=True)
    _store(request).delete_session(request.cookies.get(_COOKIE))
    response.delete_cookie(_COOKIE, path="/")
    response.delete_cookie(_CSRF_COOKIE, path="/")
    return {"authenticated": False}


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
    result = {key: value for key, value in _settings_view(settings).items() if not key.endswith("_configured")}
    for key in _SECRET_EDITABLE:
        secret = getattr(settings, key)
        if secret is not None:
            result[key] = secret.get_secret_value()
    return result


def _pending_restart(request: Request, session: dict | None) -> list[str]:
    """Settings whose saved value differs from the one the running server uses."""
    desired = _settings_persisted(request.app.state.admin_desired_settings)
    active = _settings_persisted(request.app.state.runtime.settings)
    pending = sorted(key for key in desired if key not in _LIVE and desired[key] != active.get(key))
    if session is None:  # same visibility rule as _pat_safe_view: a PAT must not learn session-only key names
        pending = [key for key in pending if key not in _SESSION_ONLY_SETTINGS]
    return pending


_NAME_MIGRATION = "devices_display_name_repaired"


def _is_address_name(device) -> bool:
    return device.display_name in {f"{device.host}:{device.port}", device.host}


def repair_device_names(store, desired: Settings) -> Settings:
    """Older saves stored host:port as display_name, which then beat the name the inverter reports.

    Runs once per database, at startup (a GET must never persist), and drops only those
    address-shaped names, so an operator-chosen name stays. Returns the settings to run with.
    """
    if store.get(_NAME_MIGRATION):
        return desired
    if any(_is_address_name(device) for device in desired.devices):
        repaired = []
        for device in desired.devices:
            entry = device.model_dump(mode="json")
            if _is_address_name(device):
                entry["display_name"] = None
            repaired.append(entry)
        try:
            desired = store.merge_operator_settings(desired, {"devices": repaired})
        except ValidationError:
            log.warning("Device name repair skipped: the stored settings did not validate")
            return desired
    store.put(_NAME_MIGRATION, True)
    return desired


@router.get("/settings")
def get_settings(request: Request) -> dict:
    session = require_admin(request, sensitive=True)
    return {"settings": _pat_safe_view(request.app.state.admin_desired_settings, session),
            "restart_required": _pending_restart(request, session), "live": sorted(_LIVE)}


# Mirrors app.config._DEVICE_ID (DeviceEntry._check_id): validated here too so a bad device_id
# gets a field-specific 400 instead of surfacing as the generic "Invalid setting" from the
# downstream model validation (PY-01). Keep this pattern in step with app.config._DEVICE_ID.
_DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# This is a plausibility check for an address-shaped value, not DNS or full IP validation.
_HOST = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*|\[[0-9A-Fa-f:.]+\]|[0-9A-Fa-f:.]+)$"
)


def _network_id(item: dict[str, Any]) -> int | None:
    """Empty input means "directly attached"; the stored form must match the parsed-config one."""
    value = item.get("network_id")
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**32 - 1:
        raise HTTPException(400, "The network id must be a whole number")
    return value


def _normalize_host(host: str) -> str:
    """Brackets are URL syntax, not part of the host: the transport needs the bare address."""
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _display_name(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > DISPLAY_NAME_MAX or any(not c.isprintable() for c in value):
        raise HTTPException(400, f"The display name must be printable text of at most {DISPLAY_NAME_MAX} characters")
    return value


def _normalize_devices(raw: Any) -> list[dict[str, Any]]:
    """Complete a host+port list: keep sent ids and names, assign stable ids to new devices."""
    if not isinstance(raw, list) or len(raw) > 32:
        raise HTTPException(400, "Devices must be a list of at most 32 inverters")
    seen_addresses: set[tuple[str, int, int | None]] = set()
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(400, "Invalid inverter entry")
        host = str(item.get("host") or "").strip()
        port = item.get("port", 8899)
        if not host or not _HOST.fullmatch(host):
            raise HTTPException(400, "Enter a plain IP address or host name, without scheme or path")
        host = _normalize_host(host)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise HTTPException(400, "The port must be between 1 and 65535")
        network_id = _network_id(item)
        address = (host.lower(), port, network_id)  # slaves share the master's endpoint
        if address in seen_addresses:
            raise HTTPException(400, f"The inverter address {host}:{port} is listed twice")
        seen_addresses.add(address)
        device_id = item.get("device_id") or None
        if device_id is not None and not isinstance(device_id, str):
            raise HTTPException(400, "The device id must be text")
        if device_id is not None and not _DEVICE_ID.fullmatch(device_id):
            raise HTTPException(
                400,
                "The device id must start with a letter or digit and use only letters, digits, "
                "'_', '.' or '-' (at most 64 characters)",
            )
        entry = {"host": host, "port": port, "device_id": device_id,
                 "display_name": _display_name(item.get("display_name")), "network_id": network_id}
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


def _reconfigure_devices(request: Request) -> None:
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
    except ReconfigurationRejected as exc:
        raise _ReconfigureFailed(
            409,
            f"Device {exc.device_id!r} has an active battery dispatch that could not be safely "
            "restored for this change; cancel or wait out that operation and try again.",
            rollback=True,
        ) from exc
    except ReconfigurationBuildError as exc:
        log.error("The new device list could not be built; the previous one keeps running: %s", exc)
        raise _ReconfigureFailed(
            409, "The new device list could not be applied; the previous configuration is still active.",
            rollback=True,
        ) from exc
    except TimeoutError as exc:
        log.error("Reconfiguring devices did not finish within %.0f s", _RECONFIGURE_TIMEOUT_SECONDS)
        raise _ReconfigureFailed(
            504, "Applying the device list is still in progress; check the device status before saving again.",
            rollback=False,
        ) from exc
    except Exception as exc:
        log.exception("Reconfiguring devices after a settings change failed")
        raise _ReconfigureFailed(
            500, "Applying the device list failed; devices may be offline until the service is restarted.",
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
        _run_on_loop(request, "restart_export", 5.0)
    except Exception:
        log.exception("Restarting the metrics export after a settings change failed")


def _changed_session_only(request: Request, body: dict[str, Any]) -> set[str]:
    """Session-only keys whose requested value differs from the current one.

    Re-sending an unchanged value (a client echoing the whole settings dict) is not a change.
    """
    current = _settings_view(request.app.state.admin_desired_settings)
    changed = {key for key in body if key in _SESSION_ONLY_SETTINGS and current.get(key) != body[key]}
    # Secrets are never in the view, so any non-null value counts as a change.
    changed |= {key for key in body if key in _SECRET_EDITABLE and body[key] is not None}
    changed |= {
        key for key in body
        if key in _EXPORT_TARGET_SETTINGS - _SECRET_EDITABLE and current.get(key) != body[key]
    }
    # Switching the scrape endpoint on is a privilege change only where it would serve metrics
    # without a token (token requirement off, or trusted scrape sources configured). Switching it
    # off, or on behind the token requirement, stays available to a PAT.
    if body.get("enable_metrics_endpoint") is True and not current.get("enable_metrics_endpoint"):
        require_token = body.get("metrics_require_token", current.get("metrics_require_token"))
        if not require_token or current.get("metrics_trusted_sources"):
            changed.add("enable_metrics_endpoint")
    return changed


@router.put("/settings")
def put_settings(body: dict[str, Any], request: Request) -> dict:
    session = require_admin(request, mutation=True)
    if not body or set(body) - _EDITABLE:
        raise HTTPException(400, "Unknown or empty setting")
    body = {k: v for k, v in body.items() if not (k in _SECRET_EDITABLE and v == "")}  # "" keeps the secret
    if not body:
        raise HTTPException(400, "Unknown or empty setting")
    if "devices" in body:
        body["devices"] = _normalize_devices(body["devices"])
    if session is None and _changed_session_only(request, body):
        raise HTTPException(403, "Administration session required")
    runtime = request.app.state.runtime
    with _SETTINGS_LOCK:  # keeps concurrent autosaves from publishing an older merge last
        previous = request.app.state.admin_desired_settings
        try:
            updated = _store(request).merge_operator_settings(previous, body)
        except ValidationError as exc:
            raise HTTPException(400, "Invalid setting") from exc
        request.app.state.admin_desired_settings = updated
        changed_export_keys = {
            key for key in _EXPORT_RESTART_KEYS & set(body)
            if getattr(previous, key) != getattr(updated, key)
        }
        devices_changed = "devices" in body and previous.devices != updated.devices
        runtime.settings = runtime.settings.model_copy(
            update={key: getattr(updated, key) for key in _LIVE}
        )
        request.app.state.security.tokens.set_auth_required(updated.auth_required)
        # Same lock, same moment as runtime.settings: the write routers refuse from here on.
        request.app.state.security.write_enabled = updated.enable_write_support
        if {"metrics_rate_limit_requests", "metrics_rate_limit_window_seconds"} & set(body):
            request.app.state.security.limiter.set_scrape_limit(
                updated.metrics_rate_limit_requests, updated.metrics_rate_limit_window_seconds
            )
    write_warnings: dict[str, Any] = {}
    if updated.enable_write_support and not previous.enable_write_support:
        try:
            _enable_write_support(request)
        except ConfigError as exc:
            _revert_write_support(request)
            raise HTTPException(409, "The write allowlist could not be loaded; write access stays off.") from exc
    elif previous.enable_write_support and not updated.enable_write_support:
        pending = _disable_write_support(request)
        if pending:
            write_warnings = {"write_restore_pending": pending}
    # The slow live reloads run outside _SETTINGS_LOCK so they cannot stall other settings requests.
    if changed_export_keys:
        _restart_export(request)
    if devices_changed:
        with _RECONFIGURE_LOCK:
            try:
                _reconfigure_devices(request)
            except _ReconfigureFailed as exc:
                if exc.rollback:
                    _revert_devices(request, updated, previous)
                raise
    return {"settings": _pat_safe_view(updated, session), "restart_required": _pending_restart(request, session),
            "live": sorted(_LIVE), **write_warnings}


def _run_on_loop(request: Request, name: str, timeout: float) -> Any:
    """Run the app-state coroutine function ``name`` on the event loop from this worker thread."""
    func = getattr(request.app.state, name, None)
    loop = getattr(request.app.state, "loop", None)
    if func is None or loop is None:
        return None
    return asyncio.run_coroutine_threadsafe(func(), loop).result(timeout=timeout)


def _enable_write_support(request: Request) -> None:
    """Make a just-enabled write switch effective: allowlist, default approvals, dispatch backing."""
    # Late import: app_factory imports this module.
    from app.api.app_factory import (
        _load_default_write_entries,
    )

    state = request.app.state
    # Reloaded because it is skipped at boot while writes are off; a broken file refuses the enable.
    state.default_write_entries = _load_default_write_entries(state.runtime.settings, state.runtime.catalog)
    _approve_default_writes(request)
    with _PARAMETERS_LOCK:
        names = list(_store(request).get("write_names") or [])
        state.runtime.gateway.set_allowlist(state.build_write_allowlist(names))
    try:
        _run_on_loop(request, "enable_dispatch", _RECONFIGURE_TIMEOUT_SECONDS)
    except Exception:
        # Writes would be accepted by the router but have no dispatch to run on: fail closed.
        log.exception("Building battery dispatch for the enabled write support failed")
        _revert_write_support(request)
        raise HTTPException(500, "Battery dispatch could not be started; write access stays off.") from None


def _disable_write_support(request: Request) -> list[str]:
    """Hand the devices back to automatic operation after the switch went off; returns pending ids."""
    try:
        return _run_on_loop(request, "disable_dispatch", _RECONFIGURE_TIMEOUT_SECONDS) or []
    except Exception:
        log.exception("Restoring the inverters after disabling write access failed")
        return list(request.app.state.runtime.devices)


def _revert_write_support(request: Request) -> None:
    """Switch write access back off after the live enable failed (it was off before)."""
    state = request.app.state
    with _SETTINGS_LOCK:
        reverted = _store(request).merge_operator_settings(
            state.admin_desired_settings, {"enable_write_support": False}
        )
        state.admin_desired_settings = reverted
        state.runtime.settings = state.runtime.settings.model_copy(
            update={"enable_write_support": False}
        )
        state.security.write_enabled = False


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
        wanted = [n for n in RctDispatchGateway.REQUIRED_WRITES if n in state.default_write_entries]
        merged = current + [name for name in wanted if name not in current]
        allowlist = state.build_write_allowlist(merged)  # build first: persist only what works
        store.put_many({"write_names": merged, _WRITE_DEFAULTS_APPLIED_KEY: True})
        state.runtime.gateway.set_allowlist(allowlist)


def _revert_devices(request: Request, updated: Settings, previous: Settings) -> None:
    """Put the old device list back after the live graph rejected the new one.

    Only the devices field is rolled back so the admin store, admin_desired_settings and
    runtime.settings agree with what the gateway has loaded; other fields of the same request stay
    applied. A newer save that already replaced the list is left alone.
    """
    runtime = request.app.state.runtime
    with _SETTINGS_LOCK:
        current = request.app.state.admin_desired_settings
        if current.devices != updated.devices:
            return
        reverted = _store(request).merge_operator_settings(
            current, {"devices": [d.model_dump(mode="json") for d in previous.devices]}
        )
        request.app.state.admin_desired_settings = reverted
        runtime.settings = runtime.settings.model_copy(update={"devices": previous.devices})


def _exposed_names(runtime, store) -> list[str]:
    exposed = store.get("exposed_names")
    if exposed is None:
        exposed = runtime.settings.metrics_exposed_names or runtime.catalog.preselected()
    return exposed


def _tsdb_view(runtime) -> dict:
    """TSDB export status for the dashboard tile; additive, independent of the device list."""
    settings = runtime.settings
    stats = runtime.stats
    configured = settings.db_type is not None
    export_enabled = bool(stats.export_enabled) if stats else False
    last_success_unix = stats.export_last_success_unix if stats else None
    last_success_at = (
        datetime.fromtimestamp(last_success_unix, tz=UTC).isoformat() if last_success_unix is not None else None
    )
    if not configured:
        healthy = None  # neutral: "healthy" has no meaning when no TSDB is configured
    elif not export_enabled:
        healthy = False  # configured but the export loop isn't running: nothing is being sent
    else:
        # Generous but simple freshness window: 3x the configured push interval absorbs ordinary
        # scheduling jitter and one retry backoff doubling (same "allow slack past the nominal
        # period" idea as the ttl + grace pattern in app/cache.py).
        healthy = (
            last_success_unix is not None
            and (time.time() - last_success_unix) <= 3 * settings.metrics_export_interval_seconds
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


# Prefixes of the per-tower module_sn string slots (app/catalog/objects.json). Each prefix is one
# physical battery tower that could share the inverter. The registry carries entries for both
# prefixes unconditionally, so catalog presence alone cannot tell whether a given tower actually
# exists on this device; _battery_tower_present answers that from readings.
_BATTERY_TOWER_PREFIXES = ("battery", "battery_placeholder_0")

# Two different limits that must not be conflated.
#
# RCT_MAX_MODULES_PER_TOWER is hardware: RCT documents the Power Battery / BMS V2 as 2 to 6 battery
# modules per tower - the 3.8 / 5.7 / 7.6 / 9.6 / 11.5 kWh variants correspond to 2 / 3 / 4 / 5 / 6
# modules, and a double-tower installation also allows up to 6 modules per tower.
#
# RCT_MODULE_SN_SLOTS is the protocol/catalog side: battery_module_sn_0 .. battery_module_sn_6 is an
# array with seven elements (indices 0-6). That is the size of the data structure and nothing else;
# "seven slots" does not mean "seven modules can be installed". Why the vendor sized the array at
# seven is not answered by any documentation available here, and no claim is made about it.
#
# The device has no module-count register, so the count is derived from the populated slots - but
# counting and hardware validation stay separate, see _battery_module_report.
RCT_MAX_MODULES_PER_TOWER = 6
RCT_MODULE_SN_SLOTS = 7
# One populated serial slot per tower belongs to the tower's base/top part, not to a battery module
# (observed: a 5-module tower reports six populated slots), so module_count is populated slots
# minus this. 6 modules + 1 = the 7 slots of the array.
RCT_NON_MODULE_SLOTS = 1

# Card roles that exist once per tower, with the catalog suffix that carries them.
_BATTERY_TOWER_METRIC_SUFFIXES = {"soc": "soc", "temperature": "temperature", "status": "status2"}
# Card roles with no per-tower equivalent in the catalog: battery_placeholder_0_cycles and
# battery_placeholder_0_soc_target do not exist, and power_mng_bat_next_calib_date belongs to the
# power manager, not to one tower. They are therefore reported once, for the first tower, rather
# than invented for the second - a per-tower name the device does not have cannot be read.
_BATTERY_DEVICE_METRIC_NAMES = {
    "cycles": "battery_cycles",
    "soc_target": "battery_soc_target",
    "next_calibration": "power_mng_bat_next_calib_date",
}
# A plain numeric/status reading alone does not prove the *placeholder* tower is real: a device
# answers a periodically-registered read with some value even for a register nothing is wired to
# (an unpopulated numeric register reads back as 0, not as "no reading"), so soc/temperature/
# cycles/status2 are only a presence signal for the primary tower, which every single-battery
# device has always had (keeps compatibility with registries that carry no module_sn slots at
# all). A placeholder tower is only real when it has at least one populated module serial — the
# one signal a device cannot answer for a slot nothing is plugged into.
_BATTERY_PRESENCE_SUFFIXES = ("soc", "temperature", "cycles", "status2")


def _battery_tower_present(runtime, device_id: str, prefix: str, trusted_module_slots: list[int]) -> bool:
    """`trusted_module_slots` must be the STABILIZED report's populated_module_slots (see
    _battery_module_report), never the current poll's raw _battery_populated_module_slots() - a
    transient missing/incomplete serial read must not make an established second tower vanish."""
    if trusted_module_slots:
        return True
    if prefix != "battery":
        return False  # a placeholder tower with no module serials is not installed
    for suffix in _BATTERY_PRESENCE_SUFFIXES:
        name = f"{prefix}_{suffix}"
        if runtime.catalog.exists(name) and runtime.gateway.cached_reading(device_id, name) is not None:
            return True
    return False


def _clean_module_serial(value: str) -> str:
    """Strip control characters (not just whitespace) that an unpopulated t_string register can
    decode to on real hardware - e.g. a slot full of non-NUL control bytes that str.strip() leaves
    untouched because strip() only removes whitespace by default. What is left is what a real
    serial number actually looks like: printable characters."""
    return re.sub(r"[\x00-\x1f\x7f]", "", value).strip()


# A slot's cached_reading() answer distinguishes three states, not two. cached_reading() returns
# None for a slot that has never been read (or whose cache entry expired) - that is UNKNOWN, not
# "empty". Only a slot that WAS read and decoded to a blank/garbage string is EMPTY. The periodic
# loop fills all seven module_sn slots over several poll cycles (see _battery_module_report's
# docstring), so a mid-scan snapshot has some slots at one of these states and the rest still
# UNKNOWN; conflating UNKNOWN with EMPTY is exactly what let a 3-of-6-read tower be accepted as a
# genuine 3-module tower before the rest had been read.
_SLOT_UNKNOWN = "unknown"
_SLOT_EMPTY = "empty"
_SLOT_POPULATED = "populated"


def _battery_module_slot_states(runtime, device_id: str, prefix: str) -> list[str]:
    """One of _SLOT_UNKNOWN/_SLOT_EMPTY/_SLOT_POPULATED per protocol slot (index = slot number). A
    slot the catalog does not carry at all can never resolve to anything else, so it is EMPTY
    rather than UNKNOWN - it must not block a registry without module_sn slots from ever being
    judged "complete"."""
    states = []
    for i in range(RCT_MODULE_SN_SLOTS):
        name = f"{prefix}_module_sn_{i}"
        if not runtime.catalog.exists(name):
            states.append(_SLOT_EMPTY)
            continue
        reading = runtime.gateway.cached_reading(device_id, name)
        if reading is None:
            # A register the device repeatedly leaves unanswered (e.g. slot 6 on a 6-module tower)
            # is never going to be cached; counting it UNKNOWN would hold "pending" forever.
            read_failed = getattr(runtime.gateway, "read_failed", None)
            unanswered = read_failed is not None and read_failed(device_id, name)
            states.append(_SLOT_EMPTY if unanswered else _SLOT_UNKNOWN)
            continue
        value = reading[0]
        if isinstance(value, str) and _clean_module_serial(value):
            states.append(_SLOT_POPULATED)
        else:
            states.append(_SLOT_EMPTY)
    return states


def _battery_populated_module_slots(states: list[str]) -> list[int]:
    """Indices of the slots classified POPULATED in a slot-state snapshot."""
    return [i for i, state in enumerate(states) if state == _SLOT_POPULATED]


def _raw_battery_module_report(states: list[str]) -> dict:
    """Pure classification of one read's slot-state snapshot - no memory of any previous read.

    Completeness gates everything else: while any slot is still UNKNOWN, the snapshot is a
    mid-scan read and no topology may be derived from it at all, no matter how clean the slots that
    HAVE been read look - a 3-module tower mid-scan looks identical to a genuine 3-module tower
    that finished scanning first. Only once every slot has been read (none UNKNOWN left) does the
    populated-slot pattern get interpreted: a contiguous run 0..n-1 with the rest empty is
    unambiguous and is trusted; anything else - a gap in the middle, or more populated slots than
    the documented hardware takes - is a data anomaly and not a taller tower.

    `module_count_status` tells the client which case it is looking at:
      "ok"      - complete snapshot, trusted count, render that many modules
      "pending" - either the snapshot is still incomplete (slots arrive over several poll cycles,
                  the normal startup state), or it is complete but genuinely empty
      "anomaly" - complete snapshot whose populated slots cannot describe a documented tower

    The internal "_complete" key drives `_stabilize_battery_module_report`'s last-known-good hold
    and is stripped before a report reaches a caller outside this module (see
    `_battery_module_report`).
    """
    complete = _SLOT_UNKNOWN not in states
    populated = _battery_populated_module_slots(states)
    if not complete:
        return {"module_count": None, "module_count_status": "pending",
                "populated_module_slots": populated, "_complete": False}
    if not populated:
        return {"module_count": None, "module_count_status": "pending",
                "populated_module_slots": [], "_complete": True}
    contiguous = populated == list(range(len(populated)))
    if contiguous and len(populated) - RCT_NON_MODULE_SLOTS <= RCT_MAX_MODULES_PER_TOWER:
        # A count of 1 is below the documented minimum of 2, but that is a legitimate complete
        # read (e.g. a tower stripped down to one module for service), not a mid-scan artifact -
        # the completeness gate above already ruled the mid-scan case out.
        return {"module_count": len(populated) - RCT_NON_MODULE_SLOTS, "module_count_status": "ok",
                "populated_module_slots": populated, "_complete": True}
    return {"module_count": None, "module_count_status": "anomaly",
            "populated_module_slots": populated, "_complete": True}


# How many consecutive reads of a *changed* classification are required before the reported count
# is allowed to replace the previously trusted one. The periodic loop refreshes the seven
# module_sn string slots over several read/refresh cycles (PeriodicManager.setup and
# RctGateway.refresh_stale_periodic in app/gateway/rct.py bound a single catch-up pass to
# REFRESH_MAX_PER_CYCLE stale entries every _REFRESH_CYCLE_SECONDS), and a cache entry for one slot
# can also simply expire (app/cache.py) between two polls while a sibling slot's does not. Both
# make cached_reading() answer differently for the same still-unchanged tower from one admin API
# call to the next. A single changed read is therefore not enough to flip the reported count; it
# must repeat before it is believed, while a sustained real change - an actual module added or
# removed - still takes effect rather than being frozen out forever.
_BATTERY_MODULE_STABILITY_READS = 2

_battery_module_lock = threading.Lock()
# Attribute name under which the stability state lives on runtime.gateway. Storing it there instead
# of in module-level state keyed by device id/prefix means a reconfigured or test-created gateway
# starts from a clean slate instead of inheriting another instance's history, and the state is
# garbage-collected along with the gateway it belongs to - no separate cleanup needed.
_BATTERY_MODULE_STATE_ATTR = "_battery_module_stability"


def _battery_report_signature(report: dict) -> tuple:
    return (report["module_count_status"], report["module_count"], tuple(report["populated_module_slots"]))


def _stabilize_battery_module_report(runtime, device_id: str, prefix: str, raw: dict) -> dict:
    """Holds the last-known-good topology against two things that must never flip it: a mid-scan
    (incomplete) snapshot, and a single changed-but-complete read that has not repeated yet.

    An incomplete `raw` (`raw["_complete"] is False`) can never become trusted and never even
    starts a candidate - the scan that produced it has not finished, so there is nothing yet to
    debounce. Only a COMPLETE snapshot may start or advance a candidate, and - same as before -
    it must repeat `_BATTERY_MODULE_STABILITY_READS` times before replacing the trusted topology,
    so a single flaky complete-looking read is not enough either.
    """
    key = (device_id, prefix)
    with _battery_module_lock:
        per_gateway = getattr(runtime.gateway, _BATTERY_MODULE_STATE_ATTR, None)
        if per_gateway is None:
            per_gateway = {}
            setattr(runtime.gateway, _BATTERY_MODULE_STATE_ATTR, per_gateway)
        state = per_gateway.get(key)
        if state is None:
            # Nothing established yet - even an incomplete read becomes the initial state, since
            # there is no better answer to show while the first scan is still in progress.
            per_gateway[key] = {"trusted": raw, "candidate": None, "candidate_count": 0}
            return raw
        trusted = state["trusted"]
        if not raw["_complete"]:
            # Mid-scan snapshot: hold last-known-good and drop any in-progress candidate, because
            # the scan behind that candidate has not finished either.
            state["candidate"] = None
            state["candidate_count"] = 0
            return trusted
        if _battery_report_signature(raw) == _battery_report_signature(trusted):
            state["candidate"] = None
            state["candidate_count"] = 0
            return trusted
        if (state["candidate"] is not None
                and _battery_report_signature(raw) == _battery_report_signature(state["candidate"])):
            state["candidate_count"] += 1
        else:
            state["candidate"] = raw
            state["candidate_count"] = 1
        if state["candidate_count"] < _BATTERY_MODULE_STABILITY_READS:
            return trusted
        state["trusted"] = raw
        state["candidate"] = None
        state["candidate_count"] = 0
        return raw


_PENDING_LOG_INTERVAL_SECONDS = 60.0
_pending_log_last: dict[tuple[str, str], float] = {}


def _log_pending_slots(device_id: str, prefix: str, states: list[str]) -> None:
    """Rate-limited diagnosis of slots that stay UNKNOWN, i.e. keep the tower on "Detecting modules"."""
    unknown = [i for i, state in enumerate(states) if state == _SLOT_UNKNOWN]
    if not unknown:
        return
    now = time.monotonic()
    key = (device_id, prefix)
    with _battery_module_lock:
        if now - _pending_log_last.get(key, -_PENDING_LOG_INTERVAL_SECONDS) < _PENDING_LOG_INTERVAL_SECONDS:
            return
        _pending_log_last[key] = now
    log.debug("%s on %s: module serial slots %s never read yet (states: %s)", prefix, device_id, unknown, states)


def _battery_module_report(runtime, device_id: str, prefix: str) -> dict:
    """Trusted module-count report for one tower: captures the current slot-state snapshot (see
    `_battery_module_slot_states`), classifies it (`_raw_battery_module_report`), and runs it
    through the last-known-good stabilizer so neither a mid-scan nor a single flaky complete read
    can override an already-established topology (see `_stabilize_battery_module_report`)."""
    states = _battery_module_slot_states(runtime, device_id, prefix)
    raw = _raw_battery_module_report(states)
    _log_pending_slots(device_id, prefix, states)
    report = _stabilize_battery_module_report(runtime, device_id, prefix, raw)
    if report is raw and report["module_count_status"] == "anomaly":
        log.warning(
            "%s on %s reports module serials in slots %s: not a contiguous run of at most %d slots, so no "
            "module count is derived from it (catalog carries %d slots; documented hardware takes %d modules)",
            prefix, device_id, report["populated_module_slots"], RCT_MAX_MODULES_PER_TOWER, RCT_MODULE_SN_SLOTS,
            RCT_MAX_MODULES_PER_TOWER,
        )
    return {k: v for k, v in report.items() if k != "_complete"}


def _battery_metric_names(runtime, prefix: str, *, include_device_wide: bool) -> dict[str, str]:
    """Catalog names for one tower's card roles; only names the catalog actually carries."""
    names = {}
    for role, suffix in _BATTERY_TOWER_METRIC_SUFFIXES.items():
        name = f"{prefix}_{suffix}"
        if runtime.catalog.exists(name):
            names[role] = name
    if include_device_wide:
        names.update({role: name for role, name in _BATTERY_DEVICE_METRIC_NAMES.items()
                      if runtime.catalog.exists(name)})
    return names


DEVICE_CARD_METRIC_NAMES = (
    "inverter_state", "battery_status2", "battery_placeholder_0_status2",
    "battery_soc_target", "power_mng_bat_next_calib_date",
    "heat_sink_temperature",  # inverter-side actual temperature, not the sink_temp power-reduction target
    "battery_temperature",  # battery pack temperature, distinct from the inverter's heat_sink_temperature
    "battery_cycles",  # battery.cycles - aggregate pack charge-cycle counter
    # Second tower's own readings: without them the battery_placeholder_0 card had nothing but
    # shared values to show and repeated the first tower's numbers.
    "battery_placeholder_0_soc", "battery_placeholder_0_temperature",
)


def _energy_flow_view(readings: EnergyReadings | None) -> dict:
    """Maps the cache-only, sign-normalized EnergyReadings onto the dashboard projection.

    Always a five-key object (falls back to absent_readings()), independent of dispatch/Energy
    Manager state, so a device with no cache simply yields all-null/all-stale sub-fields.
    """
    source = readings if readings is not None else absent_readings()
    return {
        field_name: {"value": reading.value, "stale": reading.stale, "age_seconds": reading.age_seconds}
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
    devices_now = tuple(runtime.devices.values())  # one reference read: the dict is swapped, never mutated
    result = []
    exposed = _exposed_names(runtime, _store(request))
    # Card values are independent of the Prometheus export selection.
    preferred = ("solar_a_power", "solar_b_power", "household_load_power", "grid_power", "battery_soc",
                 "ac_power")
    selected = [name for name in preferred if name in exposed]
    selected += [name for name in DEVICE_CARD_METRIC_NAMES if runtime.catalog.exists(name)]
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
            metric = {"name": name, "value": value, "unit": entry.unit,
                     "stale": freshness is CacheFreshness.GRACE}
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
            if not _battery_tower_present(runtime, item.device_id, prefix,
                                           module_report["populated_module_slots"]):
                continue
            tower = {
                "id": prefix,
                "title": f"Battery {len(batteries) + 1}",
                "metrics": _battery_metric_names(runtime, prefix, include_device_wide=not batteries),
            }
            tower.update(module_report)
            batteries.append(tower)
        reported = runtime.gateway.reported_name(item.device_id)
        # Read-only, cache-only, dispatch-independent: present for every device unconditionally, so
        # the dashboard flow graphic works with write support/dispatch/Energy Manager disabled.
        flow = runtime.energy_readings.readings(item.device_id) if runtime.energy_readings else None
        result.append({
            "id": item.device_id, "name": item.display_name or reported or item.device_id,
            "status": status.state.value, "host": item.host, "port": item.port,
            "last_success_at": status.last_success_at.isoformat() if status.last_success_at else None,
            "queue_length": status.queue_length, "metrics": readings, "batteries": batteries,
            "energy_flow": _energy_flow_view(flow),
        })
    return {"devices": result, "tsdb": _tsdb_view(runtime)}


def _parameter_view(request: Request) -> dict:
    runtime = request.app.state.runtime
    store = _store(request)
    entries = list(runtime.catalog.entries())
    exposed = _exposed_names(runtime, store)
    if not hasattr(request.app.state, "active_exposed_names"):
        request.app.state.active_exposed_names = list(exposed)
    active = request.app.state.active_exposed_names
    write = store.get("write_names") or []
    return {
        # help_text is "" for every parameter whose meaning the catalog does not document; the GUI
        # then renders no help line instead of an empty one.
        "available": [{"name": entry.name, "description": entry.description, "unit": entry.unit,
                       "help_text": entry.help_text,
                       "writable": entry.name in request.app.state.default_write_entries, "exportable": is_numeric(entry.value_type)} for entry in entries],
        "exposed_names": list(exposed), "write_names": list(write),
        # Collection runs with the selection loaded at startup; write permissions apply immediately.
        "restart_required": ["exposed_names"] if list(exposed) != list(active) else [],
        "live": ["write_names"],
    }


@router.get("/parameters")
def get_parameters(request: Request) -> dict:
    require_admin(request)
    return _parameter_view(request)


def _dispatch_in_use(request: Request) -> bool:
    runtime = request.app.state.runtime
    energy = runtime.energy
    if energy is not None and any(energy.mode(device_id) is not EnergyMode.OFF for device_id in tuple(runtime.devices)):
        return True
    dispatch_store = getattr(request.app.state, "dispatch_store", None)
    if dispatch_store is None:
        return False
    return any(
        record.state is not DispatchState.IDLE or record.restore_required or record.intent is not None
        for record in dispatch_store.all()
    )


@router.put("/parameters")
def put_parameters(body: ParameterSelection, request: Request) -> dict:
    session = require_admin(request, mutation=True)
    runtime = request.app.state.runtime
    allowed = {entry.name for entry in runtime.catalog.entries() if is_numeric(entry.value_type)}
    write_allowed = set(request.app.state.default_write_entries)
    if len(set(body.exposed_names)) != len(body.exposed_names) or set(body.exposed_names) - allowed:
        raise HTTPException(400, "Invalid exposed metrics")
    if len(set(body.write_names)) != len(body.write_names) or set(body.write_names) - write_allowed:
        raise HTTPException(400, "Invalid writable metrics")
    store = _store(request)

    def _mutate(current: list[str]) -> list[str]:
        # The in-use check and the revoke must not be separated by another save: both run inside
        # the shared lock update_write_names() holds, same as before this was factored out (M8).
        if session is None and set(body.write_names) - set(current):
            # Widening the write allowlist is a privilege change: cookie session only.
            raise HTTPException(403, "Administration session required")
        revoked = set(RctDispatchGateway.REQUIRED_WRITES) & set(current) - set(body.write_names)
        if revoked and _dispatch_in_use(request):
            # A restore writes these registers; revoking them now would leave it rejected forever.
            raise HTTPException(
                409, "Required dispatch writes cannot be revoked while a device is not switched off or is dispatching"
            )
        _parameter_view(request)  # pins the selection the running collector started with
        return body.write_names

    def _after(new_write_names: list[str]) -> None:
        if runtime.exporter is not None:
            runtime.exporter.set_exposed(body.exposed_names)

    update_write_names(
        store,
        runtime.gateway,
        request.app.state.build_write_allowlist,
        _mutate,
        extra={"exposed_names": body.exposed_names},
        after_persist=_after,
    )
    return _parameter_view(request)


@router.get("/tokens")
def get_tokens(request: Request) -> dict:
    require_admin(request, sensitive=True, session_only=True)
    return {"tokens": _store(request).list_tokens()}


@router.post("/tokens", status_code=201)
def post_token(body: TokenCreate, request: Request) -> dict:
    require_admin(request, mutation=True, session_only=True)
    try:
        record, token = _store(request).create_token(body.name, body.role, body.expires_at)
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
_DASHBOARD_WIDGETS = frozenset({
    "device-count", "connected-count", "metric-count", "pv-power", "house-power",
    "grid-power", "battery-soc", "days-to-calibration", "tsdb-status", "devices",
})
_DASHBOARD_LAYOUT_KEY = "dashboard_layout"


class DashboardWidgetLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    x: int = Field(ge=0, le=11)
    y: int = Field(ge=0, le=1000)
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
            raise HTTPException(422, f"Widget {widget.id} extends past the 12-column grid")


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
