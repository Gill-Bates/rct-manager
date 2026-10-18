#!/usr/bin/env python3
#
# app/api/app_factory.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""``create_app``: the only place where ports are bound to adapters (design document, lifecycle section)."""

import asyncio
import contextlib
import functools
import logging
from collections.abc import (
    AsyncIterator,
    Callable,
    Iterable,
    Iterator,
    Mapping,
)
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBearer
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.admin.api import (
    DEVICE_CARD_METRIC_NAMES,
    SECOND_TOWER_METRIC_NAMES,
    _settings_persisted,
    second_tower_confirmed,
)
from app.admin.api import router as admin_router
from app.admin.dispatch_api import router as admin_dispatch_router
from app.admin.energy_api import router as admin_energy_router
from app.admin.energy_verification import router as admin_energy_verification_router
from app.admin.forecast_refresh import (
    drain_store_threads,
    run_solar_refresh_loop,
    run_spot_market_refresh_loop,
)
from app.admin.store import AdminStore
from app.admin.ui import install_ui
from app.api.body_limit import BodyLimitMiddleware
from app.api.docs_nav import DOCS_PAGE, DOCS_STATIC_ROOT
from app.api.middleware import RequestContextMiddleware
from app.api.problems import ErrorCode, ProblemError, register_handlers
from app.api.restart import BackgroundRestart
from app.api.routers import catalog as catalog_router
from app.api.routers import consumption as consumption_router
from app.api.routers import dispatch, energy, health, metrics, values, vendor, writes
from app.api.routers import plan as plan_router
from app.api.runtime import READY_STATES, Runtime
from app.catalog.base import is_numeric
from app.catalog.registry import RegistryCatalog
from app.clock import Clock, SystemClock
from app.config import DeviceEntry, Settings, url_host
from app.dispatch.capabilities import (
    CapabilityRecord,
    CapabilityRegistry,
)
from app.dispatch.controller import DispatchController
from app.dispatch.models import DeviceLimits, DispatchConfig, StopReason
from app.dispatch.soc_policy import SocTargetPolicy, SocTargetPolicyRegistry
from app.dispatch.store import DispatchStore
from app.energy.manager import EnergyManager
from app.energy.models import EnergyMode, ModeRecord
from app.energy.plan_handoff import PlanDispatchHandoff
from app.errors import ConfigError, DeviceApiError, ReconfigurationBuildError
from app.gateway.battery_inventory import (
    BMS_SERIAL_METRIC,
    CAPACITY_AH_METRICS,
    RCT_MODULE_SN_SLOTS,
    MeasuredBatteryCapacity,
)
from app.gateway.cache import MemoryCache
from app.gateway.energy_readings import RctEnergyReadings
from app.gateway.live_charge_limit import RctLiveChargeLimitProvider
from app.gateway.rct import DeviceBinding, RctGateway
from app.gateway.rct_dispatch import RctDispatchGateway
from app.limits import DEFAULT_DEVICE_POWER_LIMIT_W
from app.observability.event_hub import EventHub
from app.observability.exporter import DeviceView, EndpointView, MetricsExporter
from app.observability.log_stream import TsdbLogHandler, register_tsdb_log_type
from app.observability.names import build_metric_names, metric_help
from app.observability.stats import ServiceCounters
from app.optimizer.runtime_sources import build_runtime_planning_service
from app.optimizer.service import PlanningService
from app.protocol.types import StructKind
from app.scheduling.budget import WorkBudget
from app.scheduling.heartbeat import Heartbeat
from app.scheduling.periodic import MAX_PERIODIC_PER_DEVICE, PeriodicManager
from app.scheduling.retry import RetryConfig
from app.scheduling.serializer import AccessSerializer
from app.scheduling.shutdown import ShutdownCoordinator
from app.security.allowlist import Allowlist
from app.security.client_ip import ClientIpResolver
from app.security.dependencies import (
    SecurityContext,
    require_write_enabled,
    source_address,
)
from app.security.ratelimit import RateLimiter
from app.security.tokens import TokenStore
from app.spotmarket.energy_charts import EnergyChartsProvider
from app.spotmarket.metadata import BiddingZoneMetadataCache, zone_catalog_persistence
from app.spotmarket.models import SpotMarketConfig
from app.transport.endpoint import (
    Connector,
    EndpointConfig,
    EndpointState,
    TransportEndpoint,
)
from app.weather.geocoding import GeocodingProvider, OpenMeteoGeocoder, PhotonGeocoder
from app.weather.models import WeatherConfig
from app.weather.open_meteo import OpenMeteoProvider
from app.weather.timezone import TimezoneResolver

