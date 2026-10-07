#!/usr/bin/env python3
#
# app/gateway/rct.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""RctGateway: the only adapter that turns metric names into frames (Requirement 30.17)."""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.allowlist import Allowlist
from app.cache import CacheEntry, CacheFreshness, ValueStore, age_seconds
from app.catalog.registry import RegistryCatalog, RegistryEntry
from app.clock import Clock
from app.config import DeviceEntry, DeviceKey, EndpointKey, FreshPeriodicMode
from app.errors import (
    ActionOutcomeUnknown,
    BudgetExhausted,
    ConfigError,
    DeviceApiError,
    DeviceMaintenance,
    DeviceTimeout,
    DeviceUnreachable,
    FreshNotAvailable,
    ProtocolError,
    QueueTimeout,
    UnknownDevice,
    UnknownMetric,
    WriteOutcomeUnknown,
    WriteRejected,
)
from app.gateway.base import (
    ActionOutcome,
    DeviceState,
    DeviceStatus,
    MetricReading,
    StaleReason,
    WriteOutcome,
)
from app.gateway.vendor import SlaveDiscovery, TransportInfo
from app.observability.stats import Histogram
from app.protocol.frames import Frame
from app.protocol.slave_data import decode_slave_data
from app.protocol.types import Command, DataType, FrameKind
from app.protocol.values import ScalarValue, decode_string, decode_value, encode_value
from app.scheduling.budget import BudgetHandle
from app.scheduling.heartbeat import Heartbeat
from app.scheduling.periodic import (
    FRESH_WINDOW_FACTOR,
    REFRESH_AFTER_FACTOR,
    PeriodicManager,
)
from app.scheduling.retry import RetryConfig, execute_with_retry
from app.scheduling.serializer import AccessSerializer
from app.scheduling.singleflight import SingleFlight
from app.transport.endpoint import TransportEndpoint
from app.transport.types import (
    TransactionOrigin,
    TransactionRequest,
    TransactionResult,
    make_frame,
)

log = logging.getLogger(__name__)
MAX_SLAVES = 31  # network ids the plant network can hold (Requirement 18.10)
_INVERTER_NAME_OBJECT_ID = 0xEBC62737
REFRESH_MAX_PER_CYCLE = 8  # reads per refresh cycle and device (Requirement 17.28)

_STALE_REASONS: tuple[tuple[type[DeviceApiError], StaleReason], ...] = (
    (DeviceMaintenance, StaleReason.DEVICE_MAINTENANCE),
    (DeviceTimeout, StaleReason.DEVICE_TIMEOUT),
    (DeviceUnreachable, StaleReason.DEVICE_UNREACHABLE),
    (QueueTimeout, StaleReason.QUEUE_TIMEOUT),
    (ProtocolError, StaleReason.PROTOCOL_ERROR),
)


def stale_reason_for(error: DeviceApiError) -> StaleReason | None:
    """Failures that may be answered from the cache; rate limits and shutdown never are."""
    for kind, reason in _STALE_REASONS:
        if isinstance(error, kind):
            return reason
    return None


@dataclass(slots=True)
class DeviceBinding:
    """Runtime parts of one device; the endpoint and serializer are shared by its device group."""

    entry: DeviceEntry
    endpoint: TransportEndpoint
    serializer: AccessSerializer
    heartbeat: Heartbeat | None = None
    periodic: PeriodicManager | None = None
    last_heartbeat_at: datetime | None = None
    reported_name: str | None = None  # the device's own name, read once at startup
    cache_hits: int = 0
    cache_misses: int = 0
    errors: int = 0
    durations: Histogram = field(default_factory=Histogram)


