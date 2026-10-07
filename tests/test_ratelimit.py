#!/usr/bin/env python3
#
# tests/test_ratelimit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Rate limiter: thread safety and fail-closed auth-failure overflow."""

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.security.ratelimit import MAX_KEYS, AuthFailureTracker, SlidingWindow
from tests.conftest import ManualClock


class _YieldingClock:
    """Constant monotonic time; yields the GIL to widen any unlocked check-then-act window."""

    def monotonic(self) -> float:
        time.sleep(0.0005)
        return 100.0


class _SlowLimitWindow(SlidingWindow):
    """Yields right after the capacity check was read: the race window without a lock."""

    @property
    def _limit(self) -> int:
        time.sleep(0.001)
        return self._limit_value

    @_limit.setter
    def _limit(self, value: int) -> None:
        self._limit_value = value


def test_sliding_window_allows_exactly_limit_hits_across_threads() -> None:
    limit, threads = 10, 64
    window = _SlowLimitWindow(limit, 60.0, _YieldingClock())  # type: ignore[arg-type]
    # Explicit sync point so every thread is lined up right before the check-then-act section,
    # instead of merely hoping sleeps in the clock/_limit widen the race window (Finding 7).
    barrier = threading.Barrier(threads)

    def worker(_: int) -> object:
        barrier.wait()
        return window.hit("same-key")

    with ThreadPoolExecutor(max_workers=threads) as pool:
        results = list(pool.map(worker, range(threads)))
    assert sum(r is None for r in results) == limit


def test_auth_tracker_blocks_after_concurrent_failures() -> None:
    tracker = AuthFailureTracker(5, 60.0, 30.0, _YieldingClock())  # type: ignore[arg-type]
    failures = 5
    barrier = threading.Barrier(failures)

    def worker(_: int) -> None:
        barrier.wait()
        tracker.record_failure("192.0.2.1")

    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(worker, range(failures)))
    assert tracker.blocked_for("192.0.2.1") > 0


def test_overflow_address_is_rejected_instead_of_silently_merged(clock: ManualClock) -> None:
    tracker = AuthFailureTracker(limit=3, window_seconds=60.0, block_seconds=900.0, clock=clock)
    # Fill the table with MAX_KEYS distinct, non-expired addresses so no eviction can free room.
    for i in range(MAX_KEYS):
        tracker._failures[f"10.0.{i // 256}.{i % 256}"] = deque([clock.monotonic()])

    with pytest.raises(RuntimeError):
        tracker.record_failure("203.0.113.1")

    # The capacity-exhausted address was never recorded, so it is never found blocked either.
    assert "__overflow__" not in tracker._blocked_until
    assert tracker.blocked_for("203.0.113.1") == 0.0
