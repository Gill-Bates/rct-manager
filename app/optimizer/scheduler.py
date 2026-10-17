#!/usr/bin/env python3
#
# app/optimizer/scheduler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""In-memory plan storage and lookup, deliberately without any execution behavior."""

from datetime import datetime

from app.clock import Clock, SystemClock
from app.optimizer.models import EnergyPlan, EnergyPlanSlot, _require_aware


class InMemoryPlanScheduler:
    """Store one non-expired plan and report the slot covering an instant.

    This class manages data only. It has no timer, no persistence, and no path to a battery action.
    One injected :class:`~app.clock.Clock` is the single time source for the whole plan lifecycle:
    ``set_plan`` and ``current_plan`` read it for expiry, and ``active_slot`` takes the same instant
    explicitly. There is no direct wall-clock access, so a deterministic clock drives it end to end.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._plan: EnergyPlan | None = None
        self._clock: Clock = clock or SystemClock()

    def set_plan(self, plan: EnergyPlan) -> None:
        if not isinstance(plan, EnergyPlan):
            raise TypeError("plan must be an EnergyPlan")
        if plan.valid_until <= self._clock.now():
            raise ValueError("plan is already expired")
        self._plan = plan

    def clear(self) -> None:
        """Drop the stored plan so no slot is served until a fresh plan is set.

        The planning service calls this on every hard no-plan recompute, so a previous plan is
        never served on top of inputs a newer cycle has just found stale or missing.
        """
        self._plan = None

    def active_slot(self, now: datetime) -> EnergyPlanSlot | None:
        _require_aware(now, "now")
        plan = self._plan
        if plan is None or plan.valid_until <= now:
            return None
        return next((slot for slot in plan.slots if slot.start <= now < slot.end), None)

    def current_plan(self) -> EnergyPlan | None:
        if self._plan is not None and self._plan.valid_until <= self._clock.now():
            self._plan = None
        return self._plan
