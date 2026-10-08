#!/usr/bin/env python3
#
# app/api/app_factory.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""``create_app``: the only place where ports are bound to adapters (design document, section "Lebenszyklus")."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBearer

from app import __version__
from app.admin.api import (
    DEVICE_CARD_METRIC_NAMES,
    RCT_MODULE_SN_SLOTS,
    _settings_persisted,
    repair_device_names,
)
from app.admin.api import router as admin_router
from app.admin.dispatch_api import router as admin_dispatch_router
from app.admin.energy_api import router as admin_energy_router
from app.admin.store import AdminStore
from app.admin.ui import install_ui
from app.allowlist import Allowlist
from app.api.body_limit import BodyLimitMiddleware
from app.api.docs_nav import SIDEBAR_HTML
from app.api.middleware import RequestContextMiddleware
from app.api.problems import ErrorCode, ProblemError, register_handlers
from app.api.routers import catalog as catalog_router
from app.api.routers import dispatch, energy, health, metrics, values, vendor, writes
from app.api.runtime import Runtime
from app.cache import MemoryCache
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
from app.dispatch.soc_policy import SocTargetPolicyRegistry
from app.dispatch.store import DispatchStore
from app.energy.manager import EnergyManager
from app.energy.models import ArmedRecord
from app.errors import ConfigError, DeviceApiError, ReconfigurationBuildError
from app.gateway.energy_readings import RctEnergyReadings
from app.gateway.rct import DeviceBinding, RctGateway
from app.gateway.rct_dispatch import RctDispatchGateway
from app.observability.exporter import DeviceView, EndpointView, MetricsExporter
from app.observability.names import build_metric_names, metric_help
from app.observability.stats import ServiceCounters
from app.protocol.types import StructKind
from app.scheduling.budget import WorkBudget
from app.scheduling.heartbeat import Heartbeat
from app.scheduling.periodic import MAX_PERIODIC_PER_DEVICE, PeriodicManager
from app.scheduling.retry import RetryConfig
from app.scheduling.serializer import AccessSerializer
from app.scheduling.shutdown import ShutdownCoordinator
from app.security.client_ip import ClientIpResolver
from app.security.dependencies import (
    SecurityContext,
    require_write_enabled,
    source_address,
)
from app.security.ratelimit import RateLimiter
from app.security.tokens import TokenStore
from app.transport.endpoint import (
    Connector,
    EndpointConfig,
    EndpointState,
    TransportEndpoint,
)

log = logging.getLogger(__name__)
_PERIODIC_CHECK_SECONDS = 10.0
_NAME_RETRY_SECONDS = 15.0  # between attempts to read the inverter's own name
_PASSWORD_POLL_SECONDS = 1.0
_RECONFIGURE_DRAIN_SECONDS = 10.0  # shutdown waits this long for an in-flight reconfiguration
_REFRESH_CYCLE_SECONDS = 10.0  # 8 reads per cycle cover 40 values within the 2 x pas.period threshold
_bearer = HTTPBearer(auto_error=False, description="Bearer token")
_FAVICON_BYTES = (Path(__file__).resolve().parent.parent / "admin" / "static" / "img" / "favicon.ico").read_bytes()


@dataclass(slots=True)
class _Parts:
    """Objects whose tasks the lifespan starts and stops."""

    endpoints: list[TransportEndpoint] = field(default_factory=list)
    serializers: list[AccessSerializer] = field(default_factory=list)
    heartbeats: list[tuple[TransportEndpoint, Heartbeat]] = field(default_factory=list)
    device_endpoints: list[tuple[DeviceEntry, TransportEndpoint]] = field(default_factory=list)
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
    device_endpoints: list[tuple[DeviceEntry, TransportEndpoint]] = field(default_factory=list)
    periodic: list[PeriodicManager] = field(default_factory=list)
    refresh_devices: list[str] = field(default_factory=list)
    endpoint_views: list[EndpointView] = field(default_factory=list)
    device_views: list[DeviceView] = field(default_factory=list)
    bindings: list[DeviceBinding] = field(default_factory=list)


_GRAPH_FIELDS = (
    "endpoints", "serializers", "heartbeats", "device_endpoints",
    "periodic", "refresh_devices", "endpoint_views", "device_views",
)  # fmt: skip


