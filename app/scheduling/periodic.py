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
from app.errors import ConfigError, DeviceApiError
from app.protocol.types import Command, DataType
from app.protocol.values import decode_value, encode_value
from app.scheduling.serializer import AccessSerializer
from app.transport.endpoint import EndpointState, TransportEndpoint
from app.transport.types import TransactionOrigin, TransactionRequest, make_frame

log = logging.getLogger(__name__)

PAS_PERIOD_OBJECT_ID = 0x9C8FE559
SYSTEM_WRITABLE_OBJECT_IDS: frozenset[int] = frozenset({PAS_PERIOD_OBJECT_ID})
MAX_PERIODIC_PER_DEVICE = 64
RETRY_BASE_SECONDS = 10.0  # after a failed setup: 10, 20, 40 ... seconds up to the cap
RETRY_MAX_SECONDS = 300.0
WARN_AFTER_FAILURES = 3  # consecutive failed setups before the retry log escalates from INFO to WARNING
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
        self._consecutive_failures = 0
        self._failed_at = 0.0  # monotonic time and connection epoch of the latest failed setup
        self._failed_epoch = -1
        self.last_failure: str | None = None  # reason of the latest failed setup, for the retry log
        self._registered: set[int] = set()
        self._registered_epoch = -1
        self._setup_lock = asyncio.Lock()  # one setup at a time: it rewrites the device-global pas.period

    def is_registered(self, object_id: int) -> bool:
        """True while the registration holds on the connection it was made on."""
        return (
            self._endpoint.state is EndpointState.CONNECTED
            and self._endpoint.connection_epoch == self._registered_epoch
            and object_id in self._registered
        )

    @property
    def live(self) -> bool:
        """Registration complete and still bound to the current connection (False after a reconnect)."""
        return (
            self.available
            and self._endpoint.state is EndpointState.CONNECTED
            and self._endpoint.connection_epoch == self._epoch
        )

    @property
    def consecutive_failures(self) -> int:
        """Failed setup rounds since the last success; 0 while the registration holds."""
        return self._consecutive_failures

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
        if (
            self.available
            and self._endpoint.state is EndpointState.CONNECTED
            and self._endpoint.connection_epoch == self._epoch
        ):
            return True
        now = self._clock.monotonic()
        if not self.available and now < self._retry_at:
            # Every attempt rewrites the device-global pas.period: back off. A connection that was
            # re-established since the failure ends the long wait early, but not before the base delay.
            reconnected = self._endpoint.connection_epoch != self._failed_epoch
            if not reconnected or now < self._failed_at + RETRY_BASE_SECONDS:
                return False
        if self._setup_lock.locked():
            return self.available
        async with self._setup_lock:
            ok = await self.setup()
        if ok:
            if self._consecutive_failures:
                log.info(
                    "Periodic reads set up successfully after %d failed attempt(s)", self._consecutive_failures
                )
            self._consecutive_failures = 0
            self._retry_delay = RETRY_BASE_SECONDS
        else:
            self._consecutive_failures += 1
            self._failed_at = self._clock.monotonic()
            self._failed_epoch = self._endpoint.connection_epoch
            self._retry_at = self._failed_at + self._retry_delay
            # A single failure is usually a transient connection hiccup that the retry resolves
            # by itself; only a persistent failure is worth a WARNING.
            persistent = self._consecutive_failures >= WARN_AFTER_FAILURES
            log.log(
                logging.WARNING if persistent else logging.INFO,
                "Setup of periodic reads failed (attempt %d%s): %s. Next attempt in %.0f s.",
                self._consecutive_failures,
                ", device still not reachable or unstable" if persistent else "",
                self._describe_failure(),
                self._retry_delay,
            )
            self._retry_delay = min(self._retry_delay * 2, RETRY_MAX_SECONDS)
        return ok

    def _describe_failure(self) -> str:
        if self.last_failure == "connection replaced during registration":
            return (
                "the connection to the device was interrupted and re-established while the "
                "measurements were being registered, so the registration restarts from scratch"
            )
        return self.last_failure or "unknown reason"

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

    def _rollback_registrations(self) -> None:
        """Drop every endpoint registration this setup round made and reset the per-round state."""
        self._endpoint.unregister_all_periodic(self._key)
        self._registered.clear()
        self.registrations = 0
        self.available = False

    async def setup(self) -> bool:
        self._endpoint.unregister_all_periodic(self._key)
        self.available, self.registrations = False, 0
        self._registered.clear()
        self.last_failure = None
        try:
            result = await self._serializer.submit(self._pas_write(self._interval, TransactionOrigin.SYSTEM_WRITE))
            if not result.ok:
                self.period_enabled = self.period_enabled or result.committed
                if not (result.committed and await self._confirmed_by_readback(self._interval)):
                    self.last_failure = f"pas.period not confirmed (write error: {result.error!r})"
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
                    self.last_failure = "connection replaced during registration"
                    return False
                # Success means a value frame for this object id was actually observed: the device
                # answers the registration with its first periodic value immediately.
                if not result.ok:
                    log.warning("Periodic registration failed: no value frame for object 0x%08x", object_id)
                    self._endpoint.unregister_periodic(self._key, object_id)
                    continue
                self._registered.add(object_id)
                self.registrations += 1
        except DeviceApiError as exc:
            # A throw past the loop above leaves the routes already registered on the endpoint; roll
            # them back so a failed setup never leaves available == False while the demux still routes.
            self._rollback_registrations()
            if exc.code != "not_ready":
                self.last_failure = f"{type(exc).__name__}: {exc}"
                log.warning("Periodic setup failed: %s", self.last_failure, exc_info=True)
                return False
            # The serializer stopped accepting work (shutdown): expected, not a device fault.
            self.last_failure = "device access is shutting down"
            log.info("Periodic setup aborted: device access is shutting down")
            return False
        except Exception as exc:  # the periodic feature must never disturb plain reads
            self._rollback_registrations()
            self.last_failure = f"{type(exc).__name__}: {exc}"
            log.warning("Periodic setup failed: %s", self.last_failure, exc_info=True)
            return False
        self._registered_epoch = epoch
        self._epoch = epoch
        self.available = self.registrations == len(self._object_ids)
        if self.available:
            log.info("Periodic reads active: %d of %d registered", self.registrations, len(self._object_ids))
        else:
            self.last_failure = f"{self.registrations} of {len(self._object_ids)} objects registered"
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
        if sent:
            self.period_enabled = False
        return sent