log = logging.getLogger(__name__)
_PERIODIC_CHECK_SECONDS = 10.0
_PLAN_HANDOFF_POLL_SECONDS = 30.0
_NAME_RETRY_SECONDS = 15.0  # between attempts to read the inverter's own name
_SERIAL_MAX_ATTEMPTS = 3  # tries to read the serial number once the name is known
_PASSWORD_POLL_SECONDS = 1.0
_RECONFIGURE_DRAIN_SECONDS = (
    10.0  # shutdown waits this long for an in-flight reconfiguration
)
_REFRESH_CYCLE_SECONDS = (
    10.0  # 8 reads per cycle cover 40 values within the 2 x pas.period threshold
)
_INVENTORY_CHECK_SECONDS = (
    15.0  # loop cadence; read_module_inventory itself gates the slow device reads
)
_CAPACITY_AH_REFRESH_SECONDS = 24 * 60 * 60
_CAPACITY_AH_RETRY_SECONDS = 6 * 60 * 60
_bearer = HTTPBearer(auto_error=False, description="Bearer token")
_FAVICON_BYTES = (
    Path(__file__).resolve().parent.parent / "admin" / "static" / "img" / "favicon.ico"
).read_bytes()
_DOCS_HEADERS = {
    # Public, token-free page (DOCS_PUBLIC): restrictive by default since it ships "Try it out".
    "Content-Security-Policy": (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


@dataclass(slots=True)
class _Parts:
    """Objects whose tasks the lifespan starts and stops."""

    endpoints: list[TransportEndpoint] = field(default_factory=list)
    serializers: list[AccessSerializer] = field(default_factory=list)
    heartbeats: list[tuple[TransportEndpoint, Heartbeat]] = field(default_factory=list)
    device_endpoints: list[tuple[DeviceEntry, TransportEndpoint]] = field(
        default_factory=list
    )
    periodic: list[PeriodicManager] = field(default_factory=list)
    refresh_devices: list[str] = field(default_factory=list)
    endpoint_views: list[EndpointView] = field(default_factory=list)
    device_views: list[DeviceView] = field(default_factory=list)
    service: ServiceCounters = field(default_factory=ServiceCounters)
    exporter: MetricsExporter | None = None

    def adopt(self, graph: "_DeviceGraph") -> None:
        """Replace the shared lists in place: ShutdownCoordinator and the lifespan hold references."""
        for name in _GRAPH_FIELDS:
            target = getattr(self, name)
            target.clear()
            target.extend(getattr(graph, name))


@dataclass(slots=True)
class _DeviceGraph:
    """Everything ``_build_device_graph`` constructs for the current device/endpoint list.

    Same field set as the matching ``_Parts`` fields, plus the ``DeviceBinding`` objects so the
    caller can hand them to ``RctGateway.replace_devices()``.
    """

    endpoints: list[TransportEndpoint] = field(default_factory=list)
    serializers: list[AccessSerializer] = field(default_factory=list)
    heartbeats: list[tuple[TransportEndpoint, Heartbeat]] = field(default_factory=list)
    device_endpoints: list[tuple[DeviceEntry, TransportEndpoint]] = field(
        default_factory=list
    )
    periodic: list[PeriodicManager] = field(default_factory=list)
    refresh_devices: list[str] = field(default_factory=list)
    endpoint_views: list[EndpointView] = field(default_factory=list)
    device_views: list[DeviceView] = field(default_factory=list)
    bindings: list[DeviceBinding] = field(default_factory=list)


_GRAPH_FIELDS = (
    "endpoints", "serializers", "heartbeats", "device_endpoints",
    "periodic", "refresh_devices", "endpoint_views", "device_views",
)  # fmt: skip


def load_allowlist(
    settings: Settings, catalog: RegistryCatalog, selected: list[str] | None = None
) -> Allowlist:
    # With write support off the allowlist is "none" regardless of the file: a disabled feature
    # must not be able to fail the whole service start on a broken/garbage allowlist file. The
    # file is read only when writes are enabled (including _load_default_write_entries, which
    # passes a copy with enable_write_support=True so its candidate load keeps reading).
    if not settings.enable_write_support:
        return Allowlist({}, catalog)
    available = Allowlist.load(settings.write_allowlist_path, catalog)
    if selected is None:
        return available
    return Allowlist(
        {
            name: entry
            for name in selected
            if (entry := available.entry(name)) is not None
        },
        catalog,
    )


@dataclass(slots=True)
class _IdentityReset:
    """The identity-bound state a reconfiguration must clear off one device_id before the swap.

    Built by ``_plan_identity_resets`` (read only) and applied by ``_apply_identity_reset``, so the
    whole affected set is planned before any device is mutated: see the atomicity note at the call
    site. ``None`` fields mean "already in the safe state, nothing to apply".
    """

    device_id: str
    capabilities: list[CapabilityRecord]
    limits: DeviceLimits | None
    switch_energy_off: bool
    soc_target_policy: SocTargetPolicy


def _plan_identity_resets(
    runtime: Runtime, reset_ids: Iterable[str]
) -> list[_IdentityReset]:
    """Read the current identity-bound state of every affected device and build its reset target.

    Pure reads: nothing is mutated here, so the plan for the whole set is computed before any single
    device is touched. Evidence and the operating mode were given for the OLD physical device, so a
    device_id freed or re-addressed by this reconfiguration must not carry them onto whatever device
    now answers at that id. When dispatch is disabled, this plans nothing.
    """
    if runtime.dispatch is None:
        return []
    plans: list[_IdentityReset] = []
    for device_id in reset_ids:
        # Every capability is reset, not only the VERIFIED ones: a revoke keeps its evidence
        # (strategy code, byte widths, sign assumptions, ...) on an UNVERIFIED record, and that
        # evidence must not survive onto the new device — only a VERIFIED->default reset here would.
        capabilities = [
            CapabilityRecord(device_id=device_id, name=capability.name)
            for capability in runtime.dispatch.capabilities(device_id)
        ]
        # Engineering mode is the per-device switch that lets dispatch run on unverified hardware; it
        # is a decision about the old physical device and must not carry over either. The power
        # limits themselves (max_charge/discharge_power_w) are a site/installation property, not a
        # hardware-identity claim, so they are left as the operator configured them.
        old_limits = runtime.dispatch.device_limits(device_id)
        limits = (
            replace(old_limits, engineering_mode=False)
            if old_limits is not None and old_limits.engineering_mode
            else None
        )
        switch_energy_off = (
            runtime.energy is not None
            and runtime.energy.mode(device_id) is not EnergyMode.OFF
        )
        # SocTargetPolicy is per-device/firmware (design doc), so it is an identity-bound claim
        # exactly like the capability evidence above and must not survive onto the new device.
        plans.append(
            _IdentityReset(
                device_id=device_id,
                capabilities=capabilities,
                limits=limits,
                switch_energy_off=switch_energy_off,
                soc_target_policy=SocTargetPolicy(device_id=device_id),
            )
        )
    return plans


async def _apply_identity_reset(runtime: Runtime, reset: _IdentityReset) -> None:
    """Apply one device's identity reset in fail-closed order: each step moves toward safe/off.

    The order matters: capabilities first (revoking them closes the dispatch gate), then engineering
    mode off, then energy mode OFF, then the SoC policy default. Every write is a single store
    transaction and idempotent, so a store that fails part-way leaves the device MORE restricted,
    never with a residual permissive setting, and the operator's retry re-applies the same target.
    The capability revokes go through the controller's batched ``set_capabilities`` so all of a
    device's rows commit in one transaction instead of one per capability.
    """
    assert runtime.dispatch is not None
    if reset.capabilities:
        await runtime.dispatch.set_capabilities(reset.device_id, reset.capabilities)
    if reset.limits is not None:
        await runtime.dispatch.set_device_limits(reset.device_id, reset.limits)
    if reset.switch_energy_off and runtime.energy is not None:
        await runtime.energy.set_mode(reset.device_id, EnergyMode.OFF, actor=None)
    await runtime.dispatch.set_soc_target_policy(
        reset.device_id, reset.soc_target_policy
    )


class _LiveDevices(Mapping[str, DeviceEntry]):
    """Read-only view that follows ``runtime.devices`` across the whole-dict swap on reconfiguration."""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    def __getitem__(self, key: str) -> DeviceEntry:
        return self._runtime.devices[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._runtime.devices)

    def __len__(self) -> int:
        return len(self._runtime.devices)


def _roles(settings: Settings) -> dict[str, str]:
    members: dict[object, int] = {}
    for device in settings.devices:
        members[device.key.endpoint] = members.get(device.key.endpoint, 0) + 1

    def role(device: DeviceEntry) -> str:
        if device.network_id is not None:
            return "slave"
        return "master" if members[device.key.endpoint] > 1 else "standalone"

    return {d.device_id: role(d) for d in settings.devices}


def _endpoint_view(
    settings: Settings,
    clock: Clock,
    endpoint: TransportEndpoint,
    serializer: AccessSerializer,
) -> EndpointView:
    counters = endpoint.counters
    window, threshold = (
        settings.unexpected_frame_window_seconds,
        settings.foreign_access_frame_threshold,
    )
    return EndpointView(
        endpoint.endpoint_id,
        counters,
        serializer.queue_length,
        lambda: serializer.budget.remaining() if serializer.budget is not None else 0,
        lambda: counters.unexpected_in_window(clock.monotonic(), window) >= threshold,
    )


def _device_view(binding: DeviceBinding) -> DeviceView:
    counters = binding.endpoint.counters_for(binding.entry.key)
    return DeviceView(
        binding.entry.device_id,
        binding.durations,
        lambda: binding.errors,
        lambda: counters.last_success_at,
        lambda: binding.periodic.registrations if binding.periodic is not None else 0,
        lambda: binding.cache_hits,
        lambda: binding.cache_misses,
    )


def _build_device_graph(
    settings: Settings,
    clock: Clock,
    connector: Connector | None,
    catalog: RegistryCatalog,
    gateway: RctGateway,
    periodic_names: list[str],
) -> _DeviceGraph:
    """Transport endpoints, serializers, device bindings and their views for the current device list.

    Shared by the initial boot path (``_build``) and the live device-list reconfiguration
    (``reconfigure_devices`` in ``_lifespan``): both need exactly the same per-endpoint/per-device
    construction, only against a different ``settings.devices``/``settings.endpoints``.
    """
    graph = _DeviceGraph()
    for key, endpoint_id in settings.endpoints.items():
        entries = [d for d in settings.devices if d.key.endpoint == key]
        config = EndpointConfig(
            connect_timeout_seconds=settings.connect_timeout_seconds,
            response_timeout_seconds=settings.response_timeout_seconds,
            write_response_timeout_seconds=settings.write_response_timeout_ms / 1000,
            min_interval=timedelta(milliseconds=settings.min_request_interval_ms),
            max_frame_bytes=settings.max_frame_bytes,
            unexpected_frame_limit=settings.unexpected_frame_limit,
            unexpected_frame_window_seconds=settings.unexpected_frame_window_seconds,
            bootloader_cooldown_seconds=settings.bootloader_cooldown_seconds,
            device_ids=tuple(d.device_id for d in entries),
        )
        endpoint = TransportEndpoint(
            endpoint_id,
            key,
            config,
            clock,
            connector=connector,
            on_value=gateway.value_sink(key),
        )
        serializer = AccessSerializer(
            endpoint,
            gateway.handler(endpoint),
            queue_max_length=settings.queue_max_length,
            queue_max_wait_seconds=settings.queue_max_wait_seconds,
            budget=WorkBudget(
                settings.device_budget_transactions,
                settings.device_budget_window_seconds,
                clock,
            ),
            cache_hit=gateway.cache_hit,
        )
        graph.endpoints.append(endpoint)
        graph.serializers.append(serializer)
        graph.endpoint_views.append(
            _endpoint_view(settings, clock, endpoint, serializer)
        )
        for entry in entries:
            binding = DeviceBinding(entry, endpoint, serializer)
            gateway.add_device(binding)
            graph.bindings.append(binding)
            graph.device_endpoints.append((entry, endpoint))
            binding.heartbeat = Heartbeat(
                endpoint.counters_for(entry.key),
                gateway.heartbeat_read(entry.device_id),
                clock,
                interval_seconds=settings.heartbeat_interval_seconds,
                failure_threshold=settings.heartbeat_failure_threshold,
            )
            graph.heartbeats.append((endpoint, binding.heartbeat))
            graph.device_views.append(_device_view(binding))
            if periodic_names:
                object_ids = [catalog.object_entry(n).object_id for n in periodic_names]
                binding.periodic = PeriodicManager(
                    endpoint,
                    serializer,
                    entry.key,
                    object_ids,
                    settings.periodic_interval_seconds,
                    clock,
                )
                graph.periodic.append(binding.periodic)
                graph.refresh_devices.append(entry.device_id)
    return graph


def _build(
    settings: Settings,
    clock: Clock,
    connector: Connector | None,
    selected_writes: list[str] | None = None,
    selected_exposed: list[str] | None = None,
) -> tuple[RctGateway, RegistryCatalog, _Parts, ShutdownCoordinator]:
    catalog = RegistryCatalog.from_file(settings.object_registry_path)
    if settings.heartbeat_metric_name not in catalog.names():
        raise ConfigError(
            "invalid_object_registry",
            detail="HEARTBEAT_METRIC_NAME is not in the object registry",
        )
    if settings.enable_vendor_diagnostics and not any(
        entry.struct is StructKind.SLAVE_DATA for entry in catalog.entries()
    ):
        # Slave discovery reads this object; without it the diagnostics endpoint cannot work.
        raise ConfigError(
            "invalid_object_registry",
            detail="ENABLE_VENDOR_DIAGNOSTICS requires the slave_data object in the object registry",
        )
    allowlist = load_allowlist(settings, catalog, selected_writes)
    retry = RetryConfig(
        settings.read_retries,
        settings.read_retry_backoff_initial_ms,
        settings.read_retry_backoff_max_ms,
        settings.write_retries,
        settings.response_timeout_seconds,
        settings.read_total_timeout_seconds,
    )
    cache = MemoryCache(settings.cache_ttl_seconds, settings.cache_grace_seconds)
    gateway = RctGateway(
        catalog,
        cache,
        clock,
        allowlist,
        retry=retry,
        fresh_mode=settings.fresh_periodic_mode,
        encoding=settings.string_encoding.value,
        heartbeat_metric=settings.heartbeat_metric_name,
        foreign_frame_threshold=settings.foreign_access_frame_threshold,
        foreign_window_seconds=settings.unexpected_frame_window_seconds,
        slave_cache_ttl_seconds=settings.slave_cache_ttl_seconds,
        slave_stable_reads=settings.slave_discovery_stable_reads,
        slave_max_reads=settings.slave_discovery_max_reads,
    )
    parts = _Parts()
    periodic_names = _effective_periodic_names(settings, catalog, selected_exposed)
    graph = _build_device_graph(
        settings, clock, connector, catalog, gateway, periodic_names
    )
    parts.adopt(graph)
    numeric = [e for e in catalog.entries() if is_numeric(e.value_type)]
    names = build_metric_names(numeric)
    exposed = (
        selected_exposed
        if selected_exposed is not None
        else settings.metrics_exposed_names or catalog.preselected()
    )
    unknown = [n for n in exposed if n not in names]
    if unknown:
        raise ConfigError(
            "invalid_metrics_exposed_names",
            detail=f"METRICS_EXPOSED_NAMES: not exportable: {unknown}",
        )
    parts.exporter = MetricsExporter(
        cache,
        clock.monotonic,
        parts.service,
        parts.device_views,
        parts.endpoint_views,
        names,
        exposed,
        {e.name: e.enum_labels for e in catalog.entries() if e.enum_labels},
        {e.name: metric_help(e) for e in numeric},
    )
    coordinator = ShutdownCoordinator(
        clock,
        grace_seconds=settings.shutdown_grace_seconds,
        periodic_reserve_seconds=settings.shutdown_periodic_reserve_seconds,
        serializers=parts.serializers,
        periodic=parts.periodic,
        endpoints=parts.endpoints,
    )
    return gateway, catalog, parts, coordinator


# The module_sn_0..6 slots per tower (t_string, static inventory data) are DELIBERATELY NOT here.
# They used to be appended to the periodic set so devices() could derive each tower's module count,
# but an unpopulated slot often goes unanswered, and a missing response on the periodic path drops
# the TCP connection and restarts the whole registration round (the startup warning storm). Serials
# are static, so they are read instead by a separate, bounded, long-cached inventory read
# (RctGateway.read_module_inventory, driven from _inventory_loop) that feeds the SAME cache
# devices() reads via cached_reading()/read_failed(). This keeps the periodic default at ~46 reads.
#
# The second tower's live metrics (SECOND_TOWER_METRIC_NAMES) are not pinned here either: a
# single-tower inverter may not answer them, and an unanswered READ drops the connection. They join
# the periodic set at runtime via _activate_second_tower once the stabilized module inventory
# confirms the tower.
_DASHBOARD_METRIC_NAMES = tuple(
    n for n in DEVICE_CARD_METRIC_NAMES if n not in SECOND_TOWER_METRIC_NAMES
)

# The module_sn slots read out-of-band by the inventory loop. Seven slots is the catalog array size
# (RCT_MODULE_SN_SLOTS), not a module limit; both tower prefixes are read so devices() can classify
# the primary and the placeholder tower exactly as before.
_MODULE_INVENTORY_NAMES = (
    *(f"battery_module_sn_{i}" for i in range(RCT_MODULE_SN_SLOTS)),
    *(f"battery_placeholder_0_module_sn_{i}" for i in range(RCT_MODULE_SN_SLOTS)),
)


def _with_dashboard_metric_names(
    periodic_names: list[str], catalog: RegistryCatalog
) -> list[str]:
    """Keep device-card values fed even when they are not in METRICS_EXPOSED_NAMES.

    Periodic reads stay within MAX_PERIODIC_PER_DEVICE; silently skip extra names once that cap
    is reached rather than turning an admin-UI nicety into a startup failure.
    """
    if not periodic_names:
        return periodic_names  # periodic reads disabled or no preselected metrics: leave it off
    extra = [
        n
        for n in _DASHBOARD_METRIC_NAMES
        if catalog.exists(n) and n not in periodic_names
    ]
    room = MAX_PERIODIC_PER_DEVICE - len(periodic_names)
    return periodic_names + extra[: max(room, 0)]


_ENERGY_METRIC_NAMES = (
    "battery_soc", "grid_power", "solar_a_power", "solar_b_power", "household_load_power",
    "battery_power",
)  # fmt: skip
# Cumulative load counters: the consumption historian differences them to recover the energy used
# while this application was stopped, so they keep being polled when the selection narrows.
_CONSUMPTION_COUNTER_NAMES = ("energy_e_load_total", "energy_e_load_day")


def _with_energy_metric_names(
    periodic_names: list[str], catalog: RegistryCatalog
) -> list[str]:
    """Pin the energy-flow figures, the way the dashboard pins its own values.

    The readings are cache-only and feed the dashboard flow graphic as well as the Energy Manager,
    so they are pinned regardless of write support: a narrowed METRICS_EXPOSED_NAMES would otherwise
    let them expire. The flow metrics are a guaranteed feature: when the selection already fills
    MAX_PERIODIC_PER_DEVICE, non-flow names from its end are dropped to make room (and logged).
    """
    if not periodic_names:
        return periodic_names
    extra = [
        n
        for n in (*_ENERGY_METRIC_NAMES, *_CONSUMPTION_COUNTER_NAMES)
        if catalog.exists(n) and n not in periodic_names
    ]
    shortfall = len(periodic_names) + len(extra) - MAX_PERIODIC_PER_DEVICE
    names = list(periodic_names)
    if shortfall > 0:
        flow = set(_ENERGY_METRIC_NAMES)
        victims = [n for n in reversed(names) if n not in flow][:shortfall]
        log.warning(
            "Periodic selection is full (%d); dropping %d metric(s) to keep the energy-flow metrics: %s",
            MAX_PERIODIC_PER_DEVICE,
            len(victims),
            ", ".join(victims),
        )
        names = [n for n in names if n not in set(victims)]
    return names + extra


def _periodic_names(
    settings: Settings,
    catalog: RegistryCatalog,
    selected_exposed: list[str] | None = None,
) -> list[str]:
    """A programmatic periodic_metrics list wins (not env-settable); else the exposed numeric metrics."""
    if not settings.enable_periodic_reads:
        return []
    if settings.periodic_metrics:
        return list(settings.periodic_metrics)
    numeric = {e.name for e in catalog.entries() if is_numeric(e.value_type)}
    if selected_exposed is not None:
        return [name for name in selected_exposed if name in numeric]
    selected = [n for n in catalog.preselected() if n in numeric]
    if len(selected) > MAX_PERIODIC_PER_DEVICE:
        raise ConfigError(
            "too_many_periodic_metrics",
            detail=f"Registry marks {len(selected)} numeric metrics as preselected; "
            f"at most {MAX_PERIODIC_PER_DEVICE} periodic reads per device are supported.",
        )
    return selected


def _dispatch_enabled(settings: Settings) -> bool:
    return settings.enable_write_support and settings.hmac_secret is not None


def _effective_periodic_names(
    settings: Settings, catalog: RegistryCatalog, selected_exposed: list[str] | None
) -> list[str]:
    """Periodic names plus the pinned dashboard and Energy Manager values."""
    names = _periodic_names(settings, catalog, selected_exposed)
    if (
        settings.periodic_metrics
    ):  # a programmatic periodic_metrics list (not env-settable) is kept as given
        return names
    # Order is the cap priority: flow metrics before card metrics before module serials.
    names = _with_energy_metric_names(names, catalog)
    return _with_dashboard_metric_names(names, catalog)


def _seed_limits(
    settings: Settings,
    stored: dict[str, DeviceLimits],
    *,
    default_fallback: bool = False,
) -> dict[str, DeviceLimits]:
    """Stored operator limits win; the environment only seeds a device that has no record yet.

    Usable capacity is per-device (P2-8): an install may run several batteries with different usable
    capacities, so a single global capacity cannot be the long-term source of truth. The legacy
    global ``dispatch_usable_battery_capacity_wh`` is kept ONLY as a one-time seed: a device that has
    no per-device capacity yet is seeded from it (logged) so an existing single-device install keeps
    working without reconfiguration; from then on the per-device value is authoritative and wins. A
    device whose stored record already carries a capacity is never overwritten, and the global is
    never applied on top of a device that already has a different per-device capacity set.
    """
    legacy_capacity = settings.dispatch_usable_battery_capacity_wh
    if (
        settings.dispatch_max_charge_power_w is not None
        and settings.dispatch_max_discharge_power_w is not None
    ):
        for device in settings.devices:
            stored.setdefault(
                device.device_id,
                DeviceLimits(
                    settings.dispatch_max_charge_power_w,
                    settings.dispatch_max_discharge_power_w,
                    usable_capacity_wh=legacy_capacity,
                ),
            )
    # Live dispatch only: Basic mode never asks for power limits, so a device with neither a stored
    # record nor an environment value gets the conservative default (in memory; Expert can persist).
    if default_fallback:
        for device in settings.devices:
            stored.setdefault(
                device.device_id,
                DeviceLimits(
                    DEFAULT_DEVICE_POWER_LIMIT_W,
                    DEFAULT_DEVICE_POWER_LIMIT_W,
                    usable_capacity_wh=legacy_capacity,
                ),
            )
    if legacy_capacity is not None:
        for device_id, limits in stored.items():
            if limits.usable_capacity_wh is None:
                log.warning(
                    "Seeding per-device usable battery capacity for %s from the legacy global "
                    "DISPATCH_USABLE_BATTERY_CAPACITY_WH=%s; set it per device via the dispatch API, "
                    "the per-device value is authoritative from then on",
                    device_id,
                    legacy_capacity,
                )
                stored[device_id] = replace(limits, usable_capacity_wh=legacy_capacity)
    return stored


def _printable(raw: str) -> str:
    return "".join(ch if ch.isprintable() else "?" for ch in raw).strip()


async def _heartbeat_loop(connect: asyncio.Task[None], heartbeat: Heartbeat) -> None:
    await asyncio.gather(connect, return_exceptions=True)
    try:
        await heartbeat.tick()  # leave the "starting" state without waiting a full interval
    except Exception:
        log.exception("Initial heartbeat failed")
        heartbeat.consecutive_failures += 1
    await heartbeat.run()


async def _log_startup_device(
    connect: asyncio.Task[None],
    endpoint: TransportEndpoint,
    gateway: RctGateway,
    entry: DeviceEntry,
    settings: Settings,
) -> None:
    """Read the inverter's own name, retrying until it succeeds, then its serial number.

    A single attempt left the dashboard on the device id ("main") for good whenever the inverter was
    unreachable at startup; the loop ends on success or when a reconfiguration cancels the task.
    """
    await asyncio.gather(connect, return_exceptions=True)
    address = f"{entry.host}:{entry.port}"
    timeout = (
        settings.queue_max_wait_seconds
        + settings.connect_timeout_seconds
        + settings.read_total_timeout_seconds
    )
    first_attempt = True
    name_known = False
    serial_attempts = 0
    while True:
        if name_known:
            # The serial is best effort: a few tries, then the card simply omits it.
            try:
                async with asyncio.timeout(timeout):
                    serial = _printable(
                        await gateway.read_inverter_serial(entry.device_id)
                    )[:64]
            except (DeviceApiError, TimeoutError):
                serial_attempts += 1
                if serial_attempts >= _SERIAL_MAX_ATTEMPTS:
                    log.warning(
                        "Inverter %s at %s: serial number could not be read",
                        entry.device_id,
                        address,
                    )
                    return
            else:
                if serial:
                    gateway.set_reported_serial(entry.device_id, serial)
                return
        elif endpoint.state is not EndpointState.CONNECTED:
            if first_attempt:
                log.warning(
                    "Inverter %s at %s: connection failed", entry.device_id, address
                )
        else:
            try:
                async with asyncio.timeout(timeout):
                    raw_name = await gateway.read_inverter_name(entry.device_id)
            except (DeviceApiError, TimeoutError) as exc:
                if first_attempt:
                    reason = (
                        exc.code
                        if isinstance(exc, DeviceApiError)
                        else "startup_timeout"
                    )
                    log.warning(
                        "Inverter %s at %s: connected, name read failed (%s)",
                        entry.device_id,
                        address,
                        reason,
                    )
            else:
                name = _printable(raw_name)[:64] or "unknown"
                gateway.set_reported_name(entry.device_id, name)
                log.info(
                    "Inverter %s at %s: connected, name=%s",
                    entry.device_id,
                    address,
                    name,
                )
                name_known = True
                continue
        first_attempt = False
        await asyncio.sleep(_NAME_RETRY_SECONDS)


async def _periodic_loop(manager: PeriodicManager, clock: Clock) -> None:
    while True:
        try:
            await manager.ensure()
        except Exception:
            log.exception("Periodic registration check failed")
        await clock.sleep(_PERIODIC_CHECK_SECONDS)


async def _refresh_loop(gateway: RctGateway, device_id: str, clock: Clock) -> None:
    while True:
        await clock.sleep(_REFRESH_CYCLE_SECONDS)
        try:
            await gateway.refresh_stale_periodic(device_id)
        except Exception:
            log.exception("Periodic value refresh failed")


def _activate_second_tower(
    runtime: Runtime, manager: PeriodicManager, device_id: str
) -> bool:
    """Add the second tower's live metrics to the periodic set once its topology is confirmed.

    Returns True when the tower is confirmed (the caller then stops asking), False while it is not.
    """
    if not second_tower_confirmed(runtime, device_id):
        return False
    ids = [
        runtime.catalog.object_entry(n).object_id
        for n in SECOND_TOWER_METRIC_NAMES
        if runtime.catalog.exists(n)
    ]
    if manager.add_object_ids(ids):
        log.info(
            "Second battery tower confirmed on %s: registering its live metrics",
            device_id,
        )
    return True


async def _inventory_loop(
    gateway: RctGateway,
    device_id: str,
    clock: Clock,
    activate_second_tower: Callable[[], bool] | None = None,
    capacity_ah_names: tuple[str, ...] = (),
    bms_serial_name: str | None = None,
) -> None:
    """Drive the bounded initial discovery of the static module-serial inventory, then idle.

    Module serials are static, so read_module_inventory() settles each slot exactly once (answered
    or confirmed empty) and never re-reads a settled slot on a timer; a settled slot is only
    re-discovered when replace_devices() drops the inventory cache. The loop keeps ticking so the
    discovered serials stay warm in the cache (an in-process re-put, no device I/O) and so a
    replace_devices() re-arms discovery, but once every slot is settled each tick does only that
    cheap refresh - there is deliberately no recurring device re-probe. (A very-slow explicit
    rediscovery for hardware-change-without-reconfiguration could be added here later.)
    """
    next_capacity_read = clock.monotonic() + _INVENTORY_CHECK_SECONDS
    while True:
        try:
            await gateway.read_module_inventory(device_id, _MODULE_INVENTORY_NAMES)
        except Exception:
            log.exception("Module inventory read failed")
        if activate_second_tower is not None:
            try:
                if activate_second_tower():
                    activate_second_tower = (
                        None  # one-way: the periodic set keeps the metrics
                    )
            except Exception:
                log.exception("Second-tower detection failed")
        status = gateway.device_status(device_id)
        if (
            capacity_ah_names
            and clock.monotonic() >= next_capacity_read
            and status.state in READY_STATES
            and status.queue_length == 0
        ):
            failed = False
            for name in capacity_ah_names:
                try:
                    await gateway.read_system(device_id, name)
                except DeviceApiError:
                    failed = True
                    log.debug(
                        "Battery Ah background read failed: device=%s metric=%s",
                        device_id,
                        name,
                    )
                except Exception:
                    failed = True
                    log.exception(
                        "Battery Ah background read failed: device=%s metric=%s",
                        device_id,
                        name,
                    )
            if not failed and bms_serial_name is not None:
                # Best effort: without a fresh BMS serial the sample just carries none.
                try:
                    await gateway.read_system(device_id, bms_serial_name)
                except DeviceApiError:
                    log.debug(
                        "Battery BMS serial background read failed: device=%s",
                        device_id,
                    )
                except Exception:
                    log.exception(
                        "Battery BMS serial background read failed: device=%s",
                        device_id,
                    )
            next_capacity_read = clock.monotonic() + (
                _CAPACITY_AH_RETRY_SECONDS if failed else _CAPACITY_AH_REFRESH_SECONDS
            )
        await clock.sleep(_INVENTORY_CHECK_SECONDS)


async def _export_loop(runtime: Runtime, parts: _Parts) -> None:
    """Push export; a configuration or runtime fault here must never stop the API."""
    from app.export.pusher import PushExporter

    try:
        parts.service.export_enabled = True
        await PushExporter(
            runtime.settings, parts.exporter, parts.service, runtime.clock
        ).run()
    except Exception:
        log.exception("Metrics export stopped")
    finally:
        parts.service.export_enabled = False


async def _await_password_change(
    store: AdminStore, start_jobs: Callable[[], None]
) -> None:
    """Hold the device jobs back until the bootstrap password is replaced, then start them."""
    while await asyncio.to_thread(store.password_change_pending):
        await asyncio.sleep(_PASSWORD_POLL_SECONDS)
    log.info("Admin password changed: starting periodic reads, heartbeat and export")
    start_jobs()


def _lifespan(
    runtime: Runtime, parts: _Parts, gateway: RctGateway, connector: Connector | None
) -> Callable[[FastAPI], contextlib.AbstractAsyncContextManager[None]]:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tasks = runtime.tasks
        export_task: asyncio.Task | None = None
        store = getattr(app.state, "admin_store", None)
        # Tracked separately from `tasks` so reconfigure_devices() can cancel exactly the tasks of
        # the current device graph without touching the export task or a not-yet-started future one.
        connect_tasks: list[asyncio.Task] = []
        startup_tasks: list[asyncio.Task] = []
        heartbeat_tasks: list[asyncio.Task] = []
        periodic_tasks: list[asyncio.Task] = []
        refresh_tasks: list[asyncio.Task] = []
        dispatch_tasks: list[asyncio.Task] = []
        planning_tasks: list[asyncio.Task] = []
        dispatch_recovered = asyncio.Event()
        connects: dict[int, asyncio.Task] = {}
        # Set first thing in the teardown: runtime.shutting_down() only turns true once the shutdown
        # coordinator runs, and a reconfiguration must stop starting work before that as well.
        closing = False

        def is_closing() -> bool:
            return closing or runtime.shutting_down()

        def start_dispatch_tasks(device_ids) -> None:
            """One ``dispatch.run()`` loop per device_id, tracked so a later reconfiguration can
            cancel exactly these tasks. Shared by the initial boot path and ``reconfigure_devices``.
            """
            if runtime.dispatch is None:
                return
            for device_id in device_ids:
                task = asyncio.create_task(runtime.dispatch.run(device_id))
                dispatch_tasks.append(task)
                tasks.append(task)

        def start_connections() -> None:
            # Connecting never blocks the start: an unreachable device leaves the service up (Requirement 7.8).
            new_connects = {
                id(ep): asyncio.create_task(ep.start()) for ep in parts.endpoints
            }
            connects.update(new_connects)
            connect_tasks.extend(new_connects.values())
            tasks.extend(new_connects.values())
            for entry, endpoint in parts.device_endpoints:
                task = asyncio.create_task(
                    _log_startup_device(
                        new_connects[id(endpoint)],
                        endpoint,
                        gateway,
                        entry,
                        runtime.settings,
                    )
                )
                startup_tasks.append(task)
                tasks.append(task)

        for serializer in parts.serializers:
            serializer.start()
        start_connections()

        def start_export_task() -> None:
            nonlocal export_task
            if is_closing():
                return
            if (
                runtime.settings.db_type is not None
                and runtime.settings.metrics_export_enabled
                and parts.exporter is not None
            ):
                export_task = asyncio.create_task(_export_loop(runtime, parts))
                tasks.append(export_task)
                runtime.export_task = export_task

        def start_polling_tasks() -> None:
            for endpoint, heartbeat in parts.heartbeats:
                task = asyncio.create_task(
                    _heartbeat_loop(connects[id(endpoint)], heartbeat)
                )
                heartbeat_tasks.append(task)
                tasks.append(task)
            for manager in parts.periodic:
                task = asyncio.create_task(_periodic_loop(manager, runtime.clock))
                periodic_tasks.append(task)
                tasks.append(task)
            for device_id, manager in zip(
                parts.refresh_devices, parts.periodic, strict=True
            ):
                task = asyncio.create_task(
                    _refresh_loop(gateway, device_id, runtime.clock)
                )
                refresh_tasks.append(task)
                tasks.append(task)
                inv_task = asyncio.create_task(
                    _inventory_loop(
                        gateway,
                        device_id,
                        runtime.clock,
                        functools.partial(
                            _activate_second_tower, runtime, manager, device_id
                        ),
                        (
                            CAPACITY_AH_METRICS
                            if all(
                                runtime.catalog.exists(name)
                                for name in CAPACITY_AH_METRICS
                            )
                            else ()
                        ),
                        (
                            BMS_SERIAL_METRIC
                            if runtime.catalog.exists(BMS_SERIAL_METRIC)
                            else None
                        ),
                    )
                )
                refresh_tasks.append(
                    inv_task
                )  # same per-device lifecycle as the refresh loop
                tasks.append(inv_task)

        def start_planning_tasks() -> None:
            if store is None:
                app.state.planning_services = {}
                return
            services = {
                device_id: build_runtime_planning_service(
                    runtime,
                    store,
                    device_id,
                    lambda: app.state.spotmarket_config,
                    lambda: app.state.weather_config,
                )
                for device_id in runtime.devices
            }
            app.state.planning_services = services
            for device_id, service in services.items():
                task = asyncio.create_task(service.run())
                planning_tasks.append(task)
                tasks.append(task)
                if runtime.energy is not None:
                    enabled = runtime.settings.enable_automatic_dispatch
                    handoff = PlanDispatchHandoff(
                        manager=runtime.energy,
                        enable_automatic_dispatch=enabled,
                    )
                    task = asyncio.create_task(run_plan_handoff(device_id, service, handoff, enabled))
                    planning_tasks.append(task)
                    tasks.append(task)

        async def run_plan_handoff(
            device_id: str,
            service: PlanningService,
            handoff: PlanDispatchHandoff,
            enabled: bool,
        ) -> None:
            """Offer the current slot on its own bounded tick, including expiry handback."""
            last_logged_slot = object()
            while True:
                if enabled:
                    await dispatch_recovered.wait()
                try:
                    slot = service.scheduler.active_slot(runtime.clock.now())
                    if enabled or slot != last_logged_slot:
                        await handoff.offer(device_id, slot)
                        last_logged_slot = slot
                except Exception:
                    log.exception("Plan handoff failed for device %s", device_id)
                    if enabled:
                        try:
                            await handoff.offer(device_id, None)
                        except Exception:
                            log.exception("Plan handback failed for device %s", device_id)
                await runtime.clock.sleep(_PLAN_HANDOFF_POLL_SECONDS)

        async def _recover_dispatch() -> None:
            """Await the startup recovery sweep before any dispatch loop starts, and fold its
            outcome into readiness: a green readiness must never precede recovery (C2).

            A sweep that raises outright (not a per-device restore the controller already tracks)
            is otherwise swallowed by the shutdown ``gather(return_exceptions=True)``: surface it on
            ``runtime.dispatch_recovery_failed`` so readiness reports not-ready, log it, and start no
            dispatch loop, rather than leaving the loops down while readiness looks healthy.
            """
            try:
                await runtime.dispatch.recover()
            except asyncio.CancelledError:
                raise
            except Exception:
                runtime.dispatch_recovery_failed = True
                log.exception("Battery dispatch recovery failed")
                return
            runtime.dispatch_recovery_failed = False
            dispatch_recovered.set()
            start_dispatch_tasks(runtime.devices)

        def start_device_jobs() -> None:
            if is_closing():
                return  # a late password change must not re-register periodic reads after their teardown
            if store is not None and runtime.energy is not None:
                # Both forecast loops rest while every inverter is off (the factory state).
                tasks.append(
                    asyncio.create_task(
                        run_spot_market_refresh_loop(
                            store,
                            app.state.spotmarket_provider,
                            app,
                            app.state.spotmarket_refresh_event,
                            activity=runtime.energy.activity,
                        )
                    )
                )
                tasks.append(
                    asyncio.create_task(
                        run_solar_refresh_loop(
                            store,
                            app.state.solar_forecast_provider,
                            app,
                            app.state.weather_refresh_event,
                            activity=runtime.energy.activity,
                        )
                    )
                )
            historian = getattr(app.state, "consumption_historian", None)
            if historian is not None:
                # Drains the household-load buffer into 15-minute SQLite buckets off the receiver path.
                tasks.append(asyncio.create_task(historian.run()))
            if historian is not None and store is not None:
                # Read-only import of the inverter's day totals; always on, it never writes to the device.
                from app.admin.consumption_backfill import (
                    ConsumptionHistoryBackfillService,
                )
                from app.gateway.rct_history import RctDailyConsumptionHistory

                site_timezone = (
                    lambda: getattr(
                        getattr(app.state, "weather_config", None), "timezone", None
                    )
                    or "UTC"
                )
                tasks.append(
                    asyncio.create_task(
                        ConsumptionHistoryBackfillService(
                            RctDailyConsumptionHistory(gateway, site_timezone),
                            store,
                            runtime.clock,
                            site_timezone,
                            lambda: list(runtime.devices),
                            lambda device_id: gateway.device_status(device_id).state
                            in READY_STATES,
                        ).run()
                    )
                )
            if (
                store is not None
                and runtime.settings.battery_commissioning_derivation_enabled
            ):
                from app.admin.battery_commissioning import BatteryCommissioningService
                from app.gateway.rct_history import RctCommissioningHistory

                tasks.append(
                    asyncio.create_task(
                        BatteryCommissioningService(
                            RctCommissioningHistory(gateway, runtime.clock.sleep),
                            store,
                            runtime.clock,
                            lambda: list(runtime.devices),
                            gateway.reported_serial,
                            lambda device_id: gateway.device_status(device_id).state
                            in READY_STATES,
                        ).run()
                    )
                )
            start_polling_tasks()
            start_planning_tasks()
            if runtime.dispatch is not None:
                tasks.append(asyncio.create_task(_recover_dispatch()))
            start_export_task()

        export_restart_lock = asyncio.Lock()

        async def restart_export() -> None:
            """Cancel the running export task, if any, and start a fresh one off the current
            ``runtime.settings``. Lets a saved TSDB setting (target, connection, retention) take
            effect without an application restart, same intent as the metrics live-reload path.

            Serialized: the admin API triggers this outside _SETTINGS_LOCK, so concurrent settings
            saves can call it at the same time. Without the lock, two concurrent calls could both
            see the same ``export_task`` across the ``await`` below and race on it (duplicate or
            orphan the task); the lock plus a stable local reference rule that out.
            """
            nonlocal export_task
            if is_closing():
                return
            async with export_restart_lock:
                if is_closing():
                    return  # teardown began while awaiting the lock: register no fresh export task
                old_task = export_task
                if old_task is not None:
                    old_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await old_task
                    tasks[:] = [
                        t for t in tasks if t is not old_task
                    ]  # drop the dead reference
                    export_task = None
                    runtime.export_task = None
                    parts.service.export_enabled = False
                elif store is not None and await asyncio.to_thread(
                    store.password_change_pending
                ):
                    return  # device jobs (export among them) are still held back by _await_password_change
                start_export_task()

        async def _reconfigure_devices_unlocked() -> None:
            """Rebuild the whole device/endpoint graph from the current ``runtime.settings.devices``
            and swap it into the running gateway/scheduler, without an application restart. Same
            intent as ``restart_export`` for the device list: the admin UI's device add/remove flow
            calls this through ``app.state.reconfigure_devices``.

            Treated as a transaction with respect to battery dispatch: restore every dispatch-active
            device the incoming device list affects (removed, or re-addressed under the same
            device_id) BEFORE any teardown starts, using the still-live old transport. If any of
            those restores does not end cleanly, the whole reconfiguration is aborted here —
            ``ReconfigurationRejected`` propagates to the caller — and nothing below this point runs:
            no task is cancelled, no endpoint is closed, no graph is swapped, so the old transport
            and the stuck dispatch stay exactly as they were for the operator to retry or intervene.
            A swap performed anyway while a dispatch is stuck mid-restore would hand that device's
            I/O to a transport the restore no longer has, which is the wrong failure mode for a path
            that actively writes battery power.

            Only once every affected device is confirmed restored does the existing teardown run:
            every task of the *old* graph (including the per-device dispatch loops) is cancelled and
            awaited, then its serializers are stopped and its endpoints are closed, all before the
            new graph is built — so no old connection or worker can still be running once the swap
            happens. Limits are rebuilt from the new device list and handed to the live
            ``DispatchController``, and fresh dispatch tasks are started for the new device set.
            """
            if is_closing():
                return
            # Captured once: every step below reads only this snapshot, never runtime.settings
            # again, so a concurrent put_settings() publishing a newer one mid-reconfiguration
            # cannot mix values from two different device configurations.
            target = runtime.settings
            old_ids = set(runtime.devices)
            new_devices = {d.device_id: d for d in target.devices}
            # Affected: removed outright, or re-addressed under the same device_id (host/port/
            # network_id changed) — either way the old hardware's state must not carry forward.
            removed = old_ids - set(new_devices)
            readdressed = {
                device_id
                for device_id, old_entry in runtime.devices.items()
                if device_id in new_devices
                and (old_entry.host, old_entry.port, old_entry.network_id)
                != (
                    new_devices[device_id].host,
                    new_devices[device_id].port,
                    new_devices[device_id].network_id,
                )
            }
            reset_ids = removed | readdressed
            if runtime.dispatch is not None:
                for device_id in reset_ids:
                    await runtime.dispatch.force_restore_or_raise(
                        device_id
                    )  # raises: abort, nothing torn down yet
            # A removed id can be reused by _normalize_devices; its learned state is reset too.

            selected_exposed = None
            if store is not None:
                selected_exposed = await asyncio.to_thread(store.get, "exposed_names")
            if is_closing():
                return  # nothing has been built or torn down yet
            # Same set as at boot: this path runs on every Inverters-page save, and a narrower one
            # would make the Energy Manager card go dark on the next settings change.
            periodic_names = _effective_periodic_names(
                target, runtime.catalog, selected_exposed
            )
            # Built BEFORE any teardown: a failure here leaves the old graph fully running. The
            # build registers its bindings in the live gateway, so the old map is put back until
            # the old graph has actually been torn down.
            old_bindings = gateway.device_bindings()
            try:
                graph = _build_device_graph(
                    target,
                    runtime.clock,
                    connector,
                    runtime.catalog,
                    gateway,
                    periodic_names,
                )
            except Exception as exc:
                raise ReconfigurationBuildError(str(exc)) from exc
            finally:
                gateway.replace_devices(old_bindings)

            # Identity-bound resets run only after the new graph built, so a failed build leaves state untouched.
            #
            # Atomicity: the dispatch store commits each table (capabilities, device config, energy
            # mode, SoC policy) in its own SQLite transaction; there is no cross-table transaction
            # in the store API, so a single all-or-nothing commit across the four is not available.
            # Instead the sequence is made abort-safe by two properties the loop below guarantees:
            #   1) a validate phase that can raise runs FIRST, for EVERY affected device, before any
            #      mutation of any device — so an active-operation conflict (CapabilityConflict) or
            #      an unknown-device error cannot abort the run half-applied; and
            #   2) the apply phase only moves state toward the safe/off posture (capabilities ->
            #      unverified, engineering mode off, energy mode OFF, SoC policy -> default), each
            #      via an idempotent single-row (or single-device batched) store write. A store that
            #      fails mid-apply therefore leaves the still-running old device MORE restricted, in
            #      the fail-closed direction, never with a residual permissive setting — and because
            #      every write is idempotent, the operator's retry re-applies the same safe target.
            # This is the tightest guarantee the current store API allows; it is NOT a transactional
            # rollback (see the fix report), but it removes the hazard the review named: an aborted
            # switch can no longer leave a permissive half-reset behind for the old inverter.
            try:
                resets = _plan_identity_resets(runtime, reset_ids)
            except Exception as exc:
                # Validation only reads state; the old graph is still running untouched.
                raise ReconfigurationBuildError(str(exc)) from exc
            try:
                for reset in resets:
                    await _apply_identity_reset(runtime, reset)
            except Exception as exc:
                # The old graph is still running untouched (teardown has not started yet), and every
                # applied write already moved its device toward the safe/off posture, so this stays
                # rollback-capable in the same sense a failed graph build is.
                raise ReconfigurationBuildError(str(exc)) from exc

            try:
                await _swap_graph(graph, target, reset_ids)
            except Exception:
                # The old graph is already torn down, so what runs now is a partial one. Say so
                # loudly and keep readiness red until the operator restarts or saves again.
                runtime.graph_failed = True
                log.exception(
                    "Device reconfiguration failed after the old graph was torn down; service marked not ready"
                )
                raise

        async def _swap_graph(graph: _DeviceGraph, target: Settings, reset_ids: set[str]) -> None:
            old_tasks = [
                *connect_tasks,
                *startup_tasks,
                *heartbeat_tasks,
                *periodic_tasks,
                *refresh_tasks,
                *dispatch_tasks,
                *planning_tasks,
            ]
            for task in old_tasks:
                task.cancel()
            await asyncio.gather(*old_tasks, return_exceptions=True)
            tasks[:] = [
                t for t in tasks if t not in old_tasks
            ]  # drop the dead references
            for group in (
                connect_tasks,
                startup_tasks,
                heartbeat_tasks,
                periodic_tasks,
                refresh_tasks,
                dispatch_tasks,
                planning_tasks,
            ):
                group.clear()
            app.state.planning_services = {}
            connects.clear()

            stop_results = await asyncio.gather(
                *(s.stop() for s in parts.serializers), return_exceptions=True
            )
            close_results = await asyncio.gather(
                *(e.close() for e in parts.endpoints), return_exceptions=True
            )
            errors = [
                r
                for r in (*stop_results, *close_results)
                if isinstance(r, BaseException)
            ]
            if errors:
                # The new graph must never start while an old connection or worker might still be
                # running: that violates the swap's own invariant. Raising here routes into the
                # caller's except-block, which already marks the service not ready.
                raise RuntimeError(
                    "Old device graph could not be stopped cleanly"
                ) from errors[0]

            # Read off the event loop; fetched after the teardown so the window to the set_limits()
            # below stays as short as it was with the synchronous read.
            dispatch_store_now = app.state.dispatch_store
            stored = (
                await asyncio.to_thread(dispatch_store_now.get_device_configs)
                if runtime.dispatch is not None and dispatch_store_now is not None
                else {}
            )
            if is_closing():
                return  # shutdown began while tearing down: start nothing, the shutdown cleans up

            gateway.replace_devices(graph.bindings)
            runtime.energy_readings.reset_devices(list(reset_ids))

            parts.adopt(graph)
            if parts.exporter is not None:
                parts.exporter.set_devices(parts.device_views)
                parts.exporter.set_endpoints(parts.endpoint_views)

            # Swapped as whole objects: sync handlers in the thread pool iterate these dicts.
            runtime.devices = {d.device_id: d for d in target.devices}
            runtime.roles = _roles(target)

            if runtime.dispatch is not None:
                # Rebuilt the same way as at boot: stored operator values win, the environment only
                # seeds a device that has no record, so a device-list change cannot silently drop a
                # limit or an engineering switch an operator set.
                runtime.dispatch.set_limits(
                    _seed_limits(target, stored, default_fallback=True)
                )

            for serializer in parts.serializers:
                serializer.start()
            start_connections()
            # Same guard start_device_jobs() already uses: a device added while the bootstrap
            # password is still pending must not jump the queue ahead of _await_password_change().
            if store is None or not await asyncio.to_thread(
                store.password_change_pending
            ):
                if is_closing():
                    return
                start_polling_tasks()
                start_planning_tasks()
                # Not recover(): that is a one-time startup sweep over every persisted record and
                # would re-run its (heavier) fault handling for devices that were never touched by
                # this reconfiguration. Every device affected by this change was already driven to a
                # clean IDLE above (or the whole reconfiguration aborted before reaching here), so a
                # fresh run() loop per device in the new set is all that is needed; a device kept
                # unchanged across the reconfiguration simply gets a new loop for the same state it
                # already had.
                start_dispatch_tasks(runtime.devices)
            runtime.graph_failed = False

        reconfigure_lock = asyncio.Lock()

        async def reconfigure_devices() -> None:
            async with (
                reconfigure_lock
            ):  # concurrent settings saves must not interleave teardown/rebuild
                await _reconfigure_devices_unlocked()

        # Serializes write-support transitions on the event loop. enable_dispatch() re-checks
        # _write_support_live() after its slow build, so a switch to off during the build is seen
        # and the half-built dispatch is dropped instead of going live.
        dispatch_transition_lock = asyncio.Lock()

        def _write_support_live() -> bool:
            return _dispatch_enabled(runtime.settings) and not is_closing()

        async def enable_dispatch() -> None:
            """Build the dispatch controller and Energy Manager backing when write access is switched on
            after boot, and start its tasks the way the boot path does. Idempotent: a controller that
            already exists is only (re)activated, and nothing is activated while write support is off.
            """
            async with dispatch_transition_lock:
                if is_closing() or not _dispatch_enabled(runtime.settings):
                    return
                if runtime.dispatch is None:
                    # _create_dispatch() does several synchronous DB inits/reads (DispatchStore.initialize(),
                    # get_capabilities(), get_soc_target_policies(), get_energy_states(), get_device_configs());
                    # called directly here it would block the event loop on a live write-support enable. It
                    # builds an UNpublished bundle in the worker and touches no shared runtime state there.
                    bundle = await asyncio.to_thread(
                        _create_dispatch, runtime, app.state.dispatch_config
                    )
                    # Back on the event loop: a concurrent settings save may have turned write support
                    # off during the build. Publish nothing in that case — close the unpublished store
                    # and drop the bundle — so runtime.dispatch stays None rather than being set with a
                    # store other callers would then see without an attached Energy Manager.
                    if not _write_support_live():
                        await asyncio.to_thread(bundle.store.close)
                        return
                    # One on-loop step, no await between the publishes: runtime.dispatch, its store,
                    # readings, shutdown hook, app.state.dispatch_store and the Energy Manager attach
                    # all become visible together, never a half-published dispatch state.
                    _publish_dispatch(runtime, bundle)
                    app.state.dispatch_store = bundle.store
                    if runtime.energy is not None:
                        runtime.energy.attach_dispatch(
                            port=runtime.dispatch,
                            store=bundle.store,
                            readings=runtime.energy_readings,
                            modes=bundle.energy_modes,
                        )
                # Re-checked after the slow build/await: a concurrent settings save may have turned
                # write support off meanwhile. The controller object is harmless on its own (only the
                # per-device run() loops write), so it stays built, but its write-active loops are not
                # started against an operator who just asked for writes off.
                if not _write_support_live():
                    return
                active = any(not t.done() for t in dispatch_tasks)
                if active:
                    return  # loops already running: nothing to (re)start
                # While the bootstrap password is pending, start_device_jobs() will start them later.
                if store is None or not await asyncio.to_thread(
                    store.password_change_pending
                ):
                    if not _write_support_live():
                        return
                    tasks.append(asyncio.create_task(_recover_dispatch()))
            log.info("Battery dispatch enabled at runtime")

        async def disable_dispatch() -> list[str]:
            """Hand every device back to automatic operation and switch it off after write access was
            switched off. The controller and its loops stay up so an unfinished restore keeps being
            retried; a loop ticking an IDLE record with no intent writes nothing, and new intent is
            refused by the write gate. Returns the devices whose restore is still pending.

            Shares ``dispatch_transition_lock`` with enable_dispatch so the two cannot interleave on
            the event loop (the admin API already serializes them worker-side; this closes the gap
            for any other on-loop caller and makes the ordering explicit).
            """
            async with dispatch_transition_lock:
                pending: list[str] = []
                if runtime.dispatch is None:
                    return pending
                for device_id in list(runtime.devices):
                    try:
                        await runtime.dispatch.force_restore_or_raise(
                            device_id, StopReason.WRITE_NOT_ALLOWED
                        )
                        if (
                            runtime.energy is not None
                            and runtime.energy.mode(device_id) is not EnergyMode.OFF
                        ):
                            await runtime.energy.set_mode(
                                device_id, EnergyMode.OFF, actor=None
                            )
                    except Exception:
                        log.exception(
                            "Restore after disabling write access is pending for device %s",
                            device_id,
                        )
                        pending.append(device_id)
                return pending

        app.state.enable_dispatch = enable_dispatch
        app.state.disable_dispatch = disable_dispatch
        app.state.restart_export = restart_export
        app.state.reconfigure_devices = reconfigure_devices
        app.state.loop = (
            asyncio.get_running_loop()
        )  # lets a sync admin route schedule restart_export

        if store is not None:
            app.state.spotmarket_refresh_event = asyncio.Event()
            app.state.weather_refresh_event = asyncio.Event()

        # Capture this app's own TSDB-relevant log records as ``tsdb.log`` events on the hub. Additive:
        # the stdout handler from setup_logging stays untouched. Bind the running loop so a record
        # emitted on any thread reaches subscriber queues on the loop thread, then attach the handler
        # to the root logger. Removed again in the finally below so no handler leaks between the many
        # app instances a test process builds (a leak would bleed records across tests).
        hub: EventHub = app.state.event_hub
        hub.bind_loop(asyncio.get_running_loop())
        tsdb_log_handler = TsdbLogHandler(hub)
        logging.getLogger().addHandler(tsdb_log_handler)

        if store is not None and await asyncio.to_thread(store.password_change_pending):
            log.warning(
                "Initial admin password not changed yet: periodic reads, heartbeat polling, "
                "metric collection and export are paused until the admin has logged in and changed it."
            )
            tasks.append(
                asyncio.create_task(_await_password_change(store, start_device_jobs))
            )
        else:
            start_device_jobs()
        log.info(
            "Service started: %d device(s), %d transport endpoint(s)",
            len(runtime.devices),
            len(parts.endpoints),
        )
        try:
            yield
        finally:
            closing = (
                True  # an in-flight reconfiguration stops starting work from here on
            )
            # Detach the log tap first so nothing in teardown can leak a root handler or feed a hub
            # that is about to be closed; mirrors the addHandler on startup.
            logging.getLogger().removeHandler(tsdb_log_handler)
            hub.close()
            if runtime.shutdown is not None and runtime.shutdown.plan is None:
                await runtime.shutdown.run()  # no signal drove the shutdown: run the same phases now
            try:
                # Let a reconfiguration that is mid-teardown finish before the final cancel, so no
                # task or connection it starts can outlive the sweep below; bounded against a hang.
                await asyncio.wait_for(
                    reconfigure_lock.acquire(), timeout=_RECONFIGURE_DRAIN_SECONDS
                )
                reconfigure_lock.release()
            except TimeoutError:
                log.warning(
                    "A device reconfiguration did not finish during shutdown; cancelling its tasks anyway"
                )
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if store is not None:
                # Cancelling a task does not stop a store call it already handed to a thread, so wait
                # for those (bounded) before closing; then nothing writes and the WAL can be folded in.
                await drain_store_threads()
                await asyncio.to_thread(store.close)
            dispatch_store = getattr(app.state, "dispatch_store", None)
            if dispatch_store is not None:
                await asyncio.to_thread(dispatch_store.close)

    return lifespan


_SWAGGER_UI_ROOT = (
    Path(__file__).resolve().parent.parent
    / "admin"
    / "static"
    / "vendor"
    / "swagger-ui"
)


def _add_docs(app: FastAPI, settings: Settings) -> None:
    """Token-free documentation, available only when DOCS_PUBLIC is enabled."""

    async def guard(request: Request) -> None:
        active = request.app.state.runtime.settings
        if not active.docs_public:
            raise ProblemError(ErrorCode.DOCS_NOT_AVAILABLE)
        ctx: SecurityContext = request.app.state.security
        ctx.limiter.check_request(
            source_address(request, ctx)
        )  # docs count against the request rate

    # Self-hosted, pinned swagger-ui-dist build: the FastAPI default loads swagger-ui-bundle.js and
    # swagger-ui.css from cdn.jsdelivr.net, which would need a CSP exception for a third-party
    # origin and risks bearer-token theft through "Try it out" if that CDN asset is ever compromised.
    app.mount(
        "/docs-static", StaticFiles(directory=str(_SWAGGER_UI_ROOT)), name="docs-static"
    )
    # Self-hosted sidebar/theme CSS and JS: injected into /docs as same-origin assets so the page's
    # default-src 'self' CSP needs no inline-style/script exception (see docs_nav.SIDEBAR_HTML).
    app.mount(
        "/docs-assets", StaticFiles(directory=str(DOCS_STATIC_ROOT)), name="docs-assets"
    )

    @app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(guard)])
    async def openapi_document() -> JSONResponse:
        return JSONResponse(app.openapi(), headers=_DOCS_HEADERS)

    @app.get("/docs", include_in_schema=False, dependencies=[Depends(guard)])
    async def swagger_ui() -> HTMLResponse:
        # Built here (not via get_swagger_ui_html) so the page carries no inline bootstrap <script>:
        # the SwaggerUIBundle initialization lives in the same-origin /docs-assets/docs-init.js, so
        # the restrictive default-src 'self' CSP needs no 'unsafe-inline' or per-response nonce.
        return HTMLResponse(DOCS_PAGE, headers=_DOCS_HEADERS)

    if settings.docs_public:
        log.info(
            "API documentation: http://%s:%d/docs (OpenAPI: /openapi.json)",
            url_host(settings.bind_address),
            settings.bind_port,
        )
    else:
        log.info("API documentation disabled (DOCS_PUBLIC=false)")