def load_allowlist(settings: Settings, catalog: RegistryCatalog, selected: list[str] | None = None) -> Allowlist:
    if settings.enable_write_support or settings.write_allowlist_path.exists():
        available = Allowlist.load(settings.write_allowlist_path, catalog)
        if selected is None:
            return available
        return Allowlist({name: available.entry(name) for name in selected if available.entry(name)}, catalog)
    return Allowlist({}, catalog)


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
    settings: Settings, clock: Clock, endpoint: TransportEndpoint, serializer: AccessSerializer
) -> EndpointView:
    counters = endpoint.counters
    window, threshold = settings.unexpected_frame_window_seconds, settings.foreign_access_frame_threshold
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
            endpoint_id, key, config, clock, connector=connector, on_value=gateway.value_sink(key)
        )
        serializer = AccessSerializer(
            endpoint,
            gateway.handler(endpoint),
            queue_max_length=settings.queue_max_length,
            queue_max_wait_seconds=settings.queue_max_wait_seconds,
            budget=WorkBudget(settings.device_budget_transactions, settings.device_budget_window_seconds, clock),
            cache_hit=gateway.cache_hit,
        )
        graph.endpoints.append(endpoint)
        graph.serializers.append(serializer)
        graph.endpoint_views.append(_endpoint_view(settings, clock, endpoint, serializer))
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
                    endpoint, serializer, entry.key, object_ids, settings.periodic_interval_seconds, clock
                )
                graph.periodic.append(binding.periodic)
                graph.refresh_devices.append(entry.device_id)
    return graph


