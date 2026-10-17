#!/usr/bin/env python3
#
# tests/test_optimizer_scheduler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Tests for the non-executing in-memory optimizer plan scheduler."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.optimizer.models import EnergyAction, EnergyPlan, EnergyPlanSlot
from app.optimizer.scheduler import InMemoryPlanScheduler


class _FakeClock:
    """A deterministic time source for the scheduler lifecycle: no wall clock, fully controlled."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now

    def monotonic(self) -> float:  # pragma: no cover - unused by the scheduler
        return 0.0

    async def sleep(
        self, seconds: float
    ) -> None:  # pragma: no cover - unused by the scheduler
        return None


def _plan(now: datetime, **overrides) -> EnergyPlan:
    slot = EnergyPlanSlot(
        start=now + timedelta(minutes=10),
        end=now + timedelta(minutes=40),
        action=EnergyAction.CHARGE,
        target_soc_percent=80.0,
        max_power_w=1500.0,
        reason="low spot price",
    )
    values = {
        "created_at": now,
        "valid_until": now + timedelta(hours=1),
        "slots": (slot,),
    }
    values.update(overrides)
    return EnergyPlan(**values)


def test_scheduler_stores_a_plan_and_returns_only_the_covering_slot() -> None:
    now = datetime.now(UTC)
    scheduler = InMemoryPlanScheduler()
    plan = _plan(now)
    scheduler.set_plan(plan)

    assert scheduler.current_plan() is plan
    assert scheduler.active_slot(now) is None
    assert scheduler.active_slot(now + timedelta(minutes=20)) is plan.slots[0]
    assert scheduler.active_slot(now + timedelta(minutes=50)) is None


def test_scheduler_returns_none_for_missing_or_expired_plan() -> None:
    now = datetime.now(UTC)
    scheduler = InMemoryPlanScheduler()
    assert scheduler.active_slot(now) is None

    plan = _plan(now)
    scheduler.set_plan(plan)
    assert scheduler.active_slot(plan.valid_until) is None


def test_current_plan_discards_expired_plan() -> None:
    now = datetime.now(UTC)
    clock = _FakeClock(now)
    scheduler = InMemoryPlanScheduler(clock)
    plan = _plan(now)
    scheduler.set_plan(plan)

    # The scheduler judges expiry against its injected clock, not the wall clock.
    clock.set(plan.valid_until)
    assert scheduler.current_plan() is None
    assert scheduler.current_plan() is None


def test_scheduler_uses_the_injected_clock_for_the_whole_lifecycle() -> None:
    # Item 3: set_plan and current_plan read ONE injected clock, so a deterministic clock drives
    # acceptance and expiry without any wall-clock leakage.
    base = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    clock = _FakeClock(base)
    scheduler = InMemoryPlanScheduler(clock)
    plan = _plan(base)
    scheduler.set_plan(plan)
    assert scheduler.current_plan() is plan

    # Advancing only the injected clock past valid_until expires the plan; real time is irrelevant.
    clock.set(plan.valid_until + timedelta(seconds=1))
    assert scheduler.current_plan() is None

    # A plan already expired against the injected clock is rejected by set_plan.
    clock.set(base)
    stale = _plan(base - timedelta(hours=2), valid_until=base - timedelta(hours=1))
    with pytest.raises(ValueError):
        scheduler.set_plan(stale)


def test_scheduler_rejects_an_expired_plan_without_replacing_the_current_one() -> None:
    now = datetime.now(UTC)
    scheduler = InMemoryPlanScheduler()
    current = _plan(now)
    scheduler.set_plan(current)
    expired = _plan(now - timedelta(hours=2), valid_until=now - timedelta(hours=1))

    with pytest.raises(ValueError):
        scheduler.set_plan(expired)

    assert scheduler.current_plan() is current


def test_clear_invalidates_the_stored_plan() -> None:
    now = datetime.now(UTC)
    scheduler = InMemoryPlanScheduler()
    scheduler.set_plan(_plan(now))

    scheduler.clear()

    assert scheduler.current_plan() is None
    assert scheduler.active_slot(now + timedelta(minutes=20)) is None


def test_scheduler_rejects_a_naive_lookup_time() -> None:
    with pytest.raises(ValueError):
        InMemoryPlanScheduler().active_slot(
            datetime(2026, 10, 12, 12, 0, tzinfo=UTC).replace(tzinfo=None)
        )


def test_optimizer_modules_are_isolated_from_hardware_execution() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "optimizer"
    forbidden = (
        "app.energy.",
        "app.dispatch.controller",
        "app.dispatch.store",
        "app.gateway",
        "EnergyCommand",
    )

    # The pure algorithm and the pure history/forecast live here and must stay hardware-free. The
    # impure data adapters (adapters.py) deliberately read stores/cache and are NOT on this list.
    for filename in (
        "models.py",
        "base.py",
        "scheduler.py",
        "planner.py",
        "consumption.py",
    ):
        source = (root / filename).read_text()
        for name in forbidden:
            assert name not in source, f"{filename} must not name {name}"
