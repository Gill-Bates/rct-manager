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

from app.errors import RateLimited
from app.security.ratelimit import (
    MAX_KEYS,
    AuthFailureTracker,
    RateLimiter,
    SlidingWindow,
    admin_session_key,
)
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
    # instead of relying on sleeps in the clock or limiter to widen the race window.
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


def test_overflow_address_is_rejected_instead_of_silently_merged(
    clock: ManualClock,
) -> None:
    tracker = AuthFailureTracker(
        limit=3, window_seconds=60.0, block_seconds=900.0, clock=clock
    )
    # Fill the table with MAX_KEYS distinct, non-expired addresses so no eviction can free room.
    for i in range(MAX_KEYS):
        tracker._failures[f"10.0.{i // 256}.{i % 256}"] = deque([clock.monotonic()])

    with pytest.raises(RuntimeError):
        tracker.record_failure("203.0.113.1")

    # The capacity-exhausted address was never recorded, so it is never found blocked either.
    assert "__overflow__" not in tracker._blocked_until
    assert tracker.blocked_for("203.0.113.1") == 0.0


def _limiter(clock: ManualClock, **overrides) -> RateLimiter:
    params = {
        "requests": 3,
        "window_seconds": 60.0,
        "auth_fail_limit": 2,
        "auth_fail_window_seconds": 60.0,
        "auth_fail_block_seconds": 300.0,
        "scrape_requests": 5,
        "scrape_window_seconds": 60.0,
        "admin_session_requests": 10,
        "admin_session_window_seconds": 60.0,
    }
    return RateLimiter(clock, **{**params, **overrides})  # type: ignore[arg-type]


def test_rejected_hits_do_not_extend_the_lockout(clock: ManualClock) -> None:
    window = SlidingWindow(2, 60.0, clock)
    assert window.hit("k") is None and window.hit("k") is None
    for _ in range(50):  # a client that keeps hammering while rejected
        assert window.hit("k") is not None
    clock.advance(
        60.0
    )  # the two admitted hits leave the window; the rejected ones left no trace
    assert window.hit("k") is None


def test_admin_session_budget_is_separate_from_the_business_budget(
    clock: ManualClock,
) -> None:
    limiter = _limiter(clock)
    key = admin_session_key("cookie-a", "192.0.2.1")
    for _ in range(3):
        limiter.check_request("admin:192.0.2.1")
    with pytest.raises(RateLimited):
        limiter.check_request("admin:192.0.2.1")
    for _ in range(10):  # the exhausted PAT/API budget does not starve the GUI session
        limiter.check_admin_session(key)
    with pytest.raises(RateLimited) as exc:
        limiter.check_admin_session(key)
    assert exc.value.context["retry_after"] >= 1


def test_admin_session_budget_is_isolated_per_session_and_address(
    clock: ManualClock,
) -> None:
    limiter = _limiter(clock, admin_session_requests=2)
    first = admin_session_key("cookie-a", "192.0.2.1")
    for _ in range(2):
        limiter.check_admin_session(first)
    with pytest.raises(RateLimited):
        limiter.check_admin_session(first)
    limiter.check_admin_session(
        admin_session_key("cookie-b", "192.0.2.1")
    )  # other browser, same IP
    limiter.check_admin_session(
        admin_session_key("cookie-a", "192.0.2.2")
    )  # same cookie, other IP
    assert (
        "cookie-a" not in first
    )  # the raw session secret never sits in limiter memory


def test_admin_session_backs_off_and_recovers_after_retry_after(
    clock: ManualClock,
) -> None:
    limiter = _limiter(clock, admin_session_requests=2)
    key = admin_session_key("cookie-a", "192.0.2.1")
    limiter.check_admin_session(key)
    limiter.check_admin_session(key)
    with pytest.raises(RateLimited) as exc:
        limiter.check_admin_session(key)
    clock.advance(float(exc.value.context["retry_after"]))
    limiter.check_admin_session(key)


def test_login_and_auth_failure_limits_are_unchanged_by_the_admin_budget(
    clock: ManualClock,
) -> None:
    limiter = _limiter(clock, admin_session_requests=1000)
    for _ in range(3):
        limiter.check_request("admin-login:192.0.2.1")
    with pytest.raises(RateLimited):
        limiter.check_request("admin-login:192.0.2.1")
    limiter.record_auth_failure("192.0.2.1")
    limiter.record_auth_failure("192.0.2.1")
    with pytest.raises(RateLimited):
        limiter.check_auth_blocked("192.0.2.1")
