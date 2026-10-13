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


# A single invalid frame never warns; only a sustained CRC-error RATE does. The thresholds mirror
# StreamNoiseMonitor's intent (a few events against the received traffic) but count CRC-dropped
# frames instead of unknown-command bursts.
CRC_MIN_ERRORS = 3  # never warn below this many CRC errors in a window, however the ratio looks
CRC_WARN_RATIO = 0.02  # CRC-dropped frames per received frame that opens a disturbed phase
CRC_CLEAR_RATIO = 0.01  # ratio below which the disturbed phase ends (hysteresis)
CRC_REMIND_SECONDS = 900.0  # while disturbed, repeat the WARNING at most this often


class CrcLogRateLimiter:
    """Decides when CRC-mismatch drops are worth a WARNING, over the ENDPOINT's lifetime.

    One instance is created per transport endpoint and reused across every reconnect, so the
    escalation state is NOT reset when a new per-connection StreamParser (and Receiver) is built.
    That is the whole point: without it a reconnect storm rebuilds the parser's own counter again
    and again and re-logs the same WARNING. The parser keeps counting and DEBUG-logging every
    invalid frame; this limiter rate-limits the WARNING against the received traffic, with the same
    hysteresis-and-remind shape as StreamNoiseMonitor. It never changes what counts as a valid
    frame - a real CRC failure is still rejected by the parser regardless of logging.
    """

    def __init__(self, window_seconds: float) -> None:
        self._window = window_seconds
        self._errors: deque[list[float]] = deque()  # [bucket_start, count]
        self._error_total = 0
        self._frames: deque[list[float]] = deque()  # [bucket_start, count]
        self._frame_total = 0
        self._warned_at: float | None = None  # set while a disturbed phase lasts

    @property
    def disturbed(self) -> bool:
        return self._warned_at is not None

    def frames(self, now: float, count: int) -> None:
        if count <= 0:
            return
        _append_bucket(self._frames, now, count)
        self._frame_total += count

    def crc_errors(self, now: float, count: int) -> None:
        """Record ``count`` newly observed CRC-mismatch drops (already counted on the endpoint)."""
        if count <= 0:
            return
        _append_bucket(self._errors, now, count)
        self._error_total += count

    def evaluate(self, now: float) -> None:
        self._trim(now)
        errors = self._error_total
        ratio = errors / max(self._frame_total + errors, 1)  # CRC drops are not in _frame_total
        if self._warned_at is None:
            if errors >= CRC_MIN_ERRORS and ratio >= CRC_WARN_RATIO:
                self._warned_at = now
                self._warn(errors, ratio)
        elif ratio < CRC_CLEAR_RATIO or errors < CRC_MIN_ERRORS:
            self._warned_at = None
            log.info("CRC error rate back to normal: %d error(s) in %.0f s", errors, self._window)
        elif now - self._warned_at >= CRC_REMIND_SECONDS:
            self._warned_at = now
            self._warn(errors, ratio)

    def _warn(self, errors: int, ratio: float) -> None:
        log.warning(
            "Frequent CRC errors: %d frame(s) dropped for a CRC mismatch in %.0f s (%.1f%% of received frames); "
            "individual frames are logged at DEBUG",
            errors,
            self._window,
            100 * ratio,
        )

    def _trim(self, now: float) -> None:
        horizon = now - self._window
        while self._errors and self._errors[0][0] < horizon:
            self._error_total -= self._errors.popleft()[1]
        while self._frames and self._frames[0][0] < horizon:
            self._frame_total -= self._frames.popleft()[1]


def _append_bucket(window: deque[list[float]], now: float, count: int) -> None:
    bucket = math.floor(now / _BUCKET_SECONDS) * _BUCKET_SECONDS
    if window and window[-1][0] == bucket:
        window[-1][1] += count
    else:
        window.append([bucket, count])
