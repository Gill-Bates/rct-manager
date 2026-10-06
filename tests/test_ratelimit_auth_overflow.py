#!/usr/bin/env python3
#
# tests/test_ratelimit_auth_overflow.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Regression for Finding 3: auth-failure overflow must fail closed, not block an unqueried key."""

from collections import deque

import pytest

from app.security.ratelimit import MAX_KEYS, AuthFailureTracker
from tests.conftest import ManualClock


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