@dataclass(slots=True)
class _DispatchBundle:
    """A built-but-unpublished dispatch backing: see ``_create_dispatch``/``_publish_dispatch``."""

    store: DispatchStore
    controller: DispatchController
    energy_readings: RctEnergyReadings
    energy_modes: dict[str, ModeRecord]


def _create_dispatch(runtime: Runtime, config: DispatchConfig) -> "_DispatchBundle":
    """Build an unpublished dispatch bundle; used at boot and on a live enable.

    Does the synchronous DB inits/reads and constructs the controller, store, readings and shutdown
    hook WITHOUT touching shared ``runtime`` state or the ShutdownCoordinator: on a live enable this
    runs in a worker thread (``asyncio.to_thread``), and a worker must not publish state other
    requests or a concurrent shutdown can observe half-built. ``_publish_dispatch`` installs the
    bundle in one on-loop step. Returns the bundle for that caller to publish (and the persisted
    energy modes it carries).
    """
    settings = runtime.settings
    gateway = runtime.gateway
    dispatch_store = DispatchStore(
        settings.dispatch_db_path, settings.hmac_secret.get_secret_value()
    )
    dispatch_store.initialize()
    # One registry instance for the adapter and the controller: a second one would be a
    # second truth about which hardware is verified.
    dispatch_capabilities = CapabilityRegistry(dispatch_store.get_capabilities())
    # Same rule for the per-device SoC-target derivation policy: one instance, so an operator's
    # policy change reaches the adapter that actually writes the register, not just the
    # controller.
    soc_target_policies = SocTargetPolicyRegistry(
        dispatch_store.get_soc_target_policies()
    )
    dispatch_gateway = RctDispatchGateway(
        gateway,
        capabilities=dispatch_capabilities,
        soc_target_policies=soc_target_policies,
    )
    # The live capability registry, so a verification reaches the published grid sign without a
    # restart. The readings are cache-only and never queue a device transaction.
    energy_readings = RctEnergyReadings(gateway, capabilities=dispatch_capabilities)
    energy_modes = dispatch_store.get_energy_states()
    # The store is the truth for the per-device limits and the engineering switch: they are an
    # operator setting, made through the admin dispatch API, and must survive a restart. The
    # environment values are only a bootstrap seed for a device that has no record yet.
    limits = _seed_limits(
        settings, dispatch_store.get_device_configs(), default_fallback=True
    )
    controller = DispatchController(
        dispatch_gateway,
        dispatch_store,
        runtime.clock,
        config,
        limits,
        capabilities=dispatch_capabilities,
        soc_target_policies=soc_target_policies,
        # Live Charge Envelope: a verification-gated RCT producer that is INERT (returns None for
        # every device) until its backing register is confirmed. No read-only max-charge-power
        # register is verified on this build; the only path would be a battery_voltage x
        # maximum_charge_current derivation whose operands carry "not specified by the protocol"
        # catalog caveats, so no production limit is emitted and dispatch falls back to the
        # configured static DeviceLimits.max_charge_power_w. The seam stays honest and testable
        # (AC-11): flip the producer's gate only once the register semantics are confirmed.
        live_charge_limit=RctLiveChargeLimitProvider(gateway),
    )
    return _DispatchBundle(
        store=dispatch_store,
        controller=controller,
        energy_readings=energy_readings,
        energy_modes=energy_modes,
    )


