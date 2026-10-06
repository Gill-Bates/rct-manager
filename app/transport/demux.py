#!/usr/bin/env python3
#
# app/transport/demux.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Frame classification: transaction response, periodic value or unexpected frame."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from app.config import DeviceKey
from app.protocol.frames import Frame
from app.protocol.types import Command, FrameKind
from app.transport.counters import EndpointCounters

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
        self.pending: PendingTransaction | None = None

    def register_periodic(self, device_key: DeviceKey, object_id: int) -> None:
        self._periodic.add((device_key.network_id, object_id))

    def unregister_periodic(self, device_key: DeviceKey, object_id: int) -> None:
        self._periodic.discard((device_key.network_id, object_id))

    def unregister_all_periodic(self, device_key: DeviceKey) -> None:
        self._periodic = {p for p in self._periodic if p[0] != device_key.network_id}

    def periodic_count(self) -> int:
        return len(self._periodic)

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
        return frame.command in RESPONSE_COMMANDS and (frame.plant_address, frame.object_id) in self._periodic

    def classify(self, frame: Frame, received_monotonic: float) -> FrameKind:
        if self._matches_pending(frame, received_monotonic):
            return FrameKind.TRANSACTION_RESPONSE
        if self._is_periodic(frame):
            return FrameKind.PERIODIC_VALUE
        return FrameKind.UNEXPECTED

    def dispatch(self, frame: Frame, received_monotonic: float) -> FrameKind:
        """``received_monotonic`` must come from the read boundary, not from this call."""
        kind = self.classify(frame, received_monotonic)
        now = self._monotonic()
        if kind is FrameKind.TRANSACTION_RESPONSE:
            assert self.pending is not None
            self.pending.future.set_result(frame)
            if self._is_periodic(frame) and self._on_value:
                self._on_value(frame, FrameKind.PERIODIC_VALUE)  # additional cache hand-off (3.6)
            elif self._on_value:
                self._on_value(frame, kind)
        elif kind is FrameKind.PERIODIC_VALUE:
            self._counters.last_periodic_monotonic = now
            if self._on_periodic:
                self._on_periodic(frame.plant_address, now)
            if self._on_value:
                self._on_value(frame, kind)
        else:
            self._counters.record_unexpected(now, foreign_response=frame.command in RESPONSE_COMMANDS)
        return kind
