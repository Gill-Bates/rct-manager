#!/usr/bin/env python3
#
# app/transport/endpoint.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""TransportEndpoint: sole owner of the TCP connection to one transport endpoint."""

import asyncio
import contextlib
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from app.clock import Clock
from app.config import DeviceKey, EndpointKey
from app.errors import (
    DeviceApiError,
    DeviceMaintenance,
    DeviceTimeout,
    DeviceUnreachable,
)
from app.protocol.frames import Frame, encode_frame
from app.protocol.stream import StreamParser
from app.protocol.types import WRITE_COMMANDS, FrameKind
from app.transport.counters import DeviceCounters, EndpointCounters
from app.transport.demux import Demultiplexer, PendingTransaction
from app.transport.receiver import ArrivalLedger, Receiver, ReceiverExit
from app.transport.send_gate import SendGate
from app.transport.types import SendOutcome, TransactionRequest, TransactionResult

log = logging.getLogger(__name__)

type Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class EndpointState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    LOCKED = "locked"  # lock state after Bootloader_Magic


class LockReason(StrEnum):
    """Vendor-specific lock cause. Diagnostics only (Requirement 30.10)."""

    BOOTLOADER_MAGIC = "bootloader_magic"


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    connect_timeout_seconds: float = 3.0
    response_timeout_seconds: float = 5.0
    # The device does not answer WRITE at all (device test 2026-10-02), so writes wait only briefly.
    write_response_timeout_seconds: float = 0.3
    # Bounds writer.drain(); a stuck send would otherwise hold the transaction lock forever.
    send_timeout_seconds: float = 5.0
    min_interval: timedelta = timedelta(milliseconds=300)
    max_frame_bytes: int = 4096
    unexpected_frame_limit: int = 50
    unexpected_frame_window_seconds: float = 60.0
    bootloader_cooldown_seconds: float = 300.0
    device_ids: tuple[str, ...] = ()  # for log output only


@dataclass(frozen=True, slots=True)
class EndpointStatus:
    endpoint_id: str
    state: EndpointState
    lock_reason: LockReason | None
    connection_epoch: int
    periodic_registrations: int
    counters: EndpointCounters


