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
from app.transport.noise import StreamNoiseMonitor

log = logging.getLogger(__name__)


class ReceiverExit(StrEnum):
    EOF = "eof"
    BUFFER_OVERFLOW = "buffer_overflow"  # Requirement 2.11
    UNEXPECTED_FLOOD = "unexpected_flood"  # Requirement 3.11
    BOOTLOADER = "bootloader"  # connection is rebuilt by the probe after the cooldown (Requirement 8.13)


class ArrivalLedger:
    """Tracks the monotonic instant each chunk entered the reader via ``feed_data``.

    A timestamp taken after the caller's read call returns is too late: bytes may already have
    sat in the reader's internal buffer, delivered by ``data_received``, before the caller got
    around to consuming them. Wrapping the specific reader instance's ``feed_data`` right after
    connecting captures the real arrival instant without a custom StreamReader/Protocol class, so
    the injectable ``Connector`` contract and the single ``asyncio.open_connection`` call site are
    both untouched.
    """

    def __init__(self, reader: asyncio.StreamReader, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._chunks: deque[tuple[float, int]] = deque()
        original = reader.feed_data

        def tracked_feed_data(data: bytes) -> None:
            if data:
                self._chunks.append((monotonic(), len(data)))
            original(data)

        reader.feed_data = tracked_feed_data  # instance-level wrap; the transport stays the sole caller
        if reader.feed_data is not tracked_feed_data:
            # Fails loudly if a future interpreter stops honoring instance-attribute overrides on
            # StreamReader, instead of silently reintroducing the arrival-time race.
            raise RuntimeError("ArrivalLedger could not install its feed_data wrapper")

    def earliest_arrival(self, consumed: int) -> float:
        """Earliest arrival time among ``consumed`` bytes just taken off the front of the buffer."""
        earliest: float | None = None
        while consumed > 0 and self._chunks:
            at, size = self._chunks[0]
            if earliest is None:
                earliest = at
            if size <= consumed:
                consumed -= size
                self._chunks.popleft()
            else:
                self._chunks[0] = (at, size - consumed)
                consumed = 0
        return earliest if earliest is not None else self._monotonic()


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
        self.unexpected_samples: deque[tuple[int, int, int | None]] = deque(maxlen=4)
        # Oldest arrival instant still unresolved in the parser buffer, carried across reads so a
        # frame assembled from an earlier, pre-commit chunk is not credited with a later chunk's
        # arrival time. Reset to None once the buffer fully drains.
        self._pending_since: float | None = None

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
            # Arrival instant from the ledger (see ArrivalLedger); a frame assembled across several
            # reads keeps the oldest pending instant instead of its last chunk's.
            chunk_earliest = ledger.earliest_arrival(len(data))
            received_monotonic = self._pending_since if self._pending_since is not None else chunk_earliest
            frames = self._parser.feed(data)
            if self._parser.buffered_bytes == 0:
                self._pending_since = None
            else:
                # Frames extracted this round resolved any earlier partial frame, so a leftover
                # tail began fresh with this chunk; otherwise the oldest pending instant persists.
                self._pending_since = chunk_earliest if frames else received_monotonic
            stats = self._parser.stats
            self._counters.discarded_bytes += stats.discarded_bytes - self._counted_discards
            self._counted_discards = stats.discarded_bytes
            self._counters.crc_errors += stats.crc_errors - self._counted_crc
            self._counters.framing_errors += stats.framing_errors - self._counted_framing
            self._counted_crc, self._counted_framing = stats.crc_errors, stats.framing_errors
            self._report_unknown_commands(now)
            self.noise.frames(now, len(frames))
            self.noise.evaluate(now)
            magic = self._parser.bootloader_magic_seen and not self._magic_handled
            if magic:
                self._magic_handled = True
                self._on_bootloader()
            for frame in frames:
                self._counters.last_frame_at = self._now()
                if self._demux.dispatch(frame, received_monotonic) is FrameKind.UNEXPECTED:
                    self.unexpected_samples.append((int(frame.command), frame.object_id, frame.plant_address))
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
