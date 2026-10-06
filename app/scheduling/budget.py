#!/usr/bin/env python3
#
# app/scheduling/budget.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Sliding-window work budget per transport endpoint (Requirement 6.10 to 6.12)."""

import contextlib
from collections import deque
from collections.abc import Iterable

from app.clock import Clock


class BudgetHandle:
    """The exact stamps one caller reserved; ``commit`` marks them spent, ``release`` gives them back."""

    def __init__(self, budget: "WorkBudget", stamps: list[float]) -> None:
        self._budget = budget
        self._stamps = stamps

    def __len__(self) -> int:
        return len(self._stamps)

    def take(self, count: int = 1) -> "BudgetHandle":
        """Split off ``count`` stamps into their own handle."""
        taken, self._stamps = self._stamps[:count], self._stamps[count:]
        return BudgetHandle(self._budget, taken)

    def commit(self) -> None:
        self._stamps = []

    def release(self) -> None:
        """Idempotent; a no-op once the transaction started and the handle was committed."""
        stamps, self._stamps = self._stamps, []
        self._budget.refund(stamps)


class WorkBudget:
    """Limits caller-triggered transactions independent of the HTTP request rate."""

    def __init__(self, transactions: int, window_seconds: float, clock: Clock) -> None:
        self._limit = transactions
        self._window = window_seconds
        self._clock = clock
        self._stamps: deque[float] = deque()

    def _purge(self) -> None:
        horizon = self._clock.monotonic() - self._window
        while self._stamps and self._stamps[0] <= horizon:
            self._stamps.popleft()

    def try_consume(self, count: int = 1) -> BudgetHandle | None:
        """All-or-nothing: a batch is checked once for every requested metric."""
        self._purge()
        if len(self._stamps) + count > self._limit:
            return None
        reserved = [self._clock.monotonic()] * count
        self._stamps.extend(reserved)
        return BudgetHandle(self, reserved)

    def refund(self, stamps: Iterable[float]) -> None:
        """Remove exactly the reserved stamps; ones already purged by the window are gone anyway."""
        for stamp in stamps:
            with contextlib.suppress(ValueError):
                self._stamps.remove(stamp)

    def remaining(self) -> int:
        self._purge()
        return self._limit - len(self._stamps)

    def retry_after(self) -> float:
        """Seconds until at least one unit is free again; 0 when the budget is not exhausted."""
        self._purge()
        if len(self._stamps) < self._limit:
            return 0.0
        return max(0.0, self._stamps[0] + self._window - self._clock.monotonic())
