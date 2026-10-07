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
from pydantic import BaseModel, Field, ValidationError

from app.admin.store import SESSION_SECONDS
from app.admin.updates import check_for_updates
from app.cache import CacheFreshness
from app.catalog.base import is_numeric
from app.config import Settings
from app.dispatch.controller import ReconfigurationRejected
from app.security.dependencies import source_address

router = APIRouter(prefix="/admin/api", include_in_schema=False)
_COOKIE = "rct_admin_session"

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
_CSRF_COOKIE = "rct_admin_csrf"
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
    "metrics_rate_limit_window_seconds",
}) | _EXPORT_RESTART_KEYS | _DEVICE_LIVE_KEYS
_SECRET_EDITABLE = frozenset({"influxdb_token", "questdb_password"})
_SETTINGS_LOCK = threading.Lock()
log = logging.getLogger(__name__)


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


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin:
        return origin == f"{request.url.scheme}://{request.url.netloc}"
    referer = request.headers.get("referer")
    if referer:
        return referer.startswith(f"{request.url.scheme}://{request.url.netloc}/")
    return True


def _csrf(request: Request, session: dict | None = None) -> None:
    cookie = request.cookies.get(_CSRF_COOKIE)
    header = request.headers.get("x-csrf-token")
    if not _same_origin(request) or not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(403, "Invalid CSRF token")
    if session is not None and not _store(request).verify_csrf(session, header):
        raise HTTPException(403, "Invalid CSRF token")


def _bearer_admin(request: Request, mutation: bool) -> bool:
    """Authorize by a read/write PAT (no cookie session); independent of the auth_required setting."""
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
    if mutation and entry.role != "read/write":
        raise HTTPException(403, "Read/write token required")
    return True


def require_admin(request: Request, *, mutation: bool = False, allow_password_change: bool = False) -> dict | None:
    bearer = not allow_password_change and "authorization" in request.headers and not request.cookies.get(_COOKIE)
    if bearer and _bearer_admin(request, mutation):
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
    return bool(request.app.state.runtime.settings.behind_reverse_proxy or request.url.scheme == "https")


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


def _settings_persisted(settings: Settings) -> dict[str, Any]:
    result = {key: value for key, value in _settings_view(settings).items() if not key.endswith("_configured")}
    for key in _SECRET_EDITABLE:
        secret = getattr(settings, key)
        if secret is not None:
            result[key] = secret.get_secret_value()
    return result


def _pending_restart(request: Request) -> list[str]:
    """Settings whose saved value differs from the one the running server uses."""
    desired = _settings_persisted(request.app.state.admin_desired_settings)
    active = _settings_persisted(request.app.state.runtime.settings)
    return sorted(key for key in desired if key not in _LIVE and desired[key] != active.get(key))


_NAME_MIGRATION = "devices_display_name_repaired"


def _is_address_name(device) -> bool:
    return device.display_name in {f"{device.host}:{device.port}", device.host}


def _repair_device_names(request: Request) -> None:
    """Older saves stored host:port as display_name, which then beat the name the inverter reports.

    Runs once per database and drops only those address-shaped names, so an operator-chosen name stays.
    """
    store = _store(request)
    if store.get(_NAME_MIGRATION):
        return
    with _SETTINGS_LOCK:
        desired = request.app.state.admin_desired_settings
        if any(_is_address_name(device) for device in desired.devices):
            repaired = []
            for device in desired.devices:
                entry = device.model_dump(mode="json")
                if _is_address_name(device):
                    entry["display_name"] = None
                repaired.append(entry)
            request.app.state.admin_desired_settings = store.merge_operator_settings(desired, {"devices": repaired})
            runtime = request.app.state.runtime
            for device_id, device in list(runtime.devices.items()):  # heading recovers without a restart
                if _is_address_name(device):
                    runtime.devices[device_id] = device.model_copy(update={"display_name": None})
        store.put(_NAME_MIGRATION, True)


