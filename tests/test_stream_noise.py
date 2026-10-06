#!/usr/bin/env python3
#
# tests/test_stream_noise.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Logging policy for unknown command bytes: rated against the traffic, with hysteresis."""

import logging

import pytest

from app.transport.noise import BURST_GAP_SECONDS, REMIND_SECONDS, StreamNoiseMonitor

WINDOW = 60.0


def _levels(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.levelname for r in caplog.records if r.levelno >= logging.INFO]


def _bursts(monitor: StreamNoiseMonitor, start: float, count: int, frames_each: int) -> float:
    """``count`` bursts BURST_GAP_SECONDS apart, each followed by ``frames_each`` good frames."""
    now = start
    for _ in range(count):
        monitor.unknown(now, 2, 0x0D)
        monitor.frames(now, frames_each)
        monitor.evaluate(now)
        now += BURST_GAP_SECONDS
    return now


def test_single_burst_stays_debug(caplog: pytest.LogCaptureFixture) -> None:
    monitor = StreamNoiseMonitor(WINDOW)
    with caplog.at_level(logging.DEBUG, logger="app.transport.receiver"):
        _bursts(monitor, 0.0, 1, 0)
    assert _levels(caplog) == []
    assert [r.levelname for r in caplog.records] == ["DEBUG"]


def test_bytes_inside_the_burst_gap_are_one_burst(caplog: pytest.LogCaptureFixture) -> None:
    monitor = StreamNoiseMonitor(WINDOW)
    with caplog.at_level(logging.INFO, logger="app.transport.receiver"):
        for i in range(10):
            monitor.unknown(i * 0.1, 2, 0x0D)
            monitor.evaluate(i * 0.1)
    assert _levels(caplog) == []


def test_bursts_on_a_busy_line_are_tolerated(caplog: pytest.LogCaptureFixture) -> None:
    """5 bursts against 1000 frames are 0.5 %: normal for a shared device."""
    monitor = StreamNoiseMonitor(WINDOW)
    with caplog.at_level(logging.INFO, logger="app.transport.receiver"):
        _bursts(monitor, 0.0, 5, 200)
    assert _levels(caplog) == []
    assert not monitor.disturbed


def test_frequent_noise_warns_once_and_clears(caplog: pytest.LogCaptureFixture) -> None:
    monitor = StreamNoiseMonitor(WINDOW)
    with caplog.at_level(logging.INFO, logger="app.transport.receiver"):
        now = _bursts(monitor, 0.0, 6, 10)  # 6 bursts against 60 frames: 10 %
        assert monitor.disturbed
        now = _bursts(monitor, now, 4, 10)  # still disturbed: no second warning
        for _ in range(int(WINDOW) + 5):  # one quiet window with traffic
            now += 1.0
            monitor.frames(now, 5)
            monitor.evaluate(now)
    assert _levels(caplog) == ["WARNING", "INFO"]
    assert not monitor.disturbed


def test_frame_window_memory_is_bucketed_not_per_event() -> None:
    """Finding P2-2: many frames arriving in the same instant must cost one bucket, not one deque
    entry each, or a high-rate stream could grow _frames without bound."""
    monitor = StreamNoiseMonitor(WINDOW)
    for _ in range(5000):
        monitor.frames(10.0, 1)  # all in the same instant/bucket
    assert monitor._frame_total == 5000
    assert len(monitor._frames) == 1


def test_lasting_noise_is_reminded_rarely(caplog: pytest.LogCaptureFixture) -> None:
    monitor = StreamNoiseMonitor(WINDOW)
    with caplog.at_level(logging.WARNING, logger="app.transport.receiver"):
        now = 0.0
        while now < 2 * REMIND_SECONDS + 10:  # first warning after 3 bursts, then +900 s each
            now = _bursts(monitor, now, 1, 10)
    assert _levels(caplog) == ["WARNING"] * 3  # start, plus one reminder per REMIND_SECONDS