def _publish_dispatch(runtime: Runtime, bundle: "_DispatchBundle") -> None:
    """Install a built ``_DispatchBundle`` into the shared ``runtime`` and the ShutdownCoordinator.

    All assignments run together, on the event loop (or on the main thread at boot, before the loop
    starts), with no ``await`` between them, so no caller can observe a ``runtime.dispatch`` that is
    set while its store, readings or shutdown hook are not. ``enable_dispatch`` must only call this
    once a post-build ``_write_support_live()`` check still holds.
    """
    gateway = runtime.gateway

    async def restore_for_shutdown() -> None:
        gateway.begin_shutdown_restore()  # bounded readbacks: the restore runs against the work deadline
        await bundle.controller.shutdown_restore()

    runtime.energy_readings = bundle.energy_readings
    runtime.dispatch = bundle.controller
    runtime.shutdown.set_dispatch_restore(restore_for_shutdown)


def _load_default_write_entries(settings: Settings, catalog: RegistryCatalog) -> dict:
    """The shipped write allowlist entries, loaded regardless of the current write switch.

    Raises ``ConfigError`` when the file is unusable; the boot path treats that as "none" while
    writes are off, a live enable refuses.
    """
    allowlist = load_allowlist(
        settings.model_copy(update={"enable_write_support": True}), catalog
    )
    return {
        name: entry
        for name in catalog.names()
        if (entry := allowlist.entry(name)) is not None
    }