@router.get("/settings")
def get_settings(request: Request) -> dict:
    require_admin(request)
    _repair_device_names(request)
    return {"settings": _settings_view(request.app.state.admin_desired_settings),
            "restart_required": _pending_restart(request), "live": sorted(_LIVE)}


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
        entry = {"host": host, "port": port, "device_id": item.get("device_id") or None,
                 "display_name": item.get("display_name") or None, "network_id": network_id}
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


def _reconfigure_devices(request: Request) -> None:
    """Rebuild the device/endpoint/scheduling graph in place from the just-saved device list.

    Same cross-thread scheduling as ``_restart_export``: ``put_settings`` runs in a worker thread,
    while the graph and its tasks live on the event loop.

    Raises ``HTTPException(409)`` when ``reconfigure_devices()`` aborted because an affected
    device's active battery dispatch could not be cleanly restored first (``ReconfigurationRejected``,
    see app/dispatch/controller.py); the old device graph and dispatch state are left intact by that
    abort, and the caller (``put_settings``) must not keep the new device list as the applied one.
    An unrelated failure is still only logged, matching the existing behavior for this best-effort
    live-reload path.
    """
    reconfigure = getattr(request.app.state, "reconfigure_devices", None)
    loop = getattr(request.app.state, "loop", None)
    if reconfigure is None or loop is None:
        return
    future = asyncio.run_coroutine_threadsafe(reconfigure(), loop)
    try:
        future.result(timeout=5.0)
    except ReconfigurationRejected as exc:
        raise HTTPException(
            409,
            f"Device {exc.device_id!r} has an active battery dispatch that could not be safely "
            "restored for this change; cancel or wait out that operation and try again.",
        ) from exc
    except Exception:
        log.exception("Reconfiguring devices after a settings change failed")


def _restart_export(request: Request) -> None:
    """Swap the running push-exporter task for a fresh one built from the just-saved settings.

    ``put_settings`` runs sync (FastAPI offloads it to a worker thread), while the exporter task
    and its cancellation live on the event loop, so the restart coroutine is scheduled onto that
    loop and awaited from here instead of being run in-thread.
    """
    restart = getattr(request.app.state, "restart_export", None)
    loop = getattr(request.app.state, "loop", None)
    if restart is None or loop is None:
        return  # export was never started (e.g. password change still pending): nothing to restart
    future = asyncio.run_coroutine_threadsafe(restart(), loop)
    try:
        future.result(timeout=5.0)
    except Exception:
        log.exception("Restarting the metrics export after a settings change failed")


@router.put("/settings")
def put_settings(body: dict[str, Any], request: Request) -> dict:
    require_admin(request, mutation=True)
    if not body or set(body) - _EDITABLE:
        raise HTTPException(400, "Unknown or empty setting")
    body = {k: v for k, v in body.items() if not (k in _SECRET_EDITABLE and v == "")}  # "" keeps the secret
    if not body:
        raise HTTPException(400, "Unknown or empty setting")
    if "devices" in body:
        body["devices"] = _normalize_devices(body["devices"])
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
        if {"metrics_rate_limit_requests", "metrics_rate_limit_window_seconds"} & set(body):
            request.app.state.security.limiter.set_scrape_limit(
                updated.metrics_rate_limit_requests, updated.metrics_rate_limit_window_seconds
            )
        if changed_export_keys:
            _restart_export(request)
        if devices_changed:
            try:
                _reconfigure_devices(request)
            except HTTPException:
                # The live graph rejected the new device list (an active dispatch on an affected
                # device could not be safely restored) and is still running the OLD one. Roll back
                # only the devices field so the admin store, admin_desired_settings and
                # runtime.settings all agree with what the gateway actually has loaded; other fields
                # saved by this same request (e.g. an export setting) stay applied, same as a
                # _restart_export failure already only logs rather than undoing the whole request.
                reverted = _store(request).merge_operator_settings(
                    updated, {"devices": [d.model_dump() for d in previous.devices]}
                )
                request.app.state.admin_desired_settings = reverted
                runtime.settings = runtime.settings.model_copy(update={"devices": previous.devices})
                raise
    return {"settings": _settings_view(updated), "restart_required": _pending_restart(request),
            "live": sorted(_LIVE)}


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