def _build(
    settings: Settings, clock: Clock, connector: Connector | None, selected_writes: list[str] | None = None,
    selected_exposed: list[str] | None = None,
) -> tuple[RctGateway, RegistryCatalog, _Parts, ShutdownCoordinator]:
    catalog = RegistryCatalog.from_file(settings.object_registry_path)
    if settings.heartbeat_metric_name not in catalog.names():
        raise ConfigError("invalid_object_registry", detail="HEARTBEAT_METRIC_NAME is not in the object registry")
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
    graph = _build_device_graph(settings, clock, connector, catalog, gateway, periodic_names)
    parts.adopt(graph)
    numeric = [e for e in catalog.entries() if is_numeric(e.value_type)]
    names = build_metric_names(numeric)
    exposed = selected_exposed if selected_exposed is not None else settings.metrics_exposed_names or catalog.preselected()
    unknown = [n for n in exposed if n not in names]
    if unknown:
        raise ConfigError("invalid_metrics_exposed_names", detail=f"METRICS_EXPOSED_NAMES: not exportable: {unknown}")
    parts.exporter = MetricsExporter(
        cache, clock.monotonic, parts.service, parts.device_views, parts.endpoint_views, names, exposed,
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


_DASHBOARD_METRIC_NAMES = (
    *DEVICE_CARD_METRIC_NAMES,
    # module_sn_0..6 per tower (t_string, non-numeric so the usual periodic-selection filter would
    # skip them): read so app/admin/api.py's devices() can derive each tower's module count from
    # the populated slots. Seven slots is the size of the catalog array, not a module limit - the
    # documented hardware takes at most 6 modules per tower (see RCT_MAX_MODULES_PER_TOWER).
    *(f"battery_module_sn_{i}" for i in range(RCT_MODULE_SN_SLOTS)),
    *(f"battery_placeholder_0_module_sn_{i}" for i in range(RCT_MODULE_SN_SLOTS)),
)


def _with_dashboard_metric_names(periodic_names: list[str], catalog: RegistryCatalog) -> list[str]:
    """Keep device-card values fed even when they are not in METRICS_EXPOSED_NAMES.

    Periodic reads stay within MAX_PERIODIC_PER_DEVICE; silently skip extra names once that cap
    is reached rather than turning an admin-UI nicety into a startup failure.
    """
    if not periodic_names:
        return periodic_names  # periodic reads disabled or no preselected metrics: leave it off
    extra = [n for n in _DASHBOARD_METRIC_NAMES if catalog.exists(n) and n not in periodic_names]
    room = MAX_PERIODIC_PER_DEVICE - len(periodic_names)
    return periodic_names + extra[: max(room, 0)]


_ENERGY_METRIC_NAMES = (
    "battery_soc", "grid_power", "solar_a_power", "solar_b_power", "household_load_power",
    "battery_power",
)  # fmt: skip


def _with_energy_metric_names(periodic_names: list[str], catalog: RegistryCatalog) -> list[str]:
    """Pin the energy-flow figures, the way the dashboard pins its own values.

    The readings are cache-only and feed the dashboard flow graphic as well as the Energy Manager,
    so they are pinned regardless of write support: a narrowed METRICS_EXPOSED_NAMES would otherwise
    let them expire. The flow metrics are a guaranteed feature: when the selection already fills
    MAX_PERIODIC_PER_DEVICE, non-flow names from its end are dropped to make room (and logged).
    """
    if not periodic_names:
        return periodic_names
    extra = [n for n in _ENERGY_METRIC_NAMES if catalog.exists(n) and n not in periodic_names]
    shortfall = len(periodic_names) + len(extra) - MAX_PERIODIC_PER_DEVICE
    names = list(periodic_names)
    if shortfall > 0:
        flow = set(_ENERGY_METRIC_NAMES)
        victims = [n for n in reversed(names) if n not in flow][:shortfall]
        log.warning(
            "Periodic selection is full (%d); dropping %d metric(s) to keep the energy-flow metrics: %s",
            MAX_PERIODIC_PER_DEVICE, len(victims), ", ".join(victims),
        )
        names = [n for n in names if n not in set(victims)]
    return names + extra


def _periodic_names(settings: Settings, catalog: RegistryCatalog,
                    selected_exposed: list[str] | None = None) -> list[str]:
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
    if settings.periodic_metrics:  # a programmatic periodic_metrics list (not env-settable) is kept as given
        return names
    # Order is the cap priority: flow metrics before card metrics before module serials.
    names = _with_energy_metric_names(names, catalog)
    return _with_dashboard_metric_names(names, catalog)


def _seed_limits(settings: Settings, stored: dict[str, DeviceLimits]) -> dict[str, DeviceLimits]:
    """Stored operator limits win; the environment only seeds a device that has no record yet."""
    if settings.dispatch_max_charge_power_w is not None and settings.dispatch_max_discharge_power_w is not None:
        for device in settings.devices:
            stored.setdefault(
                device.device_id,
                DeviceLimits(settings.dispatch_max_charge_power_w, settings.dispatch_max_discharge_power_w),
            )
    return stored


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
    """Read the inverter's own name, retrying until it succeeds.

    A single attempt left the dashboard on the device id ("main") for good whenever the inverter was
    unreachable at startup; the loop ends on success or when a reconfiguration cancels the task.
    """
    await asyncio.gather(connect, return_exceptions=True)
    address = f"{entry.host}:{entry.port}"
    timeout = settings.queue_max_wait_seconds + settings.connect_timeout_seconds + settings.read_total_timeout_seconds
    first_attempt = True
    while True:
        if endpoint.state is not EndpointState.CONNECTED:
            if first_attempt:
                log.warning("Inverter %s at %s: connection failed", entry.device_id, address)
        else:
            try:
                async with asyncio.timeout(timeout):
                    raw_name = await gateway.read_inverter_name(entry.device_id)
            except (DeviceApiError, TimeoutError) as exc:
                if first_attempt:
                    reason = exc.code if isinstance(exc, DeviceApiError) else "startup_timeout"
                    log.warning("Inverter %s at %s: connected, name read failed (%s)", entry.device_id, address, reason)
            else:
                name = "".join(ch if ch.isprintable() else "?" for ch in raw_name).strip()[:64] or "unknown"
                gateway.set_reported_name(entry.device_id, name)
                log.info("Inverter %s at %s: connected, name=%s", entry.device_id, address, name)
                return
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


async def _export_loop(runtime: Runtime, parts: _Parts) -> None:
    """Push export; a configuration or runtime fault here must never stop the API."""
    from app.export.pusher import PushExporter

    try:
        parts.service.export_enabled = True
        await PushExporter(runtime.settings, parts.exporter, parts.service, runtime.clock).run()
    except Exception:
        log.exception("Metrics export stopped")


async def _await_password_change(store: AdminStore, start_jobs: Callable[[], None]) -> None:
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
            new_connects = {id(ep): asyncio.create_task(ep.start()) for ep in parts.endpoints}
            connects.update(new_connects)
            connect_tasks.extend(new_connects.values())
            tasks.extend(new_connects.values())
            for entry, endpoint in parts.device_endpoints:
                task = asyncio.create_task(
                    _log_startup_device(new_connects[id(endpoint)], endpoint, gateway, entry, runtime.settings)
                )
                startup_tasks.append(task)
                tasks.append(task)

        for serializer in parts.serializers:
            serializer.start()
        start_connections()

        def start_export_task() -> None:
            nonlocal export_task
            if runtime.shutting_down():
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
                task = asyncio.create_task(_heartbeat_loop(connects[id(endpoint)], heartbeat))
                heartbeat_tasks.append(task)
                tasks.append(task)
            for manager in parts.periodic:
                task = asyncio.create_task(_periodic_loop(manager, runtime.clock))
                periodic_tasks.append(task)
                tasks.append(task)
            for device_id in parts.refresh_devices:
                task = asyncio.create_task(_refresh_loop(gateway, device_id, runtime.clock))
                refresh_tasks.append(task)
                tasks.append(task)

        def start_device_jobs() -> None:
            if runtime.shutting_down():
                return  # a late password change must not re-register periodic reads after their teardown
            start_polling_tasks()
            if runtime.dispatch is not None:
                tasks.append(asyncio.create_task(runtime.dispatch.recover()))
                start_dispatch_tasks(runtime.devices)
            start_export_task()

        async def restart_export() -> None:
            """Cancel the running export task, if any, and start a fresh one off the current
            ``runtime.settings``. Lets a saved TSDB setting (target, connection, retention) take
            effect without an application restart, same intent as the metrics live-reload path.
            """
            nonlocal export_task
            if runtime.shutting_down():
                return
            if export_task is not None:
                export_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await export_task
                tasks[:] = [t for t in tasks if t is not export_task]  # drop the dead reference
                export_task = None
                runtime.export_task = None
                parts.service.export_enabled = False
            elif store is not None and await asyncio.to_thread(store.password_change_pending):
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
            reset_ids: set[str] = set()
            if runtime.dispatch is not None:
                old_ids = set(runtime.devices)
                new_devices = {d.device_id: d for d in runtime.settings.devices}
                # Affected: removed outright, or re-addressed under the same device_id (host/port/
                # network_id changed) — either way the record a running dispatch holds would no
                # longer describe the physical device the gateway resolves that id to afterward.
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
                for device_id in removed | readdressed:
                    await runtime.dispatch.force_restore_or_raise(device_id)  # raises: abort, nothing torn down yet
                # A removed id is reset like a re-addressed one: _normalize_devices hands a freed id
                # (e.g. "main") to the next device added without an id, and that device must not
                # inherit the removed hardware's capabilities, engineering mode or arming.
                reset_ids = removed | readdressed

            selected_exposed = getattr(app.state, "active_exposed_names", None)
            if selected_exposed is None and store is not None:
                selected_exposed = await asyncio.to_thread(store.get, "exposed_names")
            if is_closing():
                return  # nothing has been built or torn down yet
            # Same set as at boot: this path runs on every Inverters-page save, and a narrower one
            # would make the Energy Manager card go dark on the next settings change.
            periodic_names = _effective_periodic_names(runtime.settings, runtime.catalog, selected_exposed)
            # Built BEFORE any teardown: a failure here leaves the old graph fully running. The
            # build registers its bindings in the live gateway, so the old map is put back until
            # the old graph has actually been torn down.
            old_bindings = gateway.device_bindings()
            try:
                graph = _build_device_graph(
                    runtime.settings, runtime.clock, connector, runtime.catalog, gateway, periodic_names
                )
            except Exception as exc:
                raise ReconfigurationBuildError(str(exc)) from exc
            finally:
                gateway.replace_devices(old_bindings)

            # Identity-bound resets run only now that the new graph built: a failed build above must
            # leave evidence, engineering mode and arming exactly as they were.
            for device_id in reset_ids:
                # Evidence and arming were given for the old physical device, not the new one.
                # Every capability is reset, not only the VERIFIED ones: a revoke keeps its
                # evidence (strategy code, byte widths, sign assumptions, ...) on an UNVERIFIED
                # record, and that evidence must not survive onto whatever device now answers
                # at this device_id — only a VERIFIED->default reset here would let it.
                for capability in runtime.dispatch.capabilities(device_id):
                    await runtime.dispatch.set_capability(
                        device_id, CapabilityRecord(device_id=device_id, name=capability.name)
                    )
                # Engineering mode is the per-device switch that lets dispatch run on unverified
                # hardware; it is a decision about the old physical device and must not carry
                # over either. The power limits themselves (max_charge/discharge_power_w) are
                # a site/installation property, not a hardware-identity claim, so they are left
                # as the operator configured them.
                old_limits = runtime.dispatch.device_limits(device_id)
                if old_limits is not None and old_limits.engineering_mode:
                    await runtime.dispatch.set_device_limits(
                        device_id, replace(old_limits, engineering_mode=False)
                    )
                if runtime.energy is not None and runtime.energy.armed(device_id):
                    await runtime.energy.set_armed(device_id, armed=False, actor=None)

            try:
                await _swap_graph(graph)
            except Exception:
                # The old graph is already torn down, so what runs now is a partial one. Say so
                # loudly and keep readiness red until the operator restarts or saves again.
                runtime.graph_failed = True
                log.exception("Device reconfiguration failed after the old graph was torn down; service marked not ready")
                raise

        async def _swap_graph(graph: _DeviceGraph) -> None:
            old_tasks = [
                *connect_tasks, *startup_tasks, *heartbeat_tasks, *periodic_tasks, *refresh_tasks, *dispatch_tasks,
            ]
            for task in old_tasks:
                task.cancel()
            await asyncio.gather(*old_tasks, return_exceptions=True)
            tasks[:] = [t for t in tasks if t not in old_tasks]  # drop the dead references
            for group in (
                connect_tasks, startup_tasks, heartbeat_tasks, periodic_tasks, refresh_tasks, dispatch_tasks,
            ):
                group.clear()
            connects.clear()

            await asyncio.gather(*(s.stop() for s in parts.serializers), return_exceptions=True)
            await asyncio.gather(*(e.close() for e in parts.endpoints), return_exceptions=True)

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

            parts.adopt(graph)
            if parts.exporter is not None:
                parts.exporter.set_devices(parts.device_views)
                parts.exporter.set_endpoints(parts.endpoint_views)

            # Swapped as whole objects: sync handlers in the thread pool iterate these dicts.
            runtime.devices = {d.device_id: d for d in runtime.settings.devices}
            runtime.roles = _roles(runtime.settings)

            if runtime.dispatch is not None:
                # Rebuilt the same way as at boot: stored operator values win, the environment only
                # seeds a device that has no record, so a device-list change cannot silently drop a
                # limit or an engineering switch an operator set.
                runtime.dispatch.set_limits(_seed_limits(runtime.settings, stored))

            for serializer in parts.serializers:
                serializer.start()
            start_connections()
            # Same guard start_device_jobs() already uses: a device added while the bootstrap
            # password is still pending must not jump the queue ahead of _await_password_change().
            if store is None or not await asyncio.to_thread(store.password_change_pending):
                if is_closing():
                    return
                start_polling_tasks()
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
            async with reconfigure_lock:  # concurrent settings saves must not interleave teardown/rebuild
                await _reconfigure_devices_unlocked()

        async def enable_dispatch() -> None:
            """Build the dispatch controller and Energy Manager backing when write access is switched on
            after boot, and start its tasks the way the boot path does. A no-op when already built.
            """
            if runtime.dispatch is not None or is_closing() or not _dispatch_enabled(runtime.settings):
                return
            dispatch_store_new, armed = _create_dispatch(runtime, app.state.dispatch_config)
            app.state.dispatch_store = dispatch_store_new
            if runtime.energy is not None:
                runtime.energy.attach_dispatch(
                    port=runtime.dispatch, store=dispatch_store_new, readings=runtime.energy_readings, armed=armed
                )
            # While the bootstrap password is pending, start_device_jobs() will start them later.
            if store is None or not await asyncio.to_thread(store.password_change_pending):
                tasks.append(asyncio.create_task(runtime.dispatch.recover()))
                start_dispatch_tasks(runtime.devices)
            log.info("Battery dispatch enabled at runtime")

        async def disable_dispatch() -> list[str]:
            """Hand every device back to automatic operation and disarm it after write access was
            switched off. The controller and its loops stay up so an unfinished restore keeps being
            retried; returns the devices whose restore is still pending.
            """
            pending: list[str] = []
            if runtime.dispatch is None:
                return pending
            for device_id in list(runtime.devices):
                try:
                    await runtime.dispatch.force_restore_or_raise(device_id, StopReason.WRITE_NOT_ALLOWED)
                    if runtime.energy is not None and runtime.energy.armed(device_id):
                        await runtime.energy.set_armed(device_id, armed=False, actor=None)
                except Exception:
                    log.exception("Restore after disabling write access is pending for device %s", device_id)
                    pending.append(device_id)
            return pending

        app.state.enable_dispatch = enable_dispatch
        app.state.disable_dispatch = disable_dispatch
        app.state.restart_export = restart_export
        app.state.reconfigure_devices = reconfigure_devices
        app.state.loop = asyncio.get_running_loop()  # lets a sync admin route schedule restart_export

        if store is not None and await asyncio.to_thread(store.password_change_pending):
            log.warning(
                "Initial admin password not changed yet: periodic reads, heartbeat polling, "
                "metric collection and export are paused until the admin has logged in and changed it."
            )
            tasks.append(asyncio.create_task(_await_password_change(store, start_device_jobs)))
        else:
            start_device_jobs()
        log.info("Service started: %d device(s), %d transport endpoint(s)", len(runtime.devices), len(parts.endpoints))
        try:
            yield
        finally:
            closing = True  # an in-flight reconfiguration stops starting work from here on
            if runtime.shutdown is not None and runtime.shutdown.plan is None:
                await runtime.shutdown.run()  # no signal drove the shutdown: run the same phases now
            try:
                # Let a reconfiguration that is mid-teardown finish before the final cancel, so no
                # task or connection it starts can outlive the sweep below; bounded against a hang.
                await asyncio.wait_for(reconfigure_lock.acquire(), timeout=_RECONFIGURE_DRAIN_SECONDS)
                reconfigure_lock.release()
            except TimeoutError:
                log.warning("A device reconfiguration did not finish during shutdown; cancelling its tasks anyway")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if store is not None:  # after the tasks: nothing writes any more, so the WAL can be folded in
                await asyncio.to_thread(store.close)
            dispatch_store = getattr(app.state, "dispatch_store", None)
            if dispatch_store is not None:
                await asyncio.to_thread(dispatch_store.close)

    return lifespan


def _add_docs(app: FastAPI, settings: Settings) -> None:
    """Token-free documentation, available only when DOCS_PUBLIC is enabled."""

    async def guard(request: Request) -> None:
        active = request.app.state.runtime.settings
        if not active.docs_public:
            raise ProblemError(ErrorCode.DOCS_NOT_AVAILABLE)
        ctx: SecurityContext = request.app.state.security
        ctx.limiter.check_request(source_address(request, ctx))  # docs count against the request rate

    @app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(guard)])
    async def openapi_document() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False, dependencies=[Depends(guard)])
    async def swagger_ui() -> HTMLResponse:
        page = get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="RCT Manager",
            swagger_favicon_url="/favicon.ico",
            swagger_ui_parameters={
                "tryItOutEnabled": True,
                "docExpansion": "list",
                "filter": True,
                "deepLinking": True,
                "tagsSorter": "alpha",
                "operationsSorter": "alpha",
                "defaultModelsExpandDepth": -1,
                "displayRequestDuration": True,
            },
        )
        body = bytes(page.body).decode().replace("</body>", SIDEBAR_HTML + "</body>", 1)
        return HTMLResponse(body)

    if settings.docs_public:
        log.info("API documentation: http://%s:%d/docs (OpenAPI: /openapi.json)",
                 url_host(settings.bind_address), settings.bind_port)
    else:
        log.info("API documentation disabled (DOCS_PUBLIC=false)")