def _load_spotmarket_config(admin_store: AdminStore | None) -> SpotMarketConfig:
    """The persisted spot-market configuration, or the defaults when none is stored or it is
    unreadable. Spot-market settings live in their own admin-store key, like ``write_names`` — a
    user-editable domain that is deliberately not an environment/operator setting.

    A stored value that no longer validates (an older or tampered record) falls back to defaults
    with a warning rather than failing boot: the spot-market domain must never be able to stop the
    service, which also controls the inverter.
    """
    if admin_store is None:
        return SpotMarketConfig()
    try:
        # AdminStore.get() decodes the record and can raise ValueError on a corrupt/undecryptable
        # row, so it has to be inside the try as well: a damaged spot-market record must fall back
        # to defaults, never stop the service that also controls the inverter.
        stored = admin_store.get("spotmarket_config")
        if stored is None:
            return SpotMarketConfig()
        return SpotMarketConfig.model_validate(stored)
    except ValueError:
        log.warning(
            "Stored spot-market configuration is invalid; using defaults", exc_info=True
        )
        return SpotMarketConfig()


def _load_weather_config(admin_store: AdminStore | None) -> WeatherConfig:
    """The persisted weather/solar configuration, or the defaults when none is stored or it is
    unreadable. Weather settings live in their own admin-store key (``weather_config``), a
    user-editable domain that is deliberately not an environment/operator setting.

    A stored value that no longer validates (an older or tampered record) falls back to defaults
    with a warning rather than failing boot: the weather domain, like the spot-market one, must
    never be able to stop the service that also controls the inverter.
    """
    if admin_store is None:
        return WeatherConfig()
    try:
        # AdminStore.get() decodes the record and can raise ValueError on a corrupt/undecryptable
        # row, so it has to be inside the try as well: a damaged weather record must fall back to
        # defaults, never stop the service that also controls the inverter.
        stored = admin_store.get("weather_config")
        if stored is None:
            return WeatherConfig()
        return WeatherConfig.model_validate(stored)
    except ValueError:
        log.warning(
            "Stored weather configuration is invalid; using defaults", exc_info=True
        )
        return WeatherConfig()


