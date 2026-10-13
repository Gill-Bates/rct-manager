#!/usr/bin/env python3
#
# tests/test_optimizer_scheduler.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Tests for the non-executing in-memory optimizer plan scheduler."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from app.optimizer.models import EnergyAction, EnergyPlan, EnergyPlanSlot
from app.optimizer.scheduler import InMemoryPlanScheduler


def _plan(now: datetime, **overrides) -> EnergyPlan:
    slot = EnergyPlanSlot(
        start=now + timedelta(minutes=10),
        end=now + timedelta(minutes=40),
        action=EnergyAction.CHARGE,
        target_soc_percent=80.0,
        max_power_w=1500.0,
        reason="low spot price",
    )
    values = {"created_at": now, "valid_until": now + timedelta(hours=1), "slots": (slot,)}
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
    scheduler = InMemoryPlanScheduler()
    plan = _plan(now)
    scheduler.set_plan(plan)

    with patch("app.optimizer.scheduler.datetime") as clock:
        clock.now.return_value = plan.valid_until
        assert scheduler.current_plan() is None

    assert scheduler.current_plan() is None


def test_scheduler_rejects_an_expired_plan_without_replacing_the_current_one() -> None:
    now = datetime.now(UTC)
    scheduler = InMemoryPlanScheduler()
    current = _plan(now)
    scheduler.set_plan(current)
    expired = _plan(now - timedelta(hours=2), valid_until=now - timedelta(hours=1))

    with pytest.raises(ValueError):
        scheduler.set_plan(expired)

    assert scheduler.current_plan() is current


def test_scheduler_rejects_a_naive_lookup_time() -> None:
    with pytest.raises(ValueError):
        InMemoryPlanScheduler().active_slot(datetime(2026, 10, 12, 12, 0, tzinfo=UTC).replace(tzinfo=None))


def test_optimizer_modules_are_isolated_from_hardware_execution() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "optimizer"
    forbidden = ("app.energy.", "app.dispatch.controller", "app.dispatch.store", "app.gateway", "EnergyCommand")

    for filename in ("models.py", "base.py", "scheduler.py"):
        source = (root / filename).read_text()
        for name in forbidden:
            assert name not in source, f"{filename} must not name {name}"
