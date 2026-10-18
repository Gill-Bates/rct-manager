#!/usr/bin/env python3
#
# tests/test_energy_plan_handoff.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Tests for the default-OFF plan dispatch handoff: mapping, dedup, locked execution, restart."""

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.energy import manager as manager_module
from app.energy.errors import EnergyRejected
from app.energy.models import EnergyAction as ExecutorAction
from app.energy.models import EnergyCommand, EnergyState
from app.energy.plan_handoff import _EXPECTED_REFUSALS, _HANDOFF_ACTOR, PlanDispatchHandoff
from app.optimizer.models import EnergyAction, EnergyPlanSlot

DEVICE = "main"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


@dataclass
class _StubStatus:
    """Just the two fields the restart check reads off a status."""

    action: ExecutorAction | None
    target_soc_percent: float | None = None
    power_limit_w: float | None = None
    state: EnergyState = EnergyState.AUTOMATIC


class _StubManager:
    """Records every (device, command, actor) the handoff submits and asserts the actor."""

    def __init__(self, *, status: _StubStatus | None = None, reject: str | None = None) -> None:
        self.calls: list[tuple[str, EnergyCommand, str | None]] = []
        self._status = status or _StubStatus(action=None)
        self._reject = reject

    async def command(self, device_id: str, command: EnergyCommand, *, actor: str | None):
        assert actor == _HANDOFF_ACTOR, "every offered command must carry the charging-strategy actor"
        self.calls.append((device_id, command, actor))
        if self._reject is not None:
            raise EnergyRejected(self._reject, device_id=device_id)

    async def status(self, device_id: str):
        return self._status

    async def set_mode(self, device_id, mode, *, actor):  # pragma: no cover - unused by the handoff
        raise AssertionError("set_mode must not be called by the handoff")

    def mode(self, device_id):  # pragma: no cover - unused by the handoff
        raise AssertionError("mode must not be called by the handoff")


def _slot(action: EnergyAction, *, target: float | None, power: float | None, offset_min: int = 0) -> EnergyPlanSlot:
    start = NOW + timedelta(minutes=offset_min)
    return EnergyPlanSlot(
        start=start,
        end=start + timedelta(minutes=15),
        action=action,
        target_soc_percent=target,
        max_power_w=power,
        reason=f"{action.value} slot",
    )


async def test_disabled_handoff_issues_no_command_even_with_an_active_slot() -> None:
    manager = _StubManager()
    handoff = PlanDispatchHandoff(manager=manager, enable_automatic_dispatch=False)

    issued = await handoff.offer(DEVICE, _slot(EnergyAction.CHARGE, target=90.0, power=3000.0))

    assert issued is False
    assert manager.calls == []


async def test_idle_and_none_slots_hand_back_an_active_command_once() -> None:
    manager = _StubManager()
    handoff = PlanDispatchHandoff(manager=manager, enable_automatic_dispatch=True)

    charge = _slot(EnergyAction.CHARGE, target=90.0, power=3000.0)
    assert await handoff.offer(DEVICE, charge) is True
    assert await handoff.offer(DEVICE, _slot(EnergyAction.IDLE, target=None, power=None)) is True
    assert await handoff.offer(DEVICE, None) is False
    assert [command.action for _, command, _ in manager.calls] == [
        ExecutorAction.CHARGE,
        ExecutorAction.AUTO,
    ]


async def test_only_explicit_grid_charge_maps_to_a_control_command() -> None:
    manager = _StubManager()
    handoff = PlanDispatchHandoff(manager=manager, enable_automatic_dispatch=True)

    assert await handoff.offer(DEVICE, _slot(EnergyAction.CHARGE, target=90.0, power=3000.0, offset_min=0)) is True
    assert await handoff.offer(DEVICE, _slot(EnergyAction.DISCHARGE, target=10.0, power=2000.0, offset_min=15)) is True
    assert await handoff.offer(DEVICE, _slot(EnergyAction.HOLD, target=None, power=None, offset_min=30)) is False

    actions = [command.action for _, command, _ in manager.calls]
    assert actions == [ExecutorAction.CHARGE, ExecutorAction.AUTO]
    # Every call carried the pinned non-admin actor (also asserted inside the stub).
    assert all(actor == _HANDOFF_ACTOR for _, _, actor in manager.calls)
    # The handback carries neither a target nor a power.
    handback = manager.calls[-1][1]
    assert handback.target_soc_percent is None
    assert handback.max_power_w is None


async def test_restart_hands_back_when_plan_has_no_active_charge_slot() -> None:
    manager = _StubManager(status=_StubStatus(action=ExecutorAction.CHARGE, target_soc_percent=90.0))
    handoff = PlanDispatchHandoff(manager=manager, enable_automatic_dispatch=True)

    assert await handoff.offer(DEVICE, None) is True
    assert manager.calls[0][1].action is ExecutorAction.AUTO


