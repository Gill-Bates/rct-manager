#!/usr/bin/env python3
#
# app/scheduling/periodic.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Periodic read management: pas.period plus one READ PERIODICALLY request per object id."""

import asyncio
import logging
from collections.abc import Sequence

from app.clock import Clock
from app.config import DeviceKey
from app.errors import ConfigError
from app.protocol.types import Command, DataType
from app.protocol.values import decode_value, encode_value
from app.scheduling.serializer import AccessSerializer
from app.transport.endpoint import TransportEndpoint
from app.transport.types import TransactionOrigin, TransactionRequest, make_frame

log = logging.getLogger(__name__)

PAS_PERIOD_OBJECT_ID = 0x9C8FE559
SYSTEM_WRITABLE_OBJECT_IDS: frozenset[int] = frozenset({PAS_PERIOD_OBJECT_ID})
MAX_PERIODIC_PER_DEVICE = 64
RETRY_BASE_SECONDS = 10.0  # after a failed setup: 10, 20, 40 ... seconds up to the cap
RETRY_MAX_SECONDS = 300.0
FRESH_WINDOW_FACTOR = 3  # a registered value counts as fresh for this many pas.period (Requirement 17.29)
REFRESH_AFTER_FACTOR = 2  # a registered value not updated for this many pas.period is read once (17.27)
READBACK_TIMEOUT_SECONDS = 1.0  # shutdown readback: bounded, the shutdown deadline still applies


