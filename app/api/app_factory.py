#!/usr/bin/env python3
#
# app/api/app_factory.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""``create_app``: the only place where ports are bound to adapters (design document, section "Lebenszyklus")."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBearer

from app import __version__
from app.admin.api import _settings_persisted
from app.admin.api import router as admin_router
from app.admin.dispatch_api import router as admin_dispatch_router
from app.admin.store import AdminStore
from app.admin.ui import install_ui
from app.allowlist import Allowlist
from app.api.body_limit import BodyLimitMiddleware
from app.api.docs_nav import SIDEBAR_HTML
from app.api.middleware import RequestContextMiddleware
from app.api.problems import ErrorCode, ProblemError, register_handlers
from app.api.routers import catalog as catalog_router
from app.api.routers import dispatch, health, metrics, values, vendor, writes
from app.api.runtime import Runtime
from app.cache import MemoryCache
from app.catalog.base import is_numeric
from app.catalog.registry import RegistryCatalog
from app.clock import Clock, SystemClock
from app.config import DeviceEntry, Settings, url_host
from app.dispatch.capabilities import CapabilityRegistry
from app.dispatch.controller import DispatchController
from app.dispatch.models import DeviceLimits, DispatchConfig
from app.dispatch.store import DispatchStore
from app.errors import ConfigError, DeviceApiError
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
from app.security.dependencies import SecurityContext, source_address
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
_PASSWORD_POLL_SECONDS = 1.0
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