def _create_dispatch(runtime: Runtime, config: DispatchConfig) -> tuple[DispatchStore, dict[str, ArmedRecord]]:
    """Build the dispatch controller and its store onto ``runtime``; used at boot and on a live enable.

    Returns the store and the persisted armed states. The caller starts the controller's tasks.
    """
    settings = runtime.settings
    gateway = runtime.gateway
    dispatch_store = DispatchStore(settings.dispatch_db_path, settings.hmac_secret.get_secret_value())
    dispatch_store.initialize()
    # One registry instance for the adapter and the controller: a second one would be a
    # second truth about which hardware is verified.
    dispatch_capabilities = CapabilityRegistry(dispatch_store.get_capabilities())
    # Same rule for the per-device SoC-target derivation policy: one instance, so an operator's
    # policy change reaches the adapter that actually writes the register, not just the
    # controller.
    soc_target_policies = SocTargetPolicyRegistry(dispatch_store.get_soc_target_policies())
    dispatch_gateway = RctDispatchGateway(
        gateway, capabilities=dispatch_capabilities, soc_target_policies=soc_target_policies
    )
    # The live capability registry, so a verification reaches the published grid sign without a
    # restart. The readings are cache-only and never queue a device transaction.
    runtime.energy_readings = RctEnergyReadings(gateway, capabilities=dispatch_capabilities)
    energy_armed = dispatch_store.get_energy_states()
    # The store is the truth for the per-device limits and the engineering switch: they are an
    # operator setting, made through the admin dispatch API, and must survive a restart. The
    # environment values are only a bootstrap seed for a device that has no record yet.
    limits = _seed_limits(settings, dispatch_store.get_device_configs())
    runtime.dispatch = DispatchController(
        dispatch_gateway,
        dispatch_store,
        runtime.clock,
        config,
        limits,
        capabilities=dispatch_capabilities,
        soc_target_policies=soc_target_policies,
    )

    async def restore_for_shutdown() -> None:
        gateway.begin_shutdown_restore()  # bounded readbacks: the restore runs against the work deadline
        await runtime.dispatch.shutdown_restore()

    runtime.shutdown.set_dispatch_restore(restore_for_shutdown)
    return dispatch_store, energy_armed


