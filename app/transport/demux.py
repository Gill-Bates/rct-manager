#!/usr/bin/env python3
#
# app/transport/demux.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Frame classification: transaction response, periodic value or unexpected frame."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.config import DeviceKey
from app.protocol.frames import Frame
from app.protocol.types import Command, FrameKind
from app.transport.counters import EndpointCounters

log = logging.getLogger(__name__)

_UNSOLICITED_LOG_INTERVAL_SECONDS = 60.0
_UNSOLICITED_LOG_MAX_KEYS = 256
_UNSOLICITED_LOG_LISTED = 12
RESPONSE_COMMANDS = frozenset({Command.RESPONSE, Command.LONG_RESPONSE, Command.RESPONSE_M, Command.LONG_RESPONSE_M})


@dataclass(frozen=True, slots=True)
class PendingTransaction:
    object_id: int
    plant_address: int | None
    sent_monotonic: float  # set at the Commit_Point, so only frames received after the send can answer
    future: asyncio.Future[Frame]


class Demultiplexer:
    """Per-endpoint classification from object id, network id and timing only (Requirement 3.4)."""

    def __init__(
        self,
        counters: EndpointCounters,
        monotonic: Callable[[], float],
        on_value: Callable[[Frame, FrameKind], None] | None = None,
        on_periodic: Callable[[int | None, float], None] | None = None,
    ) -> None:
        self._counters = counters
        self._monotonic = monotonic
        self._on_value = on_value
        self._on_periodic = on_periodic
        self._periodic: set[tuple[int | None, int]] = set()
        # Objects whose last read went unanswered on this connection: a late answer to it would
        # be indistinguishable from the answer to the next request or push, so nothing is accepted
        # for them until the connection is replaced.
        self._ambiguous: set[tuple[int | None, int]] = set()
        self.pending: PendingTransaction | None = None
        self._unsolicited_counts: dict[tuple[int | None, int], int] = {}
        self._unsolicited_overflow = 0
        self._unsolicited_window_start: float | None = None

    def register_periodic(self, device_key: DeviceKey, object_id: int) -> None:
        self._periodic.add((device_key.network_id, object_id))

    def unregister_periodic(self, device_key: DeviceKey, object_id: int) -> None:
        self._periodic.discard((device_key.network_id, object_id))

    def unregister_all_periodic(self, device_key: DeviceKey) -> None:
        self._periodic = {p for p in self._periodic if p[0] != device_key.network_id}

    def mark_ambiguous(self, plant_address: int | None, object_id: int) -> None:
        self._ambiguous.add((plant_address, object_id))

    def is_ambiguous(self, plant_address: int | None, object_id: int) -> bool:
        return (plant_address, object_id) in self._ambiguous

    def clear_ambiguous(self) -> None:
        self._ambiguous.clear()

    def periodic_count(self) -> int:
        return len(self._periodic)

    def clear_periodic(self) -> None:
        self._periodic.clear()

    def _matches_pending(self, frame: Frame, received_monotonic: float) -> bool:
        p = self.pending
        return (
            p is not None
            and frame.command in RESPONSE_COMMANDS
            and frame.object_id == p.object_id
            and frame.plant_address == p.plant_address
            # A frame that arrived before the send cannot answer it; the protocol carries no transaction id.
            and received_monotonic >= p.sent_monotonic
            and not p.future.done()
        )

    def _is_periodic(self, frame: Frame) -> bool:
        key = (frame.plant_address, frame.object_id)
        # A late answer to an unanswered read must not be cached as a fresh periodic value.
        return frame.command in RESPONSE_COMMANDS and key in self._periodic and key not in self._ambiguous

    def classify(self, frame: Frame, received_monotonic: float) -> FrameKind:
        if self._matches_pending(frame, received_monotonic):
            return FrameKind.TRANSACTION_RESPONSE
        if self._is_periodic(frame):
            return FrameKind.PERIODIC_VALUE
        return FrameKind.UNEXPECTED

    def _log_unmatched_response(self, frame: Frame, received_monotonic: float) -> None:
        """Name the one case a healthy connection can lose a transaction to: a stale arrival time."""
        p = self.pending
        if p is None or frame.object_id != p.object_id or frame.plant_address != p.plant_address:
            return
        if p.future.done():
            # The transaction was already answered: a second frame for the same object (a
            # duplicate, or another client's request for it), not a late answer.
            log.debug(
                "Duplicate response to object 0x%08x after its transaction was answered "
                "(received_monotonic=%.6f sent_monotonic=%.6f)",
                frame.object_id, received_monotonic, p.sent_monotonic,
            )
            return
        log.debug(
            "Response to object 0x%08x not matched to the pending transaction: "
            "received_monotonic=%.6f sent_monotonic=%.6f future_done=%s",
            frame.object_id,
            received_monotonic,
            p.sent_monotonic,
            p.future.done(),
        )

    def _log_unsolicited(self, frame: Frame, now: float) -> None:
        """Log a burst once at its start and summarise it later, so one-off bursts stay visible.

        A frame after the window end writes the old window's summary and opens a new window. The
        clock stays injected and no timer is needed; a window still open on connection loss is
        flushed by ``flush_unsolicited_log``.
        """
        start = self._unsolicited_window_start
        if start is not None and now - start >= _UNSOLICITED_LOG_INTERVAL_SECONDS:
            self.flush_unsolicited_log(now)
            start = None
        if start is None:
            self._unsolicited_window_start = now
            log.debug(
                "Unsolicited response burst started (no matching request or periodic registration): "
                "command=0x%02x object_id=0x%08x network_id=%s",
                frame.command, frame.object_id, frame.plant_address,
            )
        key = (frame.plant_address, frame.object_id)
        if key in self._unsolicited_counts or len(self._unsolicited_counts) < _UNSOLICITED_LOG_MAX_KEYS:
            self._unsolicited_counts[key] = self._unsolicited_counts.get(key, 0) + 1
        else:
            self._unsolicited_overflow += 1

    def flush_unsolicited_log(self, now: float | None = None) -> None:
        """Write the summary of the open window (only when it holds more than the start frame)."""
        counts = self._unsolicited_counts
        start = self._unsolicited_window_start
        total = sum(counts.values()) + self._unsolicited_overflow
        if start is not None and total > 1:
            if now is None:
                now = self._monotonic()
            shown = sorted(counts.items(), key=lambda item: (-item[1], item[0][1]))[:_UNSOLICITED_LOG_LISTED]
            detail = ", ".join(f"0x{oid:08x}(network_id={net})x{n}" for (net, oid), n in shown)
            hidden = len(counts) - len(shown)
            log.debug(
                "Unsolicited response burst summary (no matching request or periodic registration) in %.0fs: "
                "%d frame(s), %d object(s): %s%s%s",
                now - start,
                total,
                len(counts),
                detail,
                f", +{hidden} more" if hidden > 0 else "",
                f", +{self._unsolicited_overflow} frame(s) of further objects" if self._unsolicited_overflow else "",
            )
        self._unsolicited_counts = {}
        self._unsolicited_overflow = 0
        self._unsolicited_window_start = None

    def dispatch(self, frame: Frame, received_monotonic: float) -> FrameKind:
        """``received_monotonic`` must be the arrival of the frame's last byte, not of its read."""
        kind = self.classify(frame, received_monotonic)
        # Hot read path, so the level check gates every argument lookup and the formatting.
        if (
            kind is not FrameKind.TRANSACTION_RESPONSE
            and log.isEnabledFor(logging.DEBUG)
            and frame.command in RESPONSE_COMMANDS
        ):
            self._log_unmatched_response(frame, received_monotonic)
        now = self._monotonic()
        if kind is FrameKind.TRANSACTION_RESPONSE:
            assert self.pending is not None
            self.pending.future.set_result(frame)
            if self._on_value:
                # A response that is also a registered periodic value gets the cache hand-off (3.6).
                self._on_value(frame, FrameKind.PERIODIC_VALUE if self._is_periodic(frame) else kind)
        elif kind is FrameKind.PERIODIC_VALUE:
            self._counters.last_periodic_monotonic = now
            if self._on_periodic:
                self._on_periodic(frame.plant_address, now)
            if self._on_value:
                self._on_value(frame, kind)
        else:
            self._counters.record_unexpected(now, foreign_response=frame.command in RESPONSE_COMMANDS)
            if frame.command in RESPONSE_COMMANDS and log.isEnabledFor(logging.DEBUG):
                self._log_unsolicited(frame, now)
        return kind
