#!/usr/bin/env python3
#
# app/transport/counters.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Counters kept per transport endpoint (Requirement 3.12, 24.10)."""

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

# One-second buckets bound a window counter's memory to roughly window_seconds entries,
# regardless of how many events land in it: a flood of well-formed foreign
# RESPONSE frames, exempt from the flood-reconnect limit, could otherwise grow its deque by one
# float per frame for as long as the peer kept sending.
_BUCKET_SECONDS = 1.0


@dataclass(slots=True)
class EndpointCounters:
    last_frame_at: datetime | None = None
    last_success_at: datetime | None = None
    last_send_at: datetime | None = None
    last_success_monotonic: float | None = None
    last_periodic_monotonic: float | None = None
    discarded_bytes: int = 0
    unexpected_frames: int = 0
    unexpected_frames_window: deque[list[float]] = field(default_factory=deque)  # [bucket_start, count]
    # flood_frames_window below holds the subset that are not well-formed responses to another
    # client; only those can force a reconnect (Requirement 3.11).
    flood_frames_window: deque[list[float]] = field(default_factory=deque)
    transactions: int = 0
    failures: int = 0

    def record_unexpected(self, now: float, *, foreign_response: bool = False) -> None:
        """Count an unexpected frame; a well-formed response to another client is foreign access, not a flood."""
        self.unexpected_frames += 1
        _bucket_append(self.unexpected_frames_window, now)
        if not foreign_response:
            _bucket_append(self.flood_frames_window, now)

    def unexpected_in_window(self, now: float, window_seconds: float) -> int:
        return _in_window(self.unexpected_frames_window, now, window_seconds)

    def flood_in_window(self, now: float, window_seconds: float) -> int:
        return _in_window(self.flood_frames_window, now, window_seconds)


def _bucket_append(window: deque[list[float]], now: float) -> None:
    bucket = math.floor(now / _BUCKET_SECONDS) * _BUCKET_SECONDS
    if window and window[-1][0] == bucket:
        window[-1][1] += 1
    else:
        window.append([bucket, 1])


def _in_window(window: deque[list[float]], now: float, window_seconds: float) -> int:
    horizon = now - window_seconds
    while window and window[0][0] <= horizon:
        window.popleft()
    return sum(count for _, count in window)


@dataclass(slots=True)
class DeviceCounters:
    """Liveness and transaction counts of one device; master and slaves share an endpoint (Requirement 16)."""

    last_success_at: datetime | None = None
    last_success_monotonic: float | None = None
    last_periodic_monotonic: float | None = None
    transactions: int = 0
    failures: int = 0