def load_allowlist(settings: Settings, catalog: RegistryCatalog, selected: list[str] | None = None) -> Allowlist:
    if settings.enable_write_support or settings.write_allowlist_path.exists():
        available = Allowlist.load(settings.write_allowlist_path, catalog)
        if selected is None:
            return available
        return Allowlist({name: available.entry(name) for name in selected if available.entry(name)}, catalog)
    return Allowlist({}, catalog)


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
    periodic_names = _periodic_names(settings, catalog, selected_exposed)
    if not settings.periodic_metrics:  # a programmatic periodic_metrics list (not env-settable) is kept as given
        periodic_names = _with_dashboard_metric_names(periodic_names, catalog)
    graph = _build_device_graph(settings, clock, connector, catalog, gateway, periodic_names)
    parts.endpoints = graph.endpoints
    parts.serializers = graph.serializers
    parts.heartbeats = graph.heartbeats
    parts.device_endpoints = graph.device_endpoints
    parts.periodic = graph.periodic
    parts.refresh_devices = graph.refresh_devices
    parts.endpoint_views = graph.endpoint_views
    parts.device_views = graph.device_views
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
    "inverter_state", "battery_status2", "battery_placeholder_0_status2",
    "battery_soc_target", "power_mng_bat_next_calib_date",
    "heat_sink_temperature",  # inverter-side actual temperature, not the sink_temp power-reduction target
    "battery_temperature",  # battery pack temperature, distinct from the inverter's heat_sink_temperature
    "battery_cycles",  # battery.cycles - aggregate pack charge-cycle counter
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
    await asyncio.gather(connect, return_exceptions=True)
    address = f"{entry.host}:{entry.port}"
    if endpoint.state is not EndpointState.CONNECTED:
        log.warning("Inverter %s at %s: connection failed", entry.device_id, address)
        return
    try:
        timeout = (
            settings.queue_max_wait_seconds + settings.connect_timeout_seconds + settings.read_total_timeout_seconds
        )
        async with asyncio.timeout(timeout):
            raw_name = await gateway.read_inverter_name(entry.device_id)
    except (DeviceApiError, TimeoutError) as exc:
        reason = exc.code if isinstance(exc, DeviceApiError) else "startup_timeout"
        log.warning("Inverter %s at %s: connected, name read failed (%s)", entry.device_id, address, reason)
        return
    name = "".join(ch if ch.isprintable() else "?" for ch in raw_name).strip()[:64] or "unknown"
    gateway.set_reported_name(entry.device_id, name)
    log.info("Inverter %s at %s: connected, name=%s", entry.device_id, address, name)


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

        def start_device_jobs() -> None:
            if runtime.shutting_down():
                return  # a late password change must not re-register periodic reads after their teardown
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

        async def reconfigure_devices() -> None:
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
            if runtime.shutting_down():
                return
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

            periodic_names = _periodic_names(runtime.settings, runtime.catalog)
            if not runtime.settings.periodic_metrics:
                periodic_names = _with_dashboard_metric_names(periodic_names, runtime.catalog)
            graph = _build_device_graph(
                runtime.settings, runtime.clock, connector, runtime.catalog, gateway, periodic_names
            )
            gateway.replace_devices(graph.bindings)

            parts.endpoints.clear()
            parts.endpoints.extend(graph.endpoints)
            parts.serializers.clear()
            parts.serializers.extend(graph.serializers)
            parts.heartbeats.clear()
            parts.heartbeats.extend(graph.heartbeats)
            parts.device_endpoints.clear()
            parts.device_endpoints.extend(graph.device_endpoints)
            parts.periodic.clear()
            parts.periodic.extend(graph.periodic)
            parts.refresh_devices.clear()
            parts.refresh_devices.extend(graph.refresh_devices)
            parts.endpoint_views.clear()
            parts.endpoint_views.extend(graph.endpoint_views)
            parts.device_views.clear()
            parts.device_views.extend(graph.device_views)
            if parts.exporter is not None:
                parts.exporter.set_devices(parts.device_views)
                parts.exporter.set_endpoints(parts.endpoint_views)

            runtime.devices.clear()
            runtime.devices.update({d.device_id: d for d in runtime.settings.devices})
            runtime.roles.clear()
            runtime.roles.update(_roles(runtime.settings))

            if runtime.dispatch is not None:
                # Rebuilt the same way as at boot: stored operator values win, the environment only
                # seeds a device that has no record, so a device-list change cannot silently drop a
                # limit or an engineering switch an operator set.
                store_for_limits = app.state.dispatch_store
                limits = store_for_limits.get_device_configs() if store_for_limits is not None else {}
                if (
                    runtime.settings.dispatch_max_charge_power_w is not None
                    and runtime.settings.dispatch_max_discharge_power_w is not None
                ):
                    for device in runtime.settings.devices:
                        limits.setdefault(
                            device.device_id,
                            DeviceLimits(
                                runtime.settings.dispatch_max_charge_power_w,
                                runtime.settings.dispatch_max_discharge_power_w,
                            ),
                        )
                runtime.dispatch.set_limits(limits)

            for serializer in parts.serializers:
                serializer.start()
            start_connections()
            # Same guard start_device_jobs() already uses: a device added while the bootstrap
            # password is still pending must not jump the queue ahead of _await_password_change().
            if store is None or not await asyncio.to_thread(store.password_change_pending):
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
                # Not recover(): that is a one-time startup sweep over every persisted record and
                # would re-run its (heavier) fault handling for devices that were never touched by
                # this reconfiguration. Every device affected by this change was already driven to a
                # clean IDLE above (or the whole reconfiguration aborted before reaching here), so a
                # fresh run() loop per device in the new set is all that is needed; a device kept
                # unchanged across the reconfiguration simply gets a new loop for the same state it
                # already had.
                start_dispatch_tasks(runtime.devices)

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
            if runtime.shutdown is not None and runtime.shutdown.plan is None:
                await runtime.shutdown.run()  # no signal drove the shutdown: run the same phases now
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
            title="RCT REST API",
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
        gateway,
    )
    dispatch_store = None
    if settings.enable_write_support and settings.hmac_secret is not None:
        dispatch_store = DispatchStore(settings.dispatch_db_path, settings.hmac_secret.get_secret_value())
        dispatch_store.initialize()
        # One registry instance for the adapter and the controller: a second one would be a second
        # truth about which hardware is verified.
        dispatch_capabilities = CapabilityRegistry(dispatch_store.get_capabilities())
        dispatch_gateway = RctDispatchGateway(gateway, capabilities=dispatch_capabilities)
        # The store is the truth for the per-device limits and the engineering switch: they are an
        # operator setting, made through the admin dispatch API, and must survive a restart. The
        # environment values are only a bootstrap seed for a device that has no record yet.
        limits = dispatch_store.get_device_configs()
        if settings.dispatch_max_charge_power_w is not None and settings.dispatch_max_discharge_power_w is not None:
            for device in settings.devices:
                limits.setdefault(
                    device.device_id,
                    DeviceLimits(
                        settings.dispatch_max_charge_power_w,
                        settings.dispatch_max_discharge_power_w,
                    ),
                )
        runtime.dispatch = DispatchController(
            dispatch_gateway,
            dispatch_store,
            clock,
            DispatchConfig(
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
            ),
            limits,
            capabilities=dispatch_capabilities,
        )
        coordinator.set_dispatch_restore(runtime.dispatch.shutdown_restore)
    app = FastAPI(
        title="RCT REST API",
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
    app.state.first_start_password = bootstrap_password  # printed by the server once it is listening
    initial_allowlist = Allowlist.load(settings.write_allowlist_path, catalog) if admin_store is not None else None
    if initial_allowlist is not None:
        app.state.default_write_entries = {name: initial_allowlist.entry(name) for name in catalog.names() if initial_allowlist.entry(name)}
        app.state.build_write_allowlist = lambda names: Allowlist(
            {name: app.state.default_write_entries[name] for name in names}, catalog
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
        install_ui(app)
    read_auth = [Depends(_bearer)]
    for router in (health.business, catalog_router.router, values.router):
        app.include_router(router, dependencies=read_auth)
    # Declared in OpenAPI only; _bearer uses auto_error=False, so the handler keeps the trusted-source exception.
    app.include_router(metrics.router, dependencies=read_auth if settings.metrics_require_token else [])
    if settings.enable_vendor_diagnostics:
        app.include_router(vendor.router, dependencies=[Depends(_bearer)])
    if settings.enable_write_support:
        app.include_router(writes.router, dependencies=[Depends(_bearer)])
        app.include_router(dispatch.router, dependencies=[Depends(_bearer)])
    _add_docs(app, settings)
    return app


__all__ = ["create_app", "load_allowlist"]