class PeriodicManager:
    """Per device; re-registers after every reconnect (Requirement 17.11)."""

    def __init__(
        self,
        endpoint: TransportEndpoint,
        serializer: AccessSerializer,
        device_key: DeviceKey,
        object_ids: Sequence[int],
        interval_seconds: int,
        clock: Clock,
    ) -> None:
        if len(set(object_ids)) > MAX_PERIODIC_PER_DEVICE:
            raise ConfigError("too_many_periodic_metrics")
        self._endpoint = endpoint
        self._serializer = serializer
        self._key = device_key
        self._object_ids = list(dict.fromkeys(object_ids))  # one request per object id
        self._interval = interval_seconds
        self._clock = clock
        self._epoch = -1
        self.available = False
        self.registrations = 0
        # "pas.period is set" and "N objects registered" are separate states: the interval must be
        # reset on shutdown even when no registration succeeded.
        self.period_enabled = False
        self._retry_delay = RETRY_BASE_SECONDS
        self._retry_at = 0.0  # monotonic; no new setup before this point after a failure
        self._registered: set[int] = set()
        self._registered_epoch = -1
        self._setup_lock = asyncio.Lock()  # one setup at a time: it rewrites the device-global pas.period

    def is_registered(self, object_id: int) -> bool:
        """True while the registration holds on the connection it was made on."""
        return object_id in self._registered and self._endpoint.connection_epoch == self._registered_epoch

    @property
    def interval_seconds(self) -> int:
        return self._interval

    @property
    def registered_object_ids(self) -> tuple[int, ...]:
        """Object ids whose registration holds on the live connection, in registration order."""
        return tuple(o for o in self._object_ids if self.is_registered(o))

    @property
    def object_ids(self) -> tuple[int, ...]:
        return tuple(self._object_ids)

    def _pas_write(self, seconds: int, origin: TransactionOrigin) -> TransactionRequest:
        payload = encode_value(DataType.UINT32, seconds)
        frame = make_frame(self._key.network_id, Command.WRITE, PAS_PERIOD_OBJECT_ID, payload)
        return TransactionRequest(
            self._key, frame, origin, "write", self._clock.now(), idempotent=True
        )  # pre-Commit_Point retries are safe; after it the retry layer never repeats

    async def ensure(self) -> bool:
        """(Re-)register when the connection changed since the last setup."""
        if self.available and self._endpoint.connection_epoch == self._epoch:
            return True
        if not self.available and self._clock.monotonic() < self._retry_at:
            return False  # every attempt rewrites the device-global pas.period: back off
        if self._setup_lock.locked():
            return self.available
        async with self._setup_lock:
            ok = await self.setup()
        if ok:
            self._retry_delay = RETRY_BASE_SECONDS
        else:
            self._retry_at = self._clock.monotonic() + self._retry_delay
            log.warning("Periodic setup failed; next attempt in %.0f s", self._retry_delay)
            self._retry_delay = min(self._retry_delay * 2, RETRY_MAX_SECONDS)
        return ok

    def _pas_read(self, origin: TransactionOrigin) -> TransactionRequest:
        frame = make_frame(self._key.network_id, Command.READ, PAS_PERIOD_OBJECT_ID)
        return TransactionRequest(self._key, frame, origin, "read", self._clock.now(), idempotent=True)

    async def _confirmed_by_readback(self, seconds: int, timeout: float | None = None) -> bool:
        """The device never answers WRITE (design.md); the written value is confirmed by a READ."""
        if timeout is None:
            result = await self._serializer.submit(self._pas_read(TransactionOrigin.SYSTEM_WRITE))
        else:
            result = await self._endpoint.execute(self._pas_read(TransactionOrigin.SHUTDOWN), response_timeout=timeout)
        if not result.ok or result.frame is None:
            return False
        try:
            return decode_value(DataType.UINT32, result.frame.payload) == seconds
        except ValueError:
            return False

    async def setup(self) -> bool:
        self._endpoint.unregister_all_periodic(self._key)
        self.available, self.registrations = False, 0
        self._registered.clear()
        try:
            result = await self._serializer.submit(self._pas_write(self._interval, TransactionOrigin.SYSTEM_WRITE))
            if not result.ok:
                self.period_enabled = result.committed
                if not (result.committed and await self._confirmed_by_readback(self._interval)):
                    log.warning("Periodic reads unavailable for device (pas.period not confirmed)")
                    return False
                log.debug("pas.period confirmed by readback (device does not answer WRITE)")
            self.period_enabled = True
            # One setup round belongs to exactly one connection; a reconnect during the READ
            # PERIODICALLY loop below must restart it rather than mix registrations made on
            # different connections. The baseline is taken only now, once the
            # confirmed pas.period write has established the connection the loop runs on; taking
            # it before that write would always "change" on a first-ever connect.
            epoch = self._endpoint.connection_epoch
            for object_id in self._object_ids:
                request = TransactionRequest(
                    self._key,
                    make_frame(self._key.network_id, Command.READ_PERIODICALLY, object_id),
                    TransactionOrigin.SYSTEM_WRITE,
                    "read",
                    self._clock.now(),
                )
                # Registered first, so the immediate first value also reaches the cache (Requirement 3.6).
                self._endpoint.register_periodic(self._key, object_id)
                result = await self._serializer.submit(request)
                if self._endpoint.connection_epoch != epoch:
                    # The connection was replaced mid-registration: registrations already made on it
                    # are gone, and any made here belong to the old epoch, not the live one.
                    self._endpoint.unregister_all_periodic(self._key)
                    self._registered.clear()
                    self.registrations = 0
                    return False
                # Success means a value frame for this object id was actually observed: the device
                # answers the registration with its first periodic value immediately.
                if not result.ok:
                    log.warning("Periodic registration failed: no value frame for object 0x%08x", object_id)
                    self._endpoint.unregister_periodic(self._key, object_id)
                    continue
                self._registered.add(object_id)
                self.registrations += 1
        except Exception as exc:  # noqa: BLE001  # the periodic feature must never disturb plain reads
            log.warning("Periodic setup failed: %s", type(exc).__name__)
            return False
        self._registered_epoch = epoch
        self._epoch = epoch
        self.available = self.registrations == len(self._object_ids)
        if self.available:
            log.info("Periodic reads active: %d of %d registered", self.registrations, len(self._object_ids))
        else:
            log.warning("Periodic reads partial: %d of %d registered", self.registrations, len(self._object_ids))
        return self.available

    async def teardown(self) -> bool:
        """Write pas.period = 0 straight through the endpoint and send gate (shutdown phase)."""
        if not self.period_enabled:
            return True
        result = await self._endpoint.execute(self._pas_write(0, TransactionOrigin.SHUTDOWN))
        sent = result.ok or result.committed  # a committed write counts as sent; the device owns the outcome
        if sent and not result.ok:
            try:
                if await self._confirmed_by_readback(0, READBACK_TIMEOUT_SECONDS):
                    log.debug("pas.period reset confirmed by readback")
                else:
                    log.debug("pas.period reset sent but not confirmed")
            except Exception as exc:  # noqa: BLE001  # a failing readback must not fail the shutdown
                log.debug("pas.period reset sent, readback skipped: %s", type(exc).__name__)
        self._endpoint.unregister_all_periodic(self._key)
        self._registered.clear()
        self.registrations = 0
        self.available = False
        self.period_enabled = False
        return sent
