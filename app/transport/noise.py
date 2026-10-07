#!/usr/bin/env python3
#
# app/transport/noise.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""When discarded frame starts are worth a WARNING (Requirement 1.12 logging policy)."""

import logging
import math
from collections import deque

log = logging.getLogger("app.transport.receiver")

BURST_GAP_SECONDS = 2.0  # unknown command bytes closer than this belong to one burst
MIN_BURSTS = 3  # never warn about fewer bursts per window, however quiet the line is
WARN_RATIO = 0.01  # bursts per received frame that start a disturbed phase
CLEAR_RATIO = 0.005  # ratio below which a disturbed phase ends (hysteresis)
REMIND_SECONDS = 900.0  # repeat the WARNING at most this often while disturbed
# One-second buckets bound _frames to roughly window_seconds entries however busy the line gets,
# the same bound EndpointCounters's window deques use.
_BUCKET_SECONDS = 1.0


class StreamNoiseMonitor:
    """Rates unknown command bytes against the traffic instead of counting them absolutely.

    A shared device emits occasional serial-bridge bursts ("+++\\r" plus UART noise), more of them
    the busier the line is. One burst yields several unknown command bytes, so bytes arriving
    within BURST_GAP_SECONDS count as one burst. A phase is "disturbed" when a window holds at
    least MIN_BURSTS bursts and they make up WARN_RATIO of the received frames; it ends below
    CLEAR_RATIO. Each burst is logged at DEBUG, the start of a phase at WARNING (repeated every
    REMIND_SECONDS while it lasts) and its end at INFO.
    """

    def __init__(self, window_seconds: float) -> None:
        self._window = window_seconds
        self._bursts: deque[float] = deque()
        self._frames: deque[list[float]] = deque()  # [bucket_start, count]
        self._frame_total = 0
        self._last_unknown_at: float | None = None
        self._warned_at: float | None = None  # set while a disturbed phase lasts

    @property
    def disturbed(self) -> bool:
        return self._warned_at is not None

    def frames(self, now: float, count: int) -> None:
        if count <= 0:
            return
        bucket = math.floor(now / _BUCKET_SECONDS) * _BUCKET_SECONDS
        if self._frames and self._frames[-1][0] == bucket:
            self._frames[-1][1] += count
        else:
            self._frames.append([bucket, count])
        self._frame_total += count

    def unknown(self, now: float, count: int, last_command: int) -> None:
        log.debug("Discarded %d frame start(s) with an unknown command byte, last 0x%02x", count, last_command)
        if self._last_unknown_at is None or now - self._last_unknown_at >= BURST_GAP_SECONDS:
            self._bursts.append(now)
        self._last_unknown_at = now

    def evaluate(self, now: float) -> None:
        self._trim(now)
        bursts = len(self._bursts)
        ratio = bursts / max(self._frame_total, 1)
        if self._warned_at is None:
            if bursts >= MIN_BURSTS and ratio >= WARN_RATIO:
                self._warned_at = now
                self._warn(bursts, ratio)
        elif ratio < CLEAR_RATIO or bursts < MIN_BURSTS:
            self._warned_at = None
            log.info("Stream noise back to normal: %d burst(s) in %.0f s", bursts, self._window)
        elif now - self._warned_at >= REMIND_SECONDS:
            self._warned_at = now
            self._warn(bursts, ratio)

    def _warn(self, bursts: int, ratio: float) -> None:
        log.warning(
            "Frequent stream noise: %d burst(s) of unknown command bytes in %.0f s (%.1f%% of %d frames)",
            bursts,
            self._window,
            100 * ratio,
            self._frame_total,
        )

    def _trim(self, now: float) -> None:
        horizon = now - self._window
        while self._bursts and self._bursts[0] < horizon:
            self._bursts.popleft()
        while self._frames and self._frames[0][0] < horizon:
            self._frame_total -= self._frames.popleft()[1]