async def test_restart_does_not_mistake_an_unmapped_active_mode_for_automatic() -> None:
    manager = _StubManager(status=_StubStatus(action=None, state=EnergyState.HOLDING))
    handoff = PlanDispatchHandoff(manager=manager, enable_automatic_dispatch=True)

    assert await handoff.offer(DEVICE, None) is True
    assert manager.calls[0][1].action is ExecutorAction.AUTO


async def test_dedup_suppresses_an_unchanged_intent_but_not_a_change() -> None:
    manager = _StubManager()
    handoff = PlanDispatchHandoff(manager=manager, enable_automatic_dispatch=True)

    slot = _slot(EnergyAction.CHARGE, target=90.0, power=3000.0, offset_min=0)
    assert await handoff.offer(DEVICE, slot) is True
    # Same intent again -> no second command (late data / recompute with an unchanged slot).
    assert await handoff.offer(DEVICE, slot) is False
    # A changed intent (different slot window / action) -> a fresh command.
    assert await handoff.offer(DEVICE, _slot(EnergyAction.DISCHARGE, target=10.0, power=2000.0, offset_min=15)) is True
    assert len(manager.calls) == 2


async def test_restart_does_not_double_command_when_device_already_runs_the_intent() -> None:
    # Fresh process (no in-memory last-offered state); the device is already charging to the target.
    manager = _StubManager(status=_StubStatus(action=ExecutorAction.CHARGE, target_soc_percent=90.0, power_limit_w=3000.0))
    handoff = PlanDispatchHandoff(manager=manager, enable_automatic_dispatch=True)

    slot = _slot(EnergyAction.CHARGE, target=90.0, power=3000.0)
    assert await handoff.offer(DEVICE, slot) is False
    assert manager.calls == []
    # And a subsequent unchanged offer is still a no-op.
    assert await handoff.offer(DEVICE, slot) is False
    assert manager.calls == []


async def test_restart_commands_when_device_runs_a_different_intent() -> None:
    manager = _StubManager(status=_StubStatus(action=ExecutorAction.DISCHARGE, target_soc_percent=10.0))
    handoff = PlanDispatchHandoff(manager=manager, enable_automatic_dispatch=True)

    assert await handoff.offer(DEVICE, _slot(EnergyAction.CHARGE, target=90.0, power=3000.0)) is True
    assert len(manager.calls) == 1
    assert manager.calls[0][2] == _HANDOFF_ACTOR


async def test_restart_reoffers_when_only_power_limit_differs() -> None:
    manager = _StubManager(status=_StubStatus(action=ExecutorAction.CHARGE, target_soc_percent=90.0, power_limit_w=2000.0))
    handoff = PlanDispatchHandoff(manager=manager, enable_automatic_dispatch=True)

    assert await handoff.offer(DEVICE, _slot(EnergyAction.CHARGE, target=90.0, power=3000.0)) is True
    assert manager.calls[0][1].max_power_w == 3000.0


async def test_concurrent_identical_offers_issue_one_command() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class _SlowManager(_StubManager):
        async def command(self, device_id: str, command: EnergyCommand, *, actor: str | None):
            await super().command(device_id, command, actor=actor)
            entered.set()
            await release.wait()

    manager = _SlowManager()
    handoff = PlanDispatchHandoff(manager=manager, enable_automatic_dispatch=True)
    slot = _slot(EnergyAction.CHARGE, target=90.0, power=3000.0)

    first = asyncio.create_task(handoff.offer(DEVICE, slot))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    second = asyncio.create_task(handoff.offer(DEVICE, slot))
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, second) == [True, False]
    assert len(manager.calls) == 1


async def test_expected_refusal_is_non_fatal() -> None:
    manager = _StubManager(reject="energy_manager_not_external")
    handoff = PlanDispatchHandoff(manager=manager, enable_automatic_dispatch=True)

    issued = await handoff.offer(DEVICE, _slot(EnergyAction.CHARGE, target=90.0, power=3000.0))
    assert issued is False
    assert len(manager.calls) == 1  # the command was attempted, the refusal was swallowed
    # The refused intent is NOT recorded, so a later retry re-attempts it.
    assert await handoff.offer(DEVICE, _slot(EnergyAction.CHARGE, target=90.0, power=3000.0)) is False
    assert len(manager.calls) == 2


async def test_unexpected_refusal_propagates() -> None:
    manager = _StubManager(reject="invalid_request")
    handoff = PlanDispatchHandoff(manager=manager, enable_automatic_dispatch=True)

    with pytest.raises(EnergyRejected):
        await handoff.offer(DEVICE, _slot(EnergyAction.CHARGE, target=90.0, power=3000.0))


def test_every_expected_refusal_is_a_code_the_manager_raises() -> None:
    # Guards against a renamed manager code silently turning an expected refusal into a crash.
    source = Path(manager_module.__file__).read_text(encoding="utf-8")
    raised = set(re.findall(r'EnergyRejected\(\s*"([a-z_]+)"', source))
    assert _EXPECTED_REFUSALS <= raised