def _battery_tower_present(runtime, device_id: str, prefix: str, populated_module_slots: list[int]) -> bool:
    if populated_module_slots:
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


def _battery_populated_module_slots(runtime, device_id: str, prefix: str) -> list[int]:
    """Indices of the module_sn slots that carry a non-empty serial; all slots are read, not just 6."""
    populated = []
    for i in range(RCT_MODULE_SN_SLOTS):
        name = f"{prefix}_module_sn_{i}"
        if not runtime.catalog.exists(name):
            continue
        reading = runtime.gateway.cached_reading(device_id, name)
        if reading is not None and isinstance(reading[0], str) and _clean_module_serial(reading[0]):
            populated.append(i)
    return populated


def _raw_battery_module_report(populated: list[int]) -> dict:
    """Pure classification of one read's populated slots - no memory of any previous read.

    Which slots are populated matters, not only how many: a contiguous run 0..n-1 with the rest
    empty is unambiguous and is trusted. Anything else - a gap in the middle, or more populated
    slots than the documented hardware takes - is a data anomaly and not a taller tower, so the
    count becomes None and the card renders the battery without a module-accurate tower instead of
    claiming a tower no documented RCT product has.

    `module_count_status` tells the client which case it is looking at:
      "ok"      - trusted count, render that many modules
      "pending" - no serial read yet; the periodic loop fills seven string slots over several
                  cycles, so this is the normal startup state, not an error
      "anomaly" - populated slots that cannot describe a documented tower; diagnostic case

    See `_battery_module_report` for the stability layer built on top of this: a single read here
    can catch the multi-cycle slot refresh mid-flight, so this function alone is not what callers
    should poll on repeatedly.
    """
    if not populated:
        return {"module_count": None, "module_count_status": "pending", "populated_module_slots": []}
    contiguous = populated == list(range(len(populated)))
    if contiguous and len(populated) <= RCT_MAX_MODULES_PER_TOWER:
        # A count of 1 is below the documented minimum of 2, but a read that has only partly
        # completed looks exactly like this. Rendering what was actually read is the
        # non-destructive choice; discarding it would hide a real serial the device reported.
        return {"module_count": len(populated), "module_count_status": "ok",
                "populated_module_slots": populated}
    return {"module_count": None, "module_count_status": "anomaly", "populated_module_slots": populated}


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
    key = (device_id, prefix)
    with _battery_module_lock:
        per_gateway = getattr(runtime.gateway, _BATTERY_MODULE_STATE_ATTR, None)
        if per_gateway is None:
            per_gateway = {}
            setattr(runtime.gateway, _BATTERY_MODULE_STATE_ATTR, per_gateway)
        state = per_gateway.get(key)
        if state is None:
            per_gateway[key] = {"trusted": raw, "candidate": None, "candidate_count": 0}
            return raw
        trusted = state["trusted"]
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


def _battery_module_report(runtime, device_id: str, prefix: str, populated: list[int]) -> dict:
    """Module count for one tower, with counting kept separate from hardware validation and
    debounced across polls so it does not flap on every single read (see
    `_stabilize_battery_module_report`)."""
    raw = _raw_battery_module_report(populated)
    report = _stabilize_battery_module_report(runtime, device_id, prefix, raw)
    if report is raw and report["module_count_status"] == "anomaly":
        log.warning(
            "%s on %s reports module serials in slots %s: not a contiguous run of at most %d slots, so no "
            "module count is derived from it (catalog carries %d slots; documented hardware takes %d modules)",
            prefix, device_id, report["populated_module_slots"], RCT_MAX_MODULES_PER_TOWER, RCT_MODULE_SN_SLOTS,
            RCT_MAX_MODULES_PER_TOWER,
        )
    return report


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


