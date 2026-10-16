#!/usr/bin/env python3
#
# app/transport/receiver.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Receive path: the sole reader of a connection and sole user of its StreamParser."""

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from app.protocol.stream import StreamParser
from app.protocol.types import FrameKind
from app.transport.counters import EndpointCounters
from app.transport.demux import Demultiplexer
from app.transport.noise import CrcLogRateLimiter, StreamNoiseMonitor

log = logging.getLogger(__name__)


class ReceiverExit(StrEnum):
    EOF = "eof"
    BUFFER_OVERFLOW = "buffer_overflow"  # Requirement 2.11
    UNEXPECTED_FLOOD = "unexpected_flood"  # Requirement 3.11
    BOOTLOADER = "bootloader"  # connection is rebuilt by the probe after the cooldown (Requirement 8.13)


class ArrivalLedger:
    """Tracks, per absolute stream offset, the monotonic instant its bytes entered the reader.

    A timestamp taken after the caller's read call returns is too late: bytes may already have
    sat in the reader's internal buffer, delivered by ``data_received``, before the caller got
    around to consuming them. The arrival instant has to be recorded at the point the transport
    hands bytes to the reader, which for ``asyncio.StreamReader`` is ``feed_data``.

    Design choice (deliberate, kept over a protocol rework): the only public seam that records at
    that point without touching the injectable ``Connector`` contract or the single
    ``asyncio.open_connection`` call site is to wrap the specific reader instance's ``feed_data``.
    A ``StreamReaderProtocol`` subclass that records in ``data_received`` would record at the same
    point, but it is reachable only through ``loop.create_connection`` with a custom protocol
    factory, which would change the ``Connector`` type, its default, and every test connector (the
    fakes deliver bytes with ``reader.feed_data`` on a plain ``asyncio.StreamReader``). That rework
    is broad and would move the timing out of the path the test suite exercises, so in this
    safety-critical transport it is not worth the risk; the instance wrap is the smaller change.

    ``feed_data`` is a documented method of ``asyncio.StreamReader`` but overriding it per instance
    relies on the class permitting instance attributes (no ``__slots__``). The constructor asserts
    the override took effect and raises ``RuntimeError`` otherwise, so a future interpreter that
    stops honouring it fails loudly here instead of silently reintroducing the arrival-time race.
    CI pins the interpreter (see pyproject ``requires-python``); a CPython bump re-runs this guard.
    """

    def __init__(self, reader: asyncio.StreamReader, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        # (arrival, absolute offset one past the chunk's last byte), oldest first.
        self._chunks: deque[tuple[float, int]] = deque()
        self._fed = 0
        original = reader.feed_data

        def tracked_feed_data(data: bytes) -> None:
            if data:
                self._fed += len(data)
                self._chunks.append((monotonic(), self._fed))
            original(data)

        try:
            reader.feed_data = tracked_feed_data  # type: ignore[method-assign]  # instance-level wrap; the transport stays the sole caller
        except AttributeError as exc:
            # A future StreamReader with __slots__ would reject the attribute outright.
            raise RuntimeError("ArrivalLedger could not install its feed_data wrapper") from exc
        if reader.feed_data is not tracked_feed_data:
            # The assignment was accepted but did not take (e.g. a data descriptor shadowing it).
            raise RuntimeError("ArrivalLedger could not install its feed_data wrapper")

    def arrival_of(self, offset: int) -> float:
        """Arrival instant of the byte at absolute stream ``offset``.

        Callers query strictly increasing offsets, so records before ``offset`` are dropped here.
        """
        self._release(offset)
        return self._chunks[0][0] if self._chunks else self._monotonic()

    def release(self, consumed: int) -> None:
        """Forget records for the first ``consumed`` bytes, which the parser has fully handled."""
        self._release(consumed)

    def _release(self, offset: int) -> None:
        while self._chunks and self._chunks[0][1] <= offset:
            self._chunks.popleft()


class Receiver:
    def __init__(
        self,
        *,
        parser: StreamParser,
        demux: Demultiplexer,
        counters: EndpointCounters,
        monotonic: Callable[[], float],
        now: Callable[[], datetime],
        max_frame_bytes: int,
        unexpected_limit: int,
        unexpected_window_seconds: float,
        on_bootloader: Callable[[], None],
        crc_log: CrcLogRateLimiter | None = None,
        chunk_size: int = 4096,
    ) -> None:
        self._parser = parser
        self._demux = demux
        self._counters = counters
        self._monotonic = monotonic
        self._now = now
        self._max = max_frame_bytes
        self._limit = unexpected_limit
        self._window = unexpected_window_seconds
        self._on_bootloader = on_bootloader
        self._chunk = chunk_size
        self._magic_handled = False
        self._counted_discards = 0
        self._counted_crc = 0
        self._counted_framing = 0
        self._counted_unknown = 0
        self.noise = StreamNoiseMonitor(unexpected_window_seconds)
        # Endpoint-lifetime CRC-warning rate limiter; a fallback keeps a stand-alone Receiver (tests)
        # working. The endpoint passes its own long-lived instance so escalation survives reconnects.
        self._crc_log = crc_log if crc_log is not None else CrcLogRateLimiter(unexpected_window_seconds)
        self.unexpected_samples: deque[tuple[int, int, int | None]] = deque(maxlen=4)

    async def run(self, reader: asyncio.StreamReader, ledger: ArrivalLedger) -> ReceiverExit:
        """Read until the connection ends or must be rebuilt. Never drains the buffer wholesale.

        ``ledger`` must already be wrapping ``reader.feed_data`` before this call starts, so no
        chunk delivered between connecting and the receiver task actually running escapes it: the
        caller sets it up synchronously, with no intervening await, right after the connection is
        established.
        """
        while True:
            data = await reader.read(self._chunk)
            if not data:
                return ReceiverExit.EOF
            # Taken at the read boundary: good enough for noise rating and window bookkeeping,
            # which only need a coarse, monotonically advancing instant.
            now = self._monotonic()
            frames = self._parser.feed_located(data)
            stats = self._parser.stats
            self._counters.discarded_bytes += stats.discarded_bytes - self._counted_discards
            self._counted_discards = stats.discarded_bytes
            new_crc = stats.crc_errors - self._counted_crc
            self._counters.crc_errors += new_crc
            self._counters.framing_errors += stats.framing_errors - self._counted_framing
            self._counted_crc, self._counted_framing = stats.crc_errors, stats.framing_errors
            self._report_unknown_commands(now)
            self.noise.frames(now, len(frames))
            self.noise.evaluate(now)
            # CRC-mismatch frames are counted and DEBUG-logged by the parser; the receiver owns the
            # WARNING escalation and, via the endpoint's shared limiter, keeps it across reconnects.
            self._crc_log.frames(now, len(frames))
            self._crc_log.crc_errors(now, new_crc)
            self._crc_log.evaluate(now)
            magic = self._parser.bootloader_magic_seen and not self._magic_handled
            if magic:
                self._magic_handled = True
                self._on_bootloader()
            for frame, frame_end in frames:
                self._counters.last_frame_at = self._now()
                # Dated by its own last byte, so a batched or multi-read frame neither inherits
                # nor lends an arrival time (Requirement 3.2-3.8, per frame).
                received_monotonic = ledger.arrival_of(frame_end - 1)
                if self._demux.dispatch(frame, received_monotonic) is FrameKind.UNEXPECTED:
                    self.unexpected_samples.append((int(frame.command), frame.object_id, frame.plant_address))
            ledger.release(self._parser.consumed_bytes)
            if magic:
                return ReceiverExit.BOOTLOADER
            if self._parser.buffered_bytes > 2 * self._max:
                return ReceiverExit.BUFFER_OVERFLOW
            # Prunes the window of foreign responses too; only the lifetime counter is unbounded.
            self._counters.unexpected_in_window(now, self._window)
            if self._counters.flood_in_window(now, self._window) > self._limit:
                return ReceiverExit.UNEXPECTED_FLOOD

    def _report_unknown_commands(self, now: float) -> None:
        """Hand new unknown command bytes to the noise monitor; it owns the logging policy.

        The parser only counts them because it has to stay clock-free. Requirement 1.12 stays
        satisfied - logged and counted - and the bytes keep running into discarded_bytes.
        """
        stats = self._parser.stats
        new = stats.unknown_commands - self._counted_unknown
        if new <= 0:
            return
        self._counted_unknown = stats.unknown_commands
        self.noise.unknown(now, new, stats.last_unknown_command)