def create_app(
    settings: Settings,
    *,
    clock: Clock | None = None,
    connector: Connector | None = None,
) -> FastAPI:
    clock = clock or SystemClock()
    admin_store = (
        AdminStore(settings.admin_db_path, settings.hmac_secret.get_secret_value())
        if settings.hmac_secret
        else None
    )
    selected_writes = selected_exposed = bootstrap_password = None
    if admin_store is not None:
        bootstrap_password = admin_store.initialize()
        # initialize() returns the generated password only on the first bootstrap, where it has just
        # committed the admin row. The server prints it to the console once it is listening; on a
        # normal restart initialize() returns None and no password is shown.
        overrides = admin_store.get("operator_settings")
        if overrides is None:
            overrides = _settings_persisted(settings)
            admin_store.put("operator_settings", overrides)
        if overrides:
            settings = Settings.model_validate({**settings.model_dump(), **overrides})
        if "session_max_days" not in overrides:
            # Older operator settings must use the same validated lifetime as the login cookies.
            settings = admin_store.merge_operator_settings(
                settings, {"session_max_days": settings.session_max_days}
            )
        selected_exposed = admin_store.get("exposed_names")
        if selected_exposed is not None:
            settings = settings.model_copy(
                update={"metrics_exposed_names": selected_exposed}
            )
        selected_writes = admin_store.get("write_names")
        if selected_writes is None:
            selected_writes = (
                []
            )  # deny by default: the shipped catalog lists what may be enabled, nothing is on
            admin_store.put("write_names", selected_writes)
    gateway, catalog, parts, coordinator = _build(
        settings, clock, connector, selected_writes, selected_exposed
    )
    spotmarket_config = _load_spotmarket_config(admin_store)
    weather_config = _load_weather_config(admin_store)
    if admin_store is not None and selected_exposed is None:
        admin_store.put(
            "exposed_names", settings.metrics_exposed_names or catalog.preselected()
        )
    runtime = Runtime(
        settings,
        clock,
        catalog,
        gateway,
        {d.device_id: d for d in settings.devices},
        _roles(settings),
        coordinator,
        parts.exporter,
        parts.service,
        gateway,
    )
    runtime.dispatch_control_registers = frozenset(RctDispatchGateway.REQUIRED_WRITES)
    runtime.battery_capacity = MeasuredBatteryCapacity(runtime)
    # Built unconditionally and shared with the Energy Manager: it publishes the effective target
    # window from the same bounds the controller validates against, with or without dispatch.
    dispatch_config = DispatchConfig(
        min_soc=settings.dispatch_min_soc,
        max_soc=settings.dispatch_max_soc,
        grid_import_reserve_w=settings.dispatch_grid_import_reserve_w,
        grid_control_deadband_w=settings.dispatch_grid_control_deadband_w,
        power_write_deadband_w=settings.dispatch_power_write_deadband_w,
        min_write_interval_seconds=settings.dispatch_min_write_interval_seconds,
        cycle_interval_seconds=settings.dispatch_cycle_interval_seconds,
        telemetry_timeout_seconds=settings.dispatch_telemetry_timeout_seconds,
        control_telemetry_max_age_seconds=settings.dispatch_control_telemetry_max_age_seconds,
        soc_telemetry_max_age_seconds=settings.dispatch_soc_telemetry_max_age_seconds,
        live_charge_limit_max_age_seconds=settings.dispatch_live_charge_limit_max_age_seconds,
        max_operation_duration_seconds=settings.dispatch_max_operation_duration_seconds,
        max_operation_duration_engineering_seconds=(
            settings.dispatch_max_operation_duration_engineering_seconds
        ),
    )
    dispatch_store = None
    energy_modes: dict[str, ModeRecord] = {}
    # Built unconditionally so the dashboard's energy_flow projection (app/admin/api.py::devices())
    # reads cache-only, sign-normalized figures even with write support/dispatch disabled; the
    # dispatch-enabled branch below replaces this with the live capability registry.
    runtime.energy_readings = RctEnergyReadings(
        gateway, capabilities=CapabilityRegistry()
    )
    if _dispatch_enabled(settings):
        # Boot runs on the main thread before the event loop starts, so building and publishing in
        # two steps here is still atomic with respect to any request handler.
        bundle = _create_dispatch(runtime, dispatch_config)
        _publish_dispatch(runtime, bundle)
        dispatch_store, energy_modes = bundle.store, bundle.energy_modes
    app = FastAPI(
        title="RCT Manager",
        version=__version__,
        description="Vendor-neutral REST gateway for RCT Power inverters.",
        lifespan=_lifespan(runtime, parts, gateway, connector),
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.runtime = runtime
    app.state.admin_desired_settings = settings
    app.state.admin_store = admin_store
    # Local household-consumption historian: a passive observer of the periodic household_load_power
    # value plus a loop-owned SQLite drain. It is the optimizer's data source and is independent of
    # the TSDB exporter. Built only when an admin store exists (its SQLite persistence); the sink is
    # attached to the gateway so value_sink() notifies it after filling the cache, and its drain task
    # is started with the other device jobs. No additional inverter read is ever caused by it.
    consumption_historian = None
    if admin_store is not None:
        from app.admin.consumption import ConsumptionHistorian

        consumption_historian = ConsumptionHistorian(
            admin_store,
            clock,
            timezone=lambda: getattr(
                getattr(app.state, "weather_config", None), "timezone", None
            )
            or "UTC",
        )
        gateway.set_sample_sink(consumption_historian.sink)
    app.state.consumption_historian = consumption_historian
    # A service can be registered per device when charging plans are wired into app startup.
    app.state.planning_services = {}
    # Spot-market configuration is a separate domain from inverter dispatch: a provider is only a
    # data source for a later tariff engine and never touches the gateway, so the config lives on
    # app.state next to the admin store, not in the dispatch/runtime graph.
    app.state.spotmarket_config = spotmarket_config
    spotmarket_provider = EnergyChartsProvider()
    app.state.spotmarket_provider = spotmarket_provider
    # Cache-first bidding-zone metadata: the form reads the zone list from here, so opening it does
    # not hit the rate-limited Energy-Charts API on every open. A failed refresh keeps serving the
    # last good list as stale; it never touches dispatch or inverter control.
    # The last good list is also kept in the admin database, so it survives a restart and an outage.
    zone_load, zone_save = zone_catalog_persistence(admin_store)
    app.state.spotmarket_zone_cache = BiddingZoneMetadataCache(
        spotmarket_provider.fetch_bidding_zones, load=zone_load, save=zone_save
    )
    # Weather/solar forecast is a separate read-only domain and never touches the gateway. Its
    # configured provider refreshes solar_forecast_intervals in the background; previews and
    # dashboard projections read that SQLite table only.
    app.state.weather_config = weather_config
    solar_forecast_provider = OpenMeteoProvider()
    app.state.solar_forecast_provider = solar_forecast_provider
    # Resolve Weather-tab locations server-side; the browser never calls a geocoding provider
    # directly. Photon (OpenStreetMap, no key) is the default; Open-Meteo is the legacy alternative.
    app.state.solar_geocoder = (
        OpenMeteoGeocoder()
        if settings.geocoding_provider is GeocodingProvider.OPEN_METEO
        else PhotonGeocoder()
    )
    # Resolve a site's IANA timezone from its coordinates, provider-independent: the default Photon
    # geocoder carries no zone, so the zone is derived server-side off the event loop (never in the
    # browser) through the same hardened transport the forecast uses. No new network path or key.
    app.state.solar_timezone_resolver = TimezoneResolver()
    # Domain-agnostic in-process pub/sub hub behind the admin SSE endpoint (GET /admin/api/events).
    # Phase 1 wires exactly one producer/type through it: ``tsdb.log`` (fed by the filtering logging
    # handler attached to the root logger in _lifespan, torn down with the app instance). Adding a
    # type later is just register_type + publish; the endpoint and hub carry no domain logic.
    event_hub = EventHub()
    register_tsdb_log_type(event_hub)
    app.state.event_hub = event_hub

    def _restart_listener() -> None:
        hook = getattr(app.state, "server_restart", None)  # set by run_server
        if hook is None:
            log.warning(
                "The listener settings changed, but this server was not started by run_server"
            )
            return
        hook()

    def _listener_differs() -> bool:
        desired, running = app.state.admin_desired_settings, app.state.runtime.settings
        return (desired.bind_address, desired.bind_port) != (
            running.bind_address,
            running.bind_port,
        )

    app.state.background_restart = BackgroundRestart(
        _restart_listener, needed=_listener_differs
    )
    app.state.dispatch_store = dispatch_store
    app.state.dispatch_config = dispatch_config
    app.state.first_start_password = (
        bootstrap_password  # printed by the server once it is listening
    )
    if admin_store is not None:
        try:
            app.state.default_write_entries = _load_default_write_entries(
                settings, catalog
            )
        except ConfigError:
            if settings.enable_write_support:
                raise
            app.state.default_write_entries = (
                {}
            )  # a missing file is fine while writes are off; a live enable reloads it
        app.state.build_write_allowlist = lambda names: Allowlist(
            {
                name: app.state.default_write_entries[name]
                for name in names
                if name in app.state.default_write_entries
            },
            catalog,
        )

        # Constructed after app.state.default_write_entries / build_write_allowlist exist, with
        # late-bound closures over them: an operator can change the approved register names on the
        # Inverters page at any time, so a snapshot taken here would go stale. The manager is built
        # whenever an admin store exists, so the admin surface always has an object that can answer
        # with a proper refusal instead of a missing attribute.
        async def _approve_writes(names: Iterable[str]) -> list[str]:
            """Exactly what the Inverters page does, add-only: persist, then widen the allowlist.

            Goes through the same shared critical section (``update_write_names``, M8) as the
            ``/parameters`` route's revoke, instead of its own unlocked write: without that, an
            add-only widen here could interleave with a concurrent revoke there and lose an update.
            Offloaded: this runs on an operator mode change, and AdminStore can block up to ~5s
            on a SQLite lock (timeout=5, busy_timeout=5000), which must not stall the event loop
            (HTTP, heartbeats, periodic reads, dispatch, shutdown) while it waits.
            """
            from app.admin.api import update_write_names

            selected = frozenset(names)
            return await asyncio.to_thread(
                update_write_names,
                admin_store,
                runtime.gateway,
                app.state.build_write_allowlist,
                lambda current: current + [n for n in selected if n not in current],
            )

        async def _approved_writes() -> tuple[str, ...]:
            """One small local SQLite read; offloaded for the same reason as ``_approve_writes``."""
            return tuple(await asyncio.to_thread(admin_store.get, "write_names") or ())

        async def _revoke_writes(names: Iterable[str]) -> list[str]:
            """Remove exactly ``names`` from the approved write allowlist (P2 rollback primitive).

            The counterpart to ``_approve_writes``: it runs a *remove* mutator through the same
            shared critical section (``update_write_names``, M8), so a failed mode commit can undo
            only the names it just added without racing a concurrent widen or revoke. The add-only
            ``_approve_writes`` cannot do this — handing it a reduced list silently adds nothing and
            removes nothing — so compensation needs its own replace-capable path. Offloaded for the
            same event-loop reason as the widen.
            """
            from app.admin.api import update_write_names

            selected = frozenset(names)
            return await asyncio.to_thread(
                update_write_names,
                admin_store,
                runtime.gateway,
                app.state.build_write_allowlist,
                lambda current: [n for n in current if n not in selected],
            )

        runtime.energy = EnergyManager(
            port=runtime.dispatch,
            store=dispatch_store,
            readings=runtime.energy_readings,
            clock=clock,
            config=dispatch_config,
            devices=_LiveDevices(runtime),
            write_support_enabled=lambda: runtime.settings.enable_write_support,
            approve_writes=_approve_writes,
            approved_writes=_approved_writes,
            allowlist_candidates=lambda: frozenset(app.state.default_write_entries),
            required_writes=RctDispatchGateway.REQUIRED_WRITES,
            internal_strategy_write="power_mng_soc_strategy",
            revoke_writes=_revoke_writes,
            modes=energy_modes,
        )
    app.state.security = SecurityContext(
        TokenStore(auth_required=settings.auth_required, admin_store=admin_store),
        RateLimiter(
            clock,
            requests=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
            auth_fail_limit=settings.auth_fail_limit,
            auth_fail_window_seconds=settings.auth_fail_window_seconds,
            auth_fail_block_seconds=settings.auth_fail_block_seconds,
            scrape_requests=settings.metrics_rate_limit_requests,
            scrape_window_seconds=settings.metrics_rate_limit_window_seconds,
            admin_session_requests=settings.admin_session_rate_limit_requests,
            admin_session_window_seconds=settings.admin_session_rate_limit_window_seconds,
        ),
        ClientIpResolver(settings.trusted_proxies, settings.forwarded_header),
        write_enabled=settings.enable_write_support,
        vendor_enabled=settings.enable_vendor_diagnostics,
    )
    register_handlers(app)
    app.add_middleware(
        BodyLimitMiddleware
    )  # inner layer: RequestContextMiddleware below stays outermost
    app.add_middleware(
        RequestContextMiddleware,
        header=settings.correlation_id_header,
        counters=parts.service,
    )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(content=_FAVICON_BYTES, media_type="image/vnd.microsoft.icon")

    app.include_router(health.public)
    if admin_store is not None:
        app.include_router(admin_router)
        app.include_router(admin_dispatch_router)
        app.include_router(admin_energy_router)
        app.include_router(admin_energy_verification_router)
        install_ui(app)
    read_auth = [Depends(_bearer)]
    for router in (
        health.business,
        catalog_router.router,
        values.router,
        plan_router.router,
        consumption_router.router,
    ):
        app.include_router(router, dependencies=read_auth)
    # Declared in OpenAPI only; _bearer uses auto_error=False, so the handler keeps the trusted-source exception.
    app.include_router(
        metrics.router, dependencies=read_auth if settings.metrics_require_token else []
    )
    if settings.enable_vendor_diagnostics:
        app.include_router(vendor.router, dependencies=[Depends(_bearer)])
    # Always registered and gated per request on the live switch, so toggling write access needs no
    # restart. The gate runs before authentication, like the former "route does not exist" answer.
    write_deps = [Depends(require_write_enabled), Depends(_bearer)]
    for router in (writes.router, dispatch.router, energy.router):
        app.include_router(router, dependencies=write_deps)
    _add_docs(app, settings)
    return app


__all__ = ["create_app", "load_allowlist"]