class TransportEndpoint:
    """Sole owner of the TCP connection. ``execute`` is the only source of device load."""

    def __init__(
        self,
        endpoint_id: str,
        key: EndpointKey,
        config: EndpointConfig,
        clock: Clock,
        *,
        connector: Connector | None = None,
        on_value: Callable[[Frame, FrameKind], None] | None = None,
    ) -> None:
        self.endpoint_id = endpoint_id
        self._key = key
        self._cfg = config
        self._clock = clock
        # The only asyncio.open_connection call site in the project.
        self._connector: Connector = connector or asyncio.open_connection
        self.counters = EndpointCounters()
        self._device_counters: dict[DeviceKey, DeviceCounters] = {}
        self._demux = Demultiplexer(self.counters, clock.monotonic, on_value, self._on_periodic)
        self._gate = SendGate(config.min_interval, clock, config.send_timeout_seconds)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._tx_lock = asyncio.Lock()
        self._epoch = 0
        self._locked_until: float | None = None
        self._lock_reason: LockReason | None = None
        self._closed = False

    # ---- state -----------------------------------------------------------------------------
    @property
    def state(self) -> EndpointState:
        if self._lock_reason is not None:
            return EndpointState.LOCKED
        connected = self._writer is not None and not self._writer.is_closing()
        return EndpointState.CONNECTED if connected else EndpointState.DISCONNECTED

    @property
    def connection_epoch(self) -> int:
        return self._epoch

    def maintenance(self) -> bool:
        """Vendor-neutral view of the lock state."""
        return self._lock_reason is not None

    def probe_due(self) -> bool:
        """True while locked once the cooldown has passed, so one read may probe the endpoint."""
        return self._locked_until is not None and self._clock.monotonic() >= self._locked_until

    def status(self) -> EndpointStatus:
        return EndpointStatus(
            self.endpoint_id,
            self.state,
            self._lock_reason,
            self._epoch,
            self._demux.periodic_count(),
            self.counters,
        )

    def counters_for(self, device_key: DeviceKey) -> DeviceCounters:
        """Per-device counters; the endpoint-level ones stay for transport diagnostics."""
        return self._device_counters.setdefault(device_key, DeviceCounters())

    def _on_periodic(self, plant_address: int | None, now: float) -> None:
        # A periodic push is as much proof the device is alive and answering as a transaction is
        # (Heartbeat.tick() already treats it that way via last_periodic_monotonic); device_status()
        # only reads last_success_at, so without this a device driven purely by periodic reads shows
        # a last-connection timestamp frozen at whatever transaction or heartbeat last ran, even
        # while fresh values keep arriving.
        device = self.counters_for(DeviceKey(self._key, plant_address))
        device.last_periodic_monotonic = now
        device.last_success_monotonic = now
        device.last_success_at = self._clock.now()
        self.counters.last_periodic_monotonic = now
        self.counters.last_success_monotonic = now
        self.counters.last_success_at = device.last_success_at

    def register_periodic(self, device_key: DeviceKey, object_id: int) -> None:
        self._demux.register_periodic(device_key, object_id)

    def unregister_periodic(self, device_key: DeviceKey, object_id: int) -> None:
        self._demux.unregister_periodic(device_key, object_id)

    def unregister_all_periodic(self, device_key: DeviceKey) -> None:
        self._demux.unregister_all_periodic(device_key)

    # ---- lifecycle -------------------------------------------------------------------------
    async def start(self) -> None:
        """Try an initial connection; failure is logged and retried lazily by execute()."""
        try:
            await self._ensure_connected()
        except DeviceApiError as exc:
            log.warning("Initial connection to %s failed: %s", self.endpoint_id, exc.code)

    async def close(self) -> None:
        self._closed = True
        task, self._receiver_task = self._receiver_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._drop_connection(DeviceUnreachable("connection_closed"))

    def abort(self) -> None:
        """Hard close for the shutdown deadline: no awaiting, the transport is dropped immediately."""
        self._closed = True
        task, self._receiver_task = self._receiver_task, None
        if task is not None:
            task.cancel()
        writer, self._writer, self._reader = self._writer, None, None
        self._fail_pending(DeviceUnreachable("connection_closed"))
        if writer is not None:
            transport = getattr(writer, "transport", None)
            if transport is not None:
                transport.abort()
            else:
                writer.close()

    # ---- connection ------------------------------------------------------------------------
    async def _ensure_connected(self) -> None:
        async with self._connect_lock:
            if self._writer is not None and not self._writer.is_closing():
                return  # a second connection for the same endpoint is never opened
            if self._writer is not None:
                await self._drop_connection(DeviceUnreachable("connection_closed"))
            if self._closed:
                raise DeviceUnreachable("endpoint_closed")
            try:
                reader, writer = await asyncio.wait_for(
                    self._connector(self._key.host, self._key.port), self._cfg.connect_timeout_seconds
                )
            except TimeoutError:
                raise DeviceTimeout("connect_timeout") from None
            except OSError:
                raise DeviceUnreachable() from None
            try:
                # Part of the connection setup: a failure here must not leave the socket open.
                self._set_socket_options(writer)
            except OSError:
                writer.close()
                with contextlib.suppress(OSError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), 1.0)
                raise DeviceUnreachable("socket_setup_failed") from None
            # Wrapped synchronously, with no await since obtaining ``reader``, so no chunk that
            # arrives for this connection ever reaches the parser without a ledger entry.
            ledger = ArrivalLedger(reader, self._clock.monotonic)
            self._reader, self._writer = reader, writer
            self._epoch += 1
            parser = StreamParser(max_frame_bytes=self._cfg.max_frame_bytes)
            receiver = Receiver(
                parser=parser,
                demux=self._demux,
                counters=self.counters,
                monotonic=self._clock.monotonic,
                now=self._clock.now,
                max_frame_bytes=self._cfg.max_frame_bytes,
                unexpected_limit=self._cfg.unexpected_frame_limit,
                unexpected_window_seconds=self._cfg.unexpected_frame_window_seconds,
                on_bootloader=self._on_bootloader,
            )
            self._receiver_task = asyncio.create_task(self._receive(receiver, reader, writer, ledger))

    @staticmethod
    def _set_socket_options(writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    async def _receive(
        self, receiver: Receiver, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ledger: "ArrivalLedger"
    ) -> None:
        error: DeviceApiError = DeviceUnreachable("connection_lost")
        try:
            reason = await receiver.run(reader, ledger)
            if reason is ReceiverExit.BOOTLOADER:
                error = DeviceMaintenance()
        except OSError:
            reason = ReceiverExit.EOF
        except Exception:
            # A dying receive task would leave a dead connection that never reconnects.
            log.exception("Receive path of %s failed", self.endpoint_id)
            reason = ReceiverExit.EOF
        if self._writer is writer:  # not already replaced or closed
            if reason is ReceiverExit.UNEXPECTED_FLOOD:
                samples = ", ".join(
                    f"cmd=0x{command:02X} object=0x{object_id:08X} address={address if address is not None else '-'}"
                    for command, object_id, address in receiver.unexpected_samples
                )
                log.warning(
                    "Connection to %s ended: %s (%d unexpected non-response frames in %.0fs; last frames: %s)",
                    self.endpoint_id,
                    reason,
                    self.counters.flood_in_window(self._clock.monotonic(), self._cfg.unexpected_frame_window_seconds),
                    self._cfg.unexpected_frame_window_seconds,
                    samples,
                )
            else:
                log.info("Connection to %s ended: %s", self.endpoint_id, reason)
            self._receiver_task = None
            await self._drop_connection(error)

    def _fail_pending(self, error: DeviceApiError) -> None:
        pending = self._demux.pending
        if pending is not None and not pending.future.done():
            pending.future.set_exception(error)

    async def _drop_connection(self, error: DeviceApiError) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        self._fail_pending(error)
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(writer.wait_closed(), 1.0)

    # ---- lock state ------------------------------------------------------------------------
    def _on_bootloader(self) -> None:
        self._lock_reason = LockReason.BOOTLOADER_MAGIC
        self._locked_until = self._clock.monotonic() + self._cfg.bootloader_cooldown_seconds
        log.warning(
            "Bootloader magic seen: endpoint locked, devices=%s at=%s",
            ",".join(self._cfg.device_ids),
            self._clock.now().isoformat(),
        )

    def _may_probe(self, request: TransactionRequest) -> bool:
        return self.probe_due() and request.frame.command not in WRITE_COMMANDS

    # ---- transactions ----------------------------------------------------------------------
    def _fail(
        self, request: TransactionRequest, error: DeviceApiError, outcome: SendOutcome | None = None
    ) -> TransactionResult:
        self.counters.failures += 1
        self.counters_for(request.device_key).failures += 1
        return TransactionResult(outcome or SendOutcome(False, None, error), error=error)

    async def execute(self, request: TransactionRequest, *, response_timeout: float | None = None) -> TransactionResult:
        """Run exactly one attempt: send one request frame and wait for its response."""
        async with self._tx_lock:
            probing = False
            if self._lock_reason is not None:
                if not self._may_probe(request):
                    return self._fail(request, DeviceMaintenance())
                probing = True  # exactly one probe read after the cooldown
                self._locked_until = self._clock.monotonic() + self._cfg.bootloader_cooldown_seconds
            result = await self._attempt(request, response_timeout)
            if probing and result.ok:
                log.info("Endpoint %s answers again, lock released", self.endpoint_id)
                self._lock_reason = self._locked_until = None
            return result

    async def _attempt(self, request: TransactionRequest, response_timeout: float | None) -> TransactionResult:
        try:
            await self._ensure_connected()
        except DeviceApiError as exc:
            return self._fail(request, exc)
        writer = self._writer
        assert writer is not None
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        frame = request.frame

        def on_commit(sent_at, sent_monotonic: float) -> None:
            self.counters.last_send_at = sent_at
            self._demux.pending = PendingTransaction(frame.object_id, frame.plant_address, sent_monotonic, future)

        def precheck() -> Exception | None:
            return DeviceApiError("abandoned") if request.abandoned else None

        try:
            outcome = await self._gate.send(writer, encode_frame(frame), precheck=precheck, on_commit=on_commit)
            if isinstance(outcome.error, DeviceApiError):  # refused before the Commit_Point
                return self._fail(request, outcome.error, outcome)
            if outcome.error is not None:
                log.info("Connection to %s dropped: send failed (%s)", self.endpoint_id, type(outcome.error).__name__)
                await self._drop_connection(DeviceUnreachable("send_failed"))
                return self._fail(request, DeviceUnreachable("send_failed"), outcome)
            is_write = frame.command in WRITE_COMMANDS
            timeout = response_timeout
            if timeout is None:
                timeout = self._cfg.write_response_timeout_seconds if is_write else self._cfg.response_timeout_seconds
            try:
                response = await asyncio.wait_for(future, timeout)
            except TimeoutError:
                error = DeviceTimeout("response_timeout")
                if is_write and response_timeout is None:
                    # No answer to a WRITE is normal for this device; the connection stays so the
                    # read-back runs on it (Requirement 9.18). This is not a transport failure: the
                    # caller still sees TransactionResult.ok == False because no frame was received,
                    # but failure statistics and device state must not be dragged down by a routine
                    # write.
                    return TransactionResult(outcome)
                # The response is still in flight and carries no transaction id, so only a new
                # connection keeps it from answering the next transaction.
                log.info(
                    "Connection to %s dropped: no response to object 0x%08x within %.1f s; reconnecting",
                    self.endpoint_id, frame.object_id, timeout,
                )
                await self._drop_connection(error)
                return self._fail(request, error, outcome)
            except DeviceApiError as exc:
                return self._fail(request, exc, outcome)
        except asyncio.CancelledError:
            pending = self._demux.pending
            if pending is not None and pending.future is future:  # cancelled after the Commit_Point
                await self._drop_connection(DeviceUnreachable("transaction_cancelled"))
            raise
        finally:
            self._demux.pending = None
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # mark retrieved
        self.counters.transactions += 1
        self.counters.last_success_at = self._clock.now()
        self.counters.last_success_monotonic = self._clock.monotonic()
        device = self.counters_for(request.device_key)
        device.transactions += 1
        device.last_success_at = self.counters.last_success_at
        device.last_success_monotonic = self.counters.last_success_monotonic
        return TransactionResult(outcome, frame=response)