class RctGateway:
    def __init__(
        self,
        catalog: RegistryCatalog,
        cache: ValueStore,
        clock: Clock,
        allowlist: Allowlist,
        *,
        retry: RetryConfig | None = None,
        fresh_mode: FreshPeriodicMode = FreshPeriodicMode.OBSERVE,
        encoding: str = "utf-8",
        heartbeat_metric: str = "inverter_state",
        foreign_frame_threshold: int = 10,
        foreign_window_seconds: float = 60.0,
        singleflight: SingleFlight | None = None,
        slave_cache_ttl_seconds: float = 300.0,
        slave_stable_reads: int = 3,
        slave_max_reads: int = 40,
    ) -> None:
        self._catalog = catalog
        self._cache = cache
        self._clock = clock
        self._allowlist = allowlist
        self._retry = retry or RetryConfig()
        self._fresh_mode = fresh_mode
        self._encoding = encoding
        self._heartbeat_metric = heartbeat_metric
        self._foreign_threshold = foreign_frame_threshold
        self._foreign_window = foreign_window_seconds
        self._flight = singleflight or SingleFlight()
        self._slave_ttl = slave_cache_ttl_seconds
        self._slave_stable = slave_stable_reads
        self._slave_max = slave_max_reads
        self._slave_cache: dict[str, tuple[float, SlaveDiscovery]] = {}
        self._slave_locks: dict[str, asyncio.Lock] = {}
        self._write_locks: dict[tuple[str, int], asyncio.Lock] = {}  # one write+readback per object id
        self._devices: dict[str, DeviceBinding] = {}
        self._by_address: dict[tuple[EndpointKey, int | None], str] = {}
        cache.extend_ttl_when(self._fresh_window)
        self._refresh_attempts: dict[tuple[str, str], float] = {}

    def _fresh_window(self, key: tuple[str, str]) -> float | None:
        """Cache hook: bounded freshness window of a live periodic registration (Requirement 17.29)."""
        binding = self._devices.get(key[0])
        if binding is None or binding.periodic is None:
            return None
        try:
            registered = binding.periodic.is_registered(self._catalog.object_entry(key[1]).object_id)
        except UnknownMetric:
            return None
        return FRESH_WINDOW_FACTOR * binding.periodic.interval_seconds if registered else None

    async def refresh_stale_periodic(self, device_id: str, *, limit: int = REFRESH_MAX_PER_CYCLE) -> int:
        """Read registered values the device stopped pushing; returns the number of reads sent.

        Budget-exempt like the heartbeat (the work budget protects caller access), bounded by ``limit``
        per call; yields to queued callers and stops after two consecutive failures.
        """
        binding = self._device(device_id)
        periodic = binding.periodic
        if periodic is None or not periodic.available:
            return 0
        older_than = REFRESH_AFTER_FACTOR * periodic.interval_seconds
        now = self._clock.monotonic()
        due: list[tuple[float, RegistryEntry]] = []
        for object_id in periodic.registered_object_ids:
            entry = self._catalog.entry_by_object_id(object_id)
            if entry is None:
                continue
            key = (device_id, entry.name)
            cached = self._cache.get(key)
            age = age_seconds(cached, now) if cached is not None else math.inf
            if age > older_than and now - self._refresh_attempts.get(key, -math.inf) >= periodic.interval_seconds:
                due.append((age, entry))
        due.sort(key=lambda item: item[0], reverse=True)
        sent = failures = 0
        for _, entry in due[:limit]:
            parked = binding.endpoint.maintenance() and not binding.endpoint.probe_due()
            if binding.serializer.queue_length() > 0 or parked:
                break  # callers and probes go first; the next cycle continues
            key = (device_id, entry.name)
            self._refresh_attempts[key] = self._clock.monotonic()
            sent += 1
            if await self._read_into_cache(binding, entry, key, TransactionOrigin.SYSTEM_READ) is not None:
                failures = 0
            elif (failures := failures + 1) >= 2:
                break
        if sent:
            log.debug("Refreshed %d stale periodic value(s) on device %s (%d due)", sent, device_id, len(due))
        return sent

    # ---- wiring ----------------------------------------------------------------------------
    def add_device(self, binding: DeviceBinding) -> None:
        self._devices[binding.entry.device_id] = binding
        self._by_address[(binding.entry.key.endpoint, binding.entry.network_id)] = binding.entry.device_id

    def replace_devices(self, bindings: list[DeviceBinding]) -> None:
        """Swap the whole device/address map for a freshly built graph (live device-list reload)."""
        self._devices.clear()
        self._by_address.clear()
        for binding in bindings:
            self.add_device(binding)

    def handler(self, endpoint: TransportEndpoint):
        """Transaction handler for the endpoint's AccessSerializer (retry rules per request kind)."""

        async def run(request: TransactionRequest) -> TransactionResult:
            # Every transaction passes here - caller reads and writes, heartbeat and periodic alike -
            # so the histogram of Requirement 20.6 also covers an otherwise idle system. Cache hits
            # short-circuit inside the serializer and never reach this handler, so they stay out.
            binding = self._binding_for(request.device_key)
            started = self._clock.monotonic()
            result = await execute_with_retry(endpoint, request, self._retry, self._clock)
            if binding is not None:
                binding.durations.observe(self._clock.monotonic() - started)
                if result.error is not None:
                    binding.errors += 1
            return result

        return run

    def _binding_for(self, device_key: DeviceKey) -> DeviceBinding | None:
        """Device behind a transaction request; unknown addresses are not counted anywhere."""
        device_id = self._by_address.get((device_key.endpoint, device_key.network_id))
        return None if device_id is None else self._devices.get(device_id)

    def cache_hit(self, request: TransactionRequest) -> bool:
        """Serializer hook: re-check the cache right before the hand-over to the send gate."""
        if request.cache_key is None:
            return False
        entry = self._cache.get(request.cache_key)
        return entry is not None and self._classify(request.cache_key, entry) is CacheFreshness.FRESH

    def value_sink(self, endpoint_key: EndpointKey) -> Callable[[Frame, FrameKind], None]:
        """Endpoint callback: periodic values go into the cache; transaction replies are read-path only."""

        def sink(frame: Frame, kind: FrameKind) -> None:
            if kind is not FrameKind.PERIODIC_VALUE:
                return  # a write acknowledgement must never populate the cache (Requirement 9.13)
            device_id = self._by_address.get((endpoint_key, frame.plant_address))
            entry = self._catalog.entry_by_object_id(frame.object_id)
            if device_id is None or entry is None or entry.data_type is DataType.STRUCT:
                return
            try:
                value = self._decode(entry, frame.payload)
            except DeviceApiError:
                log.debug("Dropped undecodable periodic value for metric %s", entry.name)
                return
            self._cache.put(
                (device_id, entry.name),
                value,
                measured_at=self._clock.now(),
                received_monotonic=self._clock.monotonic(),
                origin="periodic",
            )

        return sink

    # ---- lookup ----------------------------------------------------------------------------
    def _device(self, device_id: str) -> DeviceBinding:
        binding = self._devices.get(device_id)
        if binding is None:
            raise UnknownDevice(device_id=device_id)
        return binding

    def _metric(self, name: str) -> RegistryEntry:
        if not self._catalog.exists(name):
            raise UnknownMetric(name=name)
        return self._catalog.object_entry(name)

    def _classify(self, key: tuple[str, str], entry: CacheEntry) -> CacheFreshness:
        return self._cache.classify(key, entry, now_monotonic=self._clock.monotonic())

    def _decode(self, entry: RegistryEntry, payload: bytes) -> ScalarValue:
        value = decode_value(entry.data_type, payload, byte_width=entry.byte_width, encoding=self._encoding)
        if isinstance(value, float) and not math.isfinite(value):
            raise ProtocolError("invalid_float", name=entry.name)
        if entry.scale != 1.0 and isinstance(value, float):
            value *= entry.scale
        return value

    # ---- reading ---------------------------------------------------------------------------
    def refund_budget(self, device_id: str, reservation: BudgetHandle | None) -> None:
        """Release every unit a request-local reservation still holds; a no-op once it is spent."""
        self._device(device_id)  # validates device_id the same way reserve_budget does
        if reservation is not None:
            reservation.release()

    def reserve_budget(self, device_id: str, count: int = 1) -> BudgetHandle | None:
        """Reserve a request-local batch of ``count`` units; raises BudgetExhausted.

        The returned handle belongs to the caller alone: pass it to this batch's ``read_metric``
        calls and to ``refund_budget`` when done. It is never shared across requests, so one
        request's refund can never consume units reserved by another (Requirement 6.10 to 6.12).
        """
        return self._device(device_id).serializer.reserve_budget(count)

    def _reading(
        self,
        entry: RegistryEntry,
        value: ScalarValue,
        *,
        measured_at: datetime,
        age: float,
        source: str,
        stale: bool,
        reason: StaleReason | None = None,
        freshness: str | None = None,
    ) -> MetricReading:
        label = entry.enum_labels.get(value) if isinstance(value, int) and not isinstance(value, bool) else None
        return MetricReading(
            entry.name,
            value,
            entry.unit,
            measured_at,
            age,
            source,  # type: ignore[arg-type]
            stale,
            reason,
            freshness,  # type: ignore[arg-type]
            label,
        )

    def _from_cache(
        self,
        entry: RegistryEntry,
        key: tuple[str, str],
        cached: CacheEntry,
        reason: StaleReason | None,
        freshness: str | None,
    ) -> MetricReading:
        age = age_seconds(cached, self._clock.monotonic())
        stale = self._classify(key, cached) is not CacheFreshness.FRESH
        return self._reading(
            entry,
            cached.value,
            measured_at=cached.measured_at,
            age=age,
            source="cache",
            stale=stale,
            reason=reason,
            freshness=freshness,
        )

    async def read_metric(
        self, device_id: str, name: str, *, fresh: bool, charge: BudgetHandle | None = None
    ) -> MetricReading:
        """``charge``: this request's own reservation (from ``reserve_budget``) for a prepaid batch read.

        ``None`` means pay-as-you-go: one unit is reserved here and charged to the caller directly.
        A prepaid ``charge`` that is already exhausted fails closed with ``BudgetExhausted`` instead
        of letting the transaction through unbudgeted.
        """
        binding = self._device(device_id)
        entry = self._metric(name)
        key = (device_id, name)
        if not fresh:
            cached = self._cache.get(key)
            if cached is not None and self._classify(key, cached) is CacheFreshness.FRESH:
                binding.cache_hits += 1
                return self._from_cache(entry, key, cached, None, None)
        binding.cache_misses += 1
        unit: BudgetHandle | None = None
        try:
            if fresh and self._fresh_mode is FreshPeriodicMode.REJECT and self._is_periodic(binding, entry):
                raise FreshNotAvailable(name=name)  # nothing taken from charge yet; the caller still owns it
            if fresh:
                if charge is not None:
                    unit = charge.take(1)
                    if not len(unit):
                        raise BudgetExhausted()  # fail closed: never run an unbudgeted transaction
                return await self._transact(binding, entry, key, fresh=True, charge=unit, pay=charge is None)
            # Only the caller that starts the flight pays; joiners ride on it for free.
            if not self._flight.inflight(key):
                unit = binding.serializer.reserve_budget(1)
            return await self._flight.run(key, lambda: self._transact(binding, entry, key, fresh=False, charge=unit))
        except DeviceApiError as exc:
            reason = stale_reason_for(exc)
            cached = self._cache.get(key)
            if reason is None or cached is None or self._classify(key, cached) is CacheFreshness.EXPIRED:
                raise
            return self._from_cache(entry, key, cached, reason, "cached" if fresh else None)

    async def read_system(self, device_id: str, name: str) -> MetricReading:
        """Uncached, budget-exempt read that preserves the concrete device error (REQ-084)."""
        binding = self._device(device_id)
        entry = self._metric(name)
        key = (device_id, name)
        request = self._read_request(binding, entry, TransactionOrigin.SYSTEM_READ, None)
        result = await binding.serializer.submit(request)
        if result.error is not None:
            raise result.error
        if result.frame is None:
            raise DeviceApiError("device_unreachable", name=name)
        value = self._decode(entry, result.frame.payload)
        now = self._clock.now()
        self._cache.put(key, value, measured_at=now, received_monotonic=self._clock.monotonic(), origin="transaction")
        return self._reading(
            entry, value, measured_at=now, age=0.0, source="device", stale=False, freshness="observed"
        )

    @staticmethod
    def _is_periodic(binding: DeviceBinding, entry: RegistryEntry) -> bool:
        periodic = binding.periodic
        return periodic is not None and periodic.available and entry.object_id in periodic.object_ids

    def _read_request(
        self, binding: DeviceBinding, entry: RegistryEntry, origin: TransactionOrigin, key: tuple[str, str] | None
    ) -> TransactionRequest:
        frame = make_frame(binding.entry.network_id, Command.READ, entry.object_id)
        return TransactionRequest(
            binding.entry.key,
            frame,
            origin,
            "read",
            self._clock.now(),
            cache_key=key,
            recheck_cache=key is not None,
            idempotent=True,
        )

    async def _transact(
        self,
        binding: DeviceBinding,
        entry: RegistryEntry,
        key: tuple[str, str],
        *,
        fresh: bool,
        charge: BudgetHandle | None = None,
        pay: bool = False,
    ) -> MetricReading:
        """``charge``: unit reserved by the caller; ``pay``: reserve here, after the maintenance check."""
        extra: BudgetHandle | None = None
        try:
            if binding.endpoint.maintenance() and not binding.endpoint.probe_due():
                raise DeviceMaintenance()
            if pay:
                charge = binding.serializer.reserve_budget(1)
            request = self._read_request(binding, entry, TransactionOrigin.CALLER, None if fresh else key)
            request.charge = charge
            result = await binding.serializer.submit(request)  # duration and errors: see handler()
            if result.skipped_by_cache:  # the serializer already gave the unit back
                cached = self._cache.get(key)
                if cached is not None:
                    return self._from_cache(entry, key, cached, None, None)
                # The entry was invalidated meanwhile: the real read is charged one unit of its own.
                extra = binding.serializer.reserve_budget(1)
                request = self._read_request(binding, entry, TransactionOrigin.CALLER, None)
                request.charge = extra
                result = await binding.serializer.submit(request)
        finally:
            for handle in (charge, extra):
                if handle is not None:
                    handle.release()
        if result.error is not None:
            raise result.error  # already counted in handler()
        if result.frame is None:
            # A read without a response and without an error: only the read path can judge this a
            # failure, because a committed write legitimately returns no frame (Requirement 9.6).
            binding.errors += 1
            raise DeviceApiError()
        value = self._decode(entry, result.frame.payload)
        now = self._clock.now()
        self._cache.put(key, value, measured_at=now, received_monotonic=self._clock.monotonic(), origin="transaction")
        return self._reading(
            entry,
            value,
            measured_at=now,
            age=0.0,
            source="device",
            stale=False,
            freshness="observed" if fresh else None,
        )

    async def _read_into_cache(
        self,
        binding: DeviceBinding,
        entry: RegistryEntry,
        key: tuple[str, str],
        origin: TransactionOrigin,
        charge: BudgetHandle | None = None,
    ) -> tuple[ScalarValue, bytes] | None:
        """One uncached read whose result alone repopulates the cache (Requirement 9.13); None on failure."""
        try:
            request = self._read_request(binding, entry, origin, None)
            request.charge = charge
            result = await binding.serializer.submit(request)
            if result.error is not None or result.frame is None:
                return None
            value = self._decode(entry, result.frame.payload)
        except DeviceApiError:
            return None
        self._cache.put(
            key, value, measured_at=self._clock.now(), received_monotonic=self._clock.monotonic(), origin="transaction"
        )
        return value, result.frame.payload

    # ---- writing ---------------------------------------------------------------------------
    def _encode(self, entry: RegistryEntry, value: ScalarValue) -> bytes:
        try:
            # A JSON body of 80 parses to int, 80.0 to float; both name the same physical value, so
            # both have to be divided by the scale. bool is an int subclass and is never a scaled
            # number, so it must stay out of the division.
            if entry.scale != 1.0 and isinstance(value, int | float) and not isinstance(value, bool):
                value = value / entry.scale
            payload = encode_value(entry.data_type, value, byte_width=entry.byte_width, encoding=self._encoding)
            if len(payload) > 65527:  # largest LONG WRITE payload including a plant address
                raise WriteRejected("value_out_of_range")
            return payload
        except OverflowError:
            raise WriteRejected("value_out_of_range") from None
        except (ValueError, TypeError):
            raise WriteRejected("value_type_mismatch") from None

    async def _write(
        self, binding: DeviceBinding, entry: RegistryEntry, key: tuple[str, str], payload: bytes
    ) -> tuple[TransactionResult, tuple[ScalarValue, bytes] | None]:
        """Send the write, drop the cache entry and read back; raises only before anything was sent.

        Write and readback run under one per-object lock so a concurrent write cannot slip between them.
        """
        lock = self._write_locks.setdefault((binding.entry.device_id, entry.object_id), asyncio.Lock())
        async with lock:
            return await self._write_locked(binding, entry, key, payload)

    async def _write_locked(
        self, binding: DeviceBinding, entry: RegistryEntry, key: tuple[str, str], payload: bytes
    ) -> tuple[TransactionResult, tuple[ScalarValue, bytes] | None]:
        if binding.endpoint.maintenance():
            raise DeviceMaintenance()
        charge = binding.serializer.reserve_budget(2)  # write plus readback, all or nothing (Requirement 6.10)
        write_charge = charge.take(1) if charge is not None else None
        request = TransactionRequest(
            binding.entry.key,
            make_frame(binding.entry.network_id, Command.WRITE, entry.object_id, payload),
            TransactionOrigin.CALLER,
            "write",
            self._clock.now(),
            idempotent=entry.idempotent_write,
            is_action=entry.is_action,
            charge=write_charge,
        )
        try:
            try:
                result = await binding.serializer.submit(request)
            finally:
                self._cache.invalidate(key)  # also when the outcome is unclear (Requirement 9.12)
            if not result.ok and not result.committed:
                raise result.error or DeviceApiError()  # nothing left the send path: final and safe
            return result, await self._read_into_cache(binding, entry, key, TransactionOrigin.CALLER, charge)
        finally:
            for handle in (write_charge, charge):
                if handle is not None:
                    handle.release()  # no-op for whatever started

    def _written_value(self, entry: RegistryEntry, payload: bytes) -> ScalarValue:
        """Decode what was sent, so a float32-rounded or non-canonical bool readback compares as equal."""
        value = decode_value(entry.data_type, payload, byte_width=entry.byte_width, encoding=self._encoding)
        if entry.scale != 1.0 and isinstance(value, float):
            value *= entry.scale
        return value

    async def write_metric(self, device_id: str, name: str, value: ScalarValue) -> WriteOutcome:
        binding = self._device(device_id)
        entry = self._metric(name)
        self._allowlist.check(name, value, action=False)  # before the serializer is touched
        payload = self._encode(entry, value)
        log.info("Write requested: device=%s metric=%s", device_id, name)
        result, readback = await self._write(binding, entry, (device_id, name), payload)
        read_value = readback[0] if readback else None
        matches = readback is not None and (
            read_value == value
            if entry.data_type is DataType.STRING
            else read_value == self._written_value(entry, payload)
        )
        if not matches:
            raise WriteOutcomeUnknown(name=name, readback_value=read_value)
        return WriteOutcome(name, value, read_value, True, not result.ok, self._clock.now())

    async def trigger_action(self, device_id: str, name: str, value: ScalarValue) -> ActionOutcome:
        binding = self._device(device_id)
        entry = self._metric(name)
        self._allowlist.check(name, value, action=True)
        payload = self._encode(entry, value)
        log.warning("Action requested: device=%s object=0x%08X", device_id, entry.object_id)
        try:
            result, readback = await self._write(binding, entry, (device_id, name), payload)
        except DeviceApiError as exc:
            log.warning("Action not sent: device=%s object=0x%08X error=%s", device_id, entry.object_id, exc.code)
            raise
        read_value = readback[0] if readback else None
        log.warning(
            "Action finished: device=%s object=0x%08X acknowledged=%s readback_ok=%s",
            device_id,
            entry.object_id,
            result.ok,
            readback is not None,
        )
        # The device never answers WRITE and may reset the action value itself, so a readback of any
        # value proves only that the device is alive (Requirement 9.14); no readback means unknown.
        if readback is None:
            raise ActionOutcomeUnknown(name=name, readback_value=read_value)
        return ActionOutcome(name, value, read_value, self._clock.now())

    # ---- liveness and status ---------------------------------------------------------------
    async def read_inverter_name(self, device_id: str) -> str:
        """Read the device's own name through its existing serialized connection."""
        binding = self._device(device_id)
        request = TransactionRequest(
            binding.entry.key,
            make_frame(binding.entry.network_id, Command.READ, _INVERTER_NAME_OBJECT_ID),
            TransactionOrigin.HEARTBEAT,
            "read",
            self._clock.now(),
            idempotent=True,
        )
        result = await binding.serializer.submit(request)
        if result.error is not None:
            raise result.error
        if result.frame is None:
            raise DeviceUnreachable()
        return decode_string(result.frame.payload, self._encoding)[0]

    def heartbeat_read(self, device_id: str) -> Callable[[], Awaitable[bool]]:
        """Callable for ``Heartbeat``: one budget-exempt read of the heartbeat metric."""
        binding = self._device(device_id)
        entry = self._metric(self._heartbeat_metric)
        key = (device_id, entry.name)

        async def read() -> bool:
            if binding.endpoint.maintenance() and not binding.endpoint.probe_due():
                return False
            if await self._read_into_cache(binding, entry, key, TransactionOrigin.HEARTBEAT) is None:
                return False
            binding.last_heartbeat_at = self._clock.now()
            return True

        return read

    def set_reported_name(self, device_id: str, name: str) -> None:
        """Record the device's own name, read once at startup via ``read_inverter_name``."""
        self._device(device_id).reported_name = name

    def reported_name(self, device_id: str) -> str | None:
        return self._device(device_id).reported_name

    def set_allowlist(self, allowlist: Allowlist) -> None:
        self._allowlist = allowlist

    def cached_reading(self, device_id: str, name: str) -> tuple[ScalarValue, CacheFreshness] | None:
        """Cached value with its freshness; None when absent or expired."""
        key = (device_id, name)
        cached = self._cache.get(key)
        if cached is None:
            return None
        freshness = self._classify(key, cached)
        return None if freshness is CacheFreshness.EXPIRED else (cached.value, freshness)

    def cached_sample(self, device_id: str, name: str) -> tuple[ScalarValue, float, CacheFreshness] | None:
        """Cached value, its age and its freshness; None when the entry is absent or expired.

        ``cached_reading()`` answers the same question without the age; this variant exists for the
        Energy Manager's readings, which publish how old a value is. It reads the cache only and
        never queues a device transaction.
        """
        key = (device_id, name)
        cached = self._cache.get(key)
        if cached is None:
            return None
        freshness = self._classify(key, cached)
        if freshness is CacheFreshness.EXPIRED:
            return None
        return cached.value, age_seconds(cached, self._clock.monotonic()), freshness

    def device_status(self, device_id: str) -> DeviceStatus:
        binding = self._device(device_id)
        counters = binding.endpoint.counters_for(binding.entry.key)
        heartbeat = binding.heartbeat
        failures = heartbeat.consecutive_failures if heartbeat else 0
        if binding.endpoint.maintenance():
            state = DeviceState.MAINTENANCE
        elif heartbeat is None or (heartbeat.liveness_source is None and failures == 0):
            state = DeviceState.STARTING
        elif heartbeat.unreachable:
            state = DeviceState.UNREACHABLE
        elif failures > 0:
            state = DeviceState.DEGRADED
        else:
            state = DeviceState.OK
        endpoint_counters = binding.endpoint.counters
        foreign = (
            endpoint_counters.unexpected_in_window(self._clock.monotonic(), self._foreign_window)
            >= self._foreign_threshold
        )
        source = heartbeat.liveness_source if heartbeat else None
        return DeviceStatus(
            device_id,
            state,
            counters.last_success_at,
            binding.last_heartbeat_at,
            failures,
            binding.serializer.queue_length(),
            foreign,
            source.value if source else None,  # type: ignore[arg-type]
            counters.transactions,
            counters.failures,
            binding.cache_hits,
            binding.cache_misses,
        )

    # ---- vendor diagnostics (Requirement 18, 30) ---------------------------------------------
    def objects(self) -> list[RegistryEntry]:
        return list(self._catalog.entries())

    def transports(self) -> list[TransportInfo]:
        groups: dict[str, list[DeviceBinding]] = {}
        for binding in self._devices.values():
            groups.setdefault(binding.endpoint.endpoint_id, []).append(binding)
        result = []
        for endpoint_id, bindings in groups.items():
            endpoint = bindings[0].endpoint
            status, entry = endpoint.status(), bindings[0].entry
            result.append(
                TransportInfo(
                    endpoint_id,
                    entry.host,
                    entry.port,
                    tuple(b.entry.device_id for b in bindings),
                    tuple(b.entry.network_id for b in bindings if b.entry.network_id is not None),
                    status.counters.discarded_bytes,
                    status.counters.unexpected_frames,
                    status.lock_reason is not None,
                    status.lock_reason,
                    status.counters.last_frame_at,
                    {b.entry.device_id: b.periodic.registrations if b.periodic else 0 for b in bindings},
                    {b.entry.device_id: bool(b.periodic and b.periodic.available) for b in bindings},
                )
            )
        return result

    async def discover_slaves(self, device_id: str) -> SlaveDiscovery:
        binding = self._device(device_id)
        async with self._slave_locks.setdefault(device_id, asyncio.Lock()):
            cached = self._slave_cache.get(device_id)
            if cached is not None and self._clock.monotonic() < cached[0]:
                return cached[1]
            result = await self._collect_slaves(binding)
            if result.complete and self._slave_ttl > 0:
                self._slave_cache[device_id] = (self._clock.monotonic() + self._slave_ttl, result)
            return result

    async def _collect_slaves(self, binding: DeviceBinding) -> SlaveDiscovery:
        entry = next((e for e in self._catalog.entries() if e.struct is not None), None)
        if entry is None:  # rejected at startup already; a mapped error beats a bare StopIteration
            raise ConfigError("invalid_object_registry", detail="the slave_data registry object is missing")
        found: dict[int, object] = {}
        stable = 0
        for _ in range(self._slave_max):
            try:
                if binding.endpoint.maintenance() and not binding.endpoint.probe_due():
                    raise DeviceMaintenance()
                charge = binding.serializer.reserve_budget(1)
                request = self._read_request(binding, entry, TransactionOrigin.CALLER, None)
                request.charge = charge
                try:
                    result = await binding.serializer.submit(request)
                finally:
                    if charge is not None:
                        charge.release()
                if result.error is not None:
                    raise result.error
                if result.frame is None:
                    raise DeviceApiError()
                data = decode_slave_data(result.frame.payload, encoding=self._encoding)
            except DeviceApiError as exc:
                return SlaveDiscovery(tuple(found.values()), False, exc.code)  # type: ignore[arg-type]
            stable = 0 if data.network_id not in found else stable + 1
            found.setdefault(data.network_id, data)
            if stable >= self._slave_stable or len(found) >= MAX_SLAVES:
                break
        # Requirement 18.10 ends the reads at the latest after the configured maximum, and 18.12
        # reserves complete=false for a read that *failed*; reaching the limit is an error-free end
        # and therefore complete=true per Requirement 18.13.
        return SlaveDiscovery(tuple(found.values()), True)  # type: ignore[arg-type]