@router.get("/devices")
def devices(request: Request) -> dict:
    require_admin(request)
    _repair_device_names(request)
    runtime = request.app.state.runtime
    result = []
    exposed = _exposed_names(runtime, _store(request))
    # Card values are independent of the Prometheus export selection.
    preferred = ("solar_a_power", "solar_b_power", "household_load_power", "grid_power", "battery_soc",
                 "ac_power")
    card_names = (
        "inverter_state", "battery_status2", "battery_placeholder_0_status2",
        "battery_soc_target", "power_mng_bat_next_calib_date", "heat_sink_temperature",
        "battery_temperature", "battery_cycles",
        # Second tower's own readings: without them the battery_placeholder_0 card had nothing but
        # shared values to show and repeated the first tower's numbers.
        "battery_placeholder_0_soc", "battery_placeholder_0_temperature",
    )
    selected = [name for name in preferred if name in exposed]
    selected += [name for name in card_names if runtime.catalog.exists(name)]
    for item in runtime.devices.values():
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
            populated_module_slots = _battery_populated_module_slots(runtime, item.device_id, prefix)
            if not _battery_tower_present(runtime, item.device_id, prefix, populated_module_slots):
                continue
            tower = {
                "id": prefix,
                "title": f"Battery {len(batteries) + 1}",
                "metrics": _battery_metric_names(runtime, prefix, include_device_wide=not batteries),
            }
            tower.update(_battery_module_report(runtime, item.device_id, prefix, populated_module_slots))
            batteries.append(tower)
        reported = runtime.gateway.reported_name(item.device_id)
        result.append({
            "id": item.device_id, "name": item.display_name or reported or item.device_id,
            "status": status.state.value, "host": item.host, "port": item.port,
            "last_success_at": status.last_success_at.isoformat() if status.last_success_at else None,
            "queue_length": status.queue_length, "metrics": readings, "batteries": batteries,
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


@router.put("/parameters")
def put_parameters(body: ParameterSelection, request: Request) -> dict:
    require_admin(request, mutation=True)
    runtime = request.app.state.runtime
    allowed = {entry.name for entry in runtime.catalog.entries() if is_numeric(entry.value_type)}
    write_allowed = set(request.app.state.default_write_entries)
    if len(set(body.exposed_names)) != len(body.exposed_names) or set(body.exposed_names) - allowed:
        raise HTTPException(400, "Invalid exposed metrics")
    if len(set(body.write_names)) != len(body.write_names) or set(body.write_names) - write_allowed:
        raise HTTPException(400, "Invalid writable metrics")
    store = _store(request)
    _parameter_view(request)  # pins the selection the running collector started with
    store.put_many({"exposed_names": body.exposed_names, "write_names": body.write_names})
    if runtime.exporter is not None:
        runtime.exporter.set_exposed(body.exposed_names)
    runtime.gateway.set_allowlist(request.app.state.build_write_allowlist(body.write_names))
    return _parameter_view(request)


@router.get("/tokens")
def get_tokens(request: Request) -> dict:
    require_admin(request)
    return {"tokens": _store(request).list_tokens()}


@router.post("/tokens", status_code=201)
def post_token(body: TokenCreate, request: Request) -> dict:
    require_admin(request, mutation=True)
    try:
        record, token = _store(request).create_token(body.name, body.role, body.expires_at)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**record, "token": token}


@router.delete("/tokens/{token_id}")
def delete_token(token_id: str, request: Request) -> dict:
    require_admin(request, mutation=True)
    if not _store(request).delete_token(token_id):
        raise HTTPException(404, "Token not found")
    return {"deleted": True}