def _load_default_write_entries(settings: Settings, catalog: RegistryCatalog) -> dict:
    """The shipped write allowlist entries, loaded regardless of the current write switch.

    Raises ``ConfigError`` when the file is unusable; the boot path treats that as "none" while
    writes are off, a live enable refuses.
    """
    allowlist = load_allowlist(settings.model_copy(update={"enable_write_support": True}), catalog)
    return {name: allowlist.entry(name) for name in catalog.names() if allowlist.entry(name)}


def create_app(settings: Settings, *, clock: Clock | None = None, connector: Connector | None = None) -> FastAPI:
    clock = clock or SystemClock()
    admin_store = AdminStore(settings.admin_db_path, settings.hmac_secret.get_secret_value()) if settings.hmac_secret else None
    selected_writes = selected_exposed = bootstrap_password = None
    if admin_store is not None:
        bootstrap_password = admin_store.initialize()
        overrides = admin_store.get("operator_settings")
        if overrides is None:
            overrides = _settings_persisted(settings)
            admin_store.put("operator_settings", overrides)
        if overrides:
            settings = Settings.model_validate({**settings.model_dump(), **overrides})
        selected_exposed = admin_store.get("exposed_names")
        if selected_exposed is not None:
            settings = settings.model_copy(update={"metrics_exposed_names": selected_exposed})
        settings = repair_device_names(admin_store, settings)
        selected_writes = admin_store.get("write_names")
        if selected_writes is None:
            selected_writes = []  # deny by default: the shipped catalog lists what may be enabled, nothing is on
            admin_store.put("write_names", selected_writes)
    gateway, catalog, parts, coordinator = _build(settings, clock, connector, selected_writes, selected_exposed)
    if admin_store is not None and selected_exposed is None:
        admin_store.put("exposed_names", settings.metrics_exposed_names or catalog.preselected())
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
        max_operation_duration_seconds=settings.dispatch_max_operation_duration_seconds,
        max_operation_duration_engineering_seconds=(
            settings.dispatch_max_operation_duration_engineering_seconds
        ),
    )
    dispatch_store = None
    energy_armed: dict[str, ArmedRecord] = {}
    # Built unconditionally so the dashboard's energy_flow projection (app/admin/api.py::devices())
    # reads cache-only, sign-normalized figures even with write support/dispatch disabled; the
    # dispatch-enabled branch below replaces this with the live capability registry.
    runtime.energy_readings = RctEnergyReadings(gateway, capabilities=CapabilityRegistry())
    if _dispatch_enabled(settings):
        dispatch_store, energy_armed = _create_dispatch(runtime, dispatch_config)
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
    app.state.dispatch_store = dispatch_store
    app.state.dispatch_config = dispatch_config
    app.state.first_start_password = bootstrap_password  # printed by the server once it is listening
    if admin_store is not None:
        try:
            app.state.default_write_entries = _load_default_write_entries(settings, catalog)
        except ConfigError:
            if settings.enable_write_support:
                raise
            app.state.default_write_entries = {}  # a missing file is fine while writes are off; a live enable reloads it
        app.state.build_write_allowlist = lambda names: Allowlist(
            {name: app.state.default_write_entries[name] for name in names if name in app.state.default_write_entries},
            catalog,
        )
        # Constructed after app.state.default_write_entries / build_write_allowlist exist, with
        # late-bound closures over them: an operator can change the approved register names on the
        # Inverters page at any time, so a snapshot taken here would go stale. The manager is built
        # whenever an admin store exists, so the admin surface always has an object that can answer
        # with a proper refusal instead of a missing attribute.
        def _approve_writes(names: Iterable[str]) -> list[str]:
            """Exactly what the Inverters page does, add-only: persist, then widen the allowlist."""
            selected = list(names)
            allowlist = app.state.build_write_allowlist(selected)  # build first: persist only what works
            # Sync SQLite write on the loop: EnergyManager's callback contract is synchronous, and
            # this runs only on an operator arming action against a local WAL database.
            admin_store.put_many({"write_names": selected})
            runtime.gateway.set_allowlist(allowlist)
            return selected

        runtime.energy = EnergyManager(
            port=runtime.dispatch,
            store=dispatch_store,
            readings=runtime.energy_readings,
            clock=clock,
            config=dispatch_config,
            devices=_LiveDevices(runtime),
            write_support_enabled=lambda: runtime.settings.enable_write_support,
            approve_writes=_approve_writes,
            # Synchronous by contract; one small local SQLite read that never waits on the device.
            approved_writes=lambda: tuple(admin_store.get("write_names") or ()),
            allowlist_candidates=lambda: frozenset(app.state.default_write_entries),
            required_writes=RctDispatchGateway.REQUIRED_WRITES,
            armed=energy_armed,
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
        ),
        ClientIpResolver(settings.trusted_proxies, settings.forwarded_header),
        write_enabled=settings.enable_write_support,
        vendor_enabled=settings.enable_vendor_diagnostics,
    )
    register_handlers(app)
    app.add_middleware(BodyLimitMiddleware)  # inner layer: RequestContextMiddleware below stays outermost
    app.add_middleware(RequestContextMiddleware, header=settings.correlation_id_header, counters=parts.service)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(content=_FAVICON_BYTES, media_type="image/vnd.microsoft.icon")

    app.include_router(health.public)
    if admin_store is not None:
        app.include_router(admin_router)
        app.include_router(admin_dispatch_router)
        app.include_router(admin_energy_router)
        install_ui(app)
    read_auth = [Depends(_bearer)]
    for router in (health.business, catalog_router.router, values.router):
        app.include_router(router, dependencies=read_auth)
    # Declared in OpenAPI only; _bearer uses auto_error=False, so the handler keeps the trusted-source exception.
    app.include_router(metrics.router, dependencies=read_auth if settings.metrics_require_token else [])
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
