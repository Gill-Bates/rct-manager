#!/usr/bin/env python3
#
# tests/test_dispatch_hold.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""HOLD as a real dispatch mode, and the separation of the stop goal from the register value.

A hold pins the battery at 0 W under external control: no SoC goal, no power budget, no grid
charging, and a handback that replays the captured snapshot. HOLD is **not** hardware-verified (see
design.md 2.4.6), so it ships behind the same per-device capability gate as every other mode — the
tests below pin the gate, not a hardware claim.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.dispatch.capabilities import (
    CapabilityName,
    CapabilityRecord,
    CapabilityRegistry,
    required_for,
)
from app.dispatch.controller import CapabilityConflict, DispatchRejected
from app.dispatch.models import (
    ControlTelemetry,
    DeviceLimits,
    DispatchCommand,
    DispatchConfig,
    DispatchMode,
    DispatchPhase,
    DispatchState,
    PowerDirection,
    PowerSetpoint,
    StopReason,
    phase_for,
)
from app.dispatch.soc_policy import (
    SocTargetMode,
    SocTargetPolicy,
    SocTargetPolicyRegistry,
)
from app.dispatch.store import DispatchStore
from app.dispatch.strategy import calculate_setpoint, target_reached
from tests.conftest import ManualClock
from tests.test_dispatch_core import (
    FakeDispatchGateway,
    controller,
    controller_with_store,
    telemetry,
    verified_registry,
)

SOC = st.floats(min_value=0, max_value=100)
CONFIG = DispatchConfig()


def stale_telemetry(age: float) -> ControlTelemetry:
    """Telemetry whose every age is past the staleness thresholds of ``DispatchConfig``."""
    return ControlTelemetry(
        50.0, age, "cache", 1200.0, age, "cache", PowerSetpoint(), age, "cache", 1200.0, age
    )


def hold(clock: ManualClock, *, hours: float = 1.0, **overrides) -> DispatchCommand:
    """A well-formed hold: no SoC goal, no power budget."""
    return DispatchCommand(
        overrides.pop("mode", DispatchMode.HOLD),
        overrides.pop("target_soc_percent", None),
        overrides.pop("max_power_w", 0.0),
        clock.now() + timedelta(hours=hours),
        **overrides,
    )


def charge(clock: ManualClock, *, target: float = 80.0, power: float = 2000.0, **overrides) -> DispatchCommand:
    return DispatchCommand(
        DispatchMode.CHARGE_FROM_GRID, target, power, clock.now() + timedelta(hours=1), **overrides
    )


def plan_names(store: DispatchStore, device_id: str = "main") -> list[str]:
    record = store.get(device_id)
    assert record is not None
    return [step["name"] for step in record.plan]


# --- test plan item 1: target_reached is total and is a stop threshold only ----------------------


@given(soc=SOC)
@pytest.mark.parametrize("mode", list(DispatchMode))
def test_a_none_target_is_never_reached_for_any_mode(mode: DispatchMode, soc: float) -> None:
    """A hold has no SoC goal, so it must never self-terminate as "already reached"."""
    assert target_reached(mode, soc, None) is False


@given(soc=SOC, target=SOC)
def test_the_charge_and_discharge_thresholds_are_explicit_and_inclusive(soc: float, target: float) -> None:
    assert target_reached(DispatchMode.CHARGE_FROM_GRID, soc, target) == (soc >= target)
    assert target_reached(DispatchMode.DISCHARGE_TO_LOAD, soc, target) == (soc <= target)
    # No mode falls through into discharge semantics any more.
    assert target_reached(DispatchMode.EXPORT_TO_GRID, soc, target) is False
    assert target_reached(DispatchMode.HOLD, soc, target) is False


@pytest.mark.parametrize("boundary", [0.0, 50.0, 100.0])
def test_the_soc_equals_target_boundary_counts_as_reached(boundary: float) -> None:
    assert target_reached(DispatchMode.CHARGE_FROM_GRID, boundary, boundary) is True
    assert target_reached(DispatchMode.DISCHARGE_TO_LOAD, boundary, boundary) is True


# --- test plan item 2: calculate_setpoint(HOLD, ...) is 0 W, whatever the telemetry says ---------


@given(
    soc=SOC,
    grid=st.floats(min_value=-20_000, max_value=20_000, allow_nan=False, allow_infinity=False),
    battery=st.floats(min_value=0, max_value=20_000, allow_nan=False, allow_infinity=False),
)
def test_a_hold_is_zero_watts_over_hostile_telemetry(soc: float, grid: float, battery: float) -> None:
    setpoint = calculate_setpoint(
        DispatchMode.HOLD,
        telemetry(soc=soc, grid=grid, battery=battery),
        target_soc_percent=None,
        max_power_w=0.0,
        config=CONFIG,
        last=PowerSetpoint(PowerDirection.DISCHARGE, battery) if battery else PowerSetpoint(),
    )
    assert setpoint == PowerSetpoint()
    assert setpoint.direction is PowerDirection.NONE
    assert setpoint.watts == 0.0


def test_a_hold_ignores_a_target_that_should_not_be_there_at_all() -> None:
    """The HOLD branch comes first, so no SoC comparison can turn a hold into a power command."""
    assert calculate_setpoint(
        DispatchMode.HOLD,
        telemetry(soc=10),
        target_soc_percent=90.0,
        max_power_w=5000.0,
        config=CONFIG,
        last=PowerSetpoint(),
    ) == PowerSetpoint()


# --- test plan item 5 / 15: the capability gate ---------------------------------------------------


def test_hold_requires_the_write_path_and_the_battery_sign_convention() -> None:
    """WRITE_PATH activates external control; BATTERY_POWER_SIGN is needed because the restore path
    replays a possibly non-zero captured setpoint through the battery convention."""
    for limit_export in (False, True):
        assert required_for(DispatchMode.HOLD, limit_export=limit_export) == frozenset(
            {CapabilityName.WRITE_PATH, CapabilityName.BATTERY_POWER_SIGN}
        )


def registry_without(name: CapabilityName, *, strategy_code: int | None = 1) -> CapabilityRegistry:
    """The shared released registry with ``name`` put back into the shipping state.

    Nothing here marks a capability released — the released records come from the existing shared
    fixture; this helper only takes one away, which is what the gate has to notice.
    """
    registry = verified_registry("main")
    registry.replace(
        CapabilityRecord(
            device_id="main",
            name=name,
            soc_strategy_external_code=strategy_code if name is CapabilityName.WRITE_PATH else None,
        )
    )
    return registry


@pytest.mark.parametrize("missing", [CapabilityName.WRITE_PATH, CapabilityName.BATTERY_POWER_SIGN])
async def test_hold_is_refused_while_a_required_capability_is_unverified(
    tmp_path: Path, missing: CapabilityName
) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway, capabilities=registry_without(missing))
    with pytest.raises(DispatchRejected, match="dispatch_unverified"):
        await dispatch.submit("main", hold(clock))
    assert gateway.calls == []  # a blocked hold must not even read the device


async def test_hold_runs_under_engineering_mode_with_a_strategy_code(tmp_path: Path) -> None:
    """The only way onto unverified hardware, and it is visibly marked: the TTL is capped shorter."""
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(
        tmp_path,
        clock,
        gateway,
        capabilities=registry_without(CapabilityName.WRITE_PATH),
        limits={"main": DeviceLimits(3000, 5000, engineering_mode=True)},
    )
    status = await dispatch.submit("main", hold(clock, hours=6))
    assert status.state is DispatchState.HOLDING
    assert status.valid_until is not None and status.valid_until < clock.now() + timedelta(hours=6)


async def test_hold_in_engineering_mode_without_a_strategy_code_is_still_refused(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(
        tmp_path,
        clock,
        gateway,
        capabilities=registry_without(CapabilityName.WRITE_PATH, strategy_code=None),
        limits={"main": DeviceLimits(3000, 5000, engineering_mode=True)},
    )
    with pytest.raises(DispatchRejected, match="dispatch_unverified"):
        await dispatch.submit("main", hold(clock))
    assert gateway.calls == []


# --- test plan items 8b and 9: the apply sequence of a hold --------------------------------------


async def test_a_hold_applies_control_mode_grid_charge_and_a_zero_setpoint(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    store = DispatchStore(tmp_path / "dispatch.db", "s" * 48)
    store.initialize()
    dispatch = controller_with_store(store, clock, gateway)

    status = await dispatch.submit("main", hold(clock))

    assert status.state is DispatchState.HOLDING
    assert status.control_state == "controlled"
    assert status.phase is DispatchPhase.CONTROLLING
    assert phase_for(status.state) is DispatchPhase.CONTROLLING
    assert status.target_soc_percent is None
    assert status.max_power_w == 0.0
    assert status.commanded_direction is PowerDirection.NONE
    assert status.commanded_power_w == 0.0
    # No soc_target step: a hold has no SoC goal, so there is nothing to write.
    assert plan_names(store) == ["control_mode", "grid_charge", "setpoint"]
    assert gateway.calls == [
        ("read_soc", "main"),
        ("snapshot", "main"),
        ("control", True),
        ("grid_charge", False),  # holding must not let the grid charge the battery
        ("telemetry", "main"),
        ("setpoint", PowerDirection.NONE, 0.0),
    ]
    assert not any(call[0] == "soc_target" for call in gateway.calls)


async def test_the_omitted_step_does_not_shift_the_confirmed_plan_indices(tmp_path: Path) -> None:
    """Item 8b, the index-shift regression: _apply() dispatches by step NAME over record.plan, so
    every step of the shortened plan is marked against its own entry."""
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    store = DispatchStore(tmp_path / "dispatch.db", "s" * 48)
    store.initialize()
    dispatch = controller_with_store(store, clock, gateway)
    await dispatch.submit("main", hold(clock))
    record = store.get("main")
    assert record is not None
    assert record.plan == [
        {"name": "control_mode", "status": "confirmed"},
        {"name": "grid_charge", "status": "confirmed"},
        {"name": "setpoint", "status": "confirmed"},
    ]


# --- test plan item 10: validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "command_overrides",
    [{"target_soc_percent": 80.0}, {"max_power_w": 2000.0}, {"target_soc_percent": 80.0, "max_power_w": 1.0}],
)
async def test_a_hold_with_a_target_or_a_power_budget_is_an_invalid_request(
    tmp_path: Path, command_overrides: dict
) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    with pytest.raises(DispatchRejected, match="invalid_request"):
        await dispatch.submit("main", hold(clock, **command_overrides))
    assert gateway.calls == []


async def test_a_charge_without_a_target_is_an_invalid_request(tmp_path: Path) -> None:
    """The optional annotation must not make the target optional for a mode that needs one."""
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    with pytest.raises(DispatchRejected, match="invalid_request"):
        await dispatch.submit(
            "main",
            DispatchCommand(DispatchMode.CHARGE_FROM_GRID, None, 2000.0, clock.now() + timedelta(hours=1)),
        )
    assert gateway.calls == []


async def test_a_hold_still_needs_the_device_limits(tmp_path: Path) -> None:
    """The gate needs the engineering switch and the TTL cap needs the gate's verdict, so the limits
    are required even though a hold commands no power."""
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway, limits={})
    with pytest.raises(DispatchRejected, match="dispatch_limits_missing"):
        await dispatch.submit("main", hold(clock))


async def test_a_repeated_identical_hold_is_idempotent(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    first = await dispatch.submit("main", hold(clock))
    gateway.calls.clear()
    again = await dispatch.submit("main", hold(clock))
    assert again.operation_id == first.operation_id
    assert gateway.calls == []


# --- test plan item 11: the control loop ---------------------------------------------------------


async def test_a_steady_hold_costs_no_write_per_tick(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit("main", hold(clock))
    gateway.calls.clear()
    clock.advance(30)
    status = await dispatch.tick("main")
    assert status.state is DispatchState.HOLDING
    assert gateway.calls == [("telemetry", "main")]  # read only, no setpoint write


async def test_a_hold_is_restored_when_its_ttl_expires(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit("main", hold(clock, hours=0.25))
    clock.advance(16 * 60)
    status = await dispatch.tick("main")
    assert status.state is DispatchState.IDLE
    assert status.stop_reason is StopReason.TTL_EXPIRED
    assert [call for call in gateway.calls if call[0] == "restore"] == [
        ("restore", 0),
        ("restore", 1),
        ("restore", 2),
        ("restore", 3),
    ]


async def test_a_hold_with_dark_telemetry_is_restored_not_left_pinned(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit("main", hold(clock))

    async def dark(device_id: str) -> ControlTelemetry:
        gateway.calls.append(("telemetry", device_id))
        return stale_telemetry(60.0)

    gateway.read_control_telemetry = dark  # type: ignore[method-assign]
    status = await dispatch.tick("main")
    assert status.state is DispatchState.IDLE
    assert status.stop_reason is StopReason.TELEMETRY_STALE


# --- test plan item 12: the handback -------------------------------------------------------------


async def test_hold_to_auto_restores_all_four_registers_and_ends_idle(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit("main", hold(clock))
    gateway.calls.clear()

    status = await dispatch.cancel("main")

    assert status.state is DispatchState.IDLE
    assert status.restore_required is False
    assert status.stop_reason is StopReason.OPERATOR_CANCELLED
    assert gateway.calls[0] == ("setpoint", PowerDirection.NONE, 0.0)  # stop external power first
    assert [call for call in gateway.calls if call[0] == "restore"] == [
        ("restore", 0),
        ("restore", 1),
        ("restore", 2),
        ("restore", 3),
    ]


# --- test plan item 13: a charge above the measured SoC is expressible again ---------------------


@pytest.mark.parametrize(
    ("policy", "expected_ratio"),
    [
        (SocTargetPolicy("main"), 0.8),
        (SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC, below_margin_percent=5.0), 0.45),
    ],
)
async def test_a_charge_above_the_measured_soc_runs_and_carries_the_derived_ratio(
    tmp_path: Path, policy: SocTargetPolicy, expected_ratio: float
) -> None:
    """The fake gateway reads 50 % SoC. A target of 80 % is above it: that is a correct charge
    command, and what the SoC-target register gets is the policy's business, not the target."""
    clock = ManualClock()
    policies = SocTargetPolicyRegistry()
    gateway = FakeDispatchGateway(clock, soc_target_policies=policies)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.set_soc_target_policy("main", policy)

    status = await dispatch.submit("main", charge(clock, target=80.0))

    assert status.state is DispatchState.CHARGING
    assert status.target_soc_percent == 80.0  # the business stop goal is published unchanged
    assert ("soc_target", pytest.approx(expected_ratio)) in gateway.calls
    assert ("setpoint", PowerDirection.CHARGE, 2000.0) in gateway.calls


# --- test plan item 13b: the mirror case (AC-25) --------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "target"),
    [
        (DispatchMode.CHARGE_FROM_GRID, 40.0),  # already above the target
        (DispatchMode.CHARGE_FROM_GRID, 50.0),  # the soc == target boundary
        (DispatchMode.DISCHARGE_TO_LOAD, 60.0),  # already below the target
        (DispatchMode.DISCHARGE_TO_LOAD, 50.0),  # the boundary again
    ],
)
async def test_an_already_reached_command_is_accepted_and_writes_nothing(
    tmp_path: Path, mode: DispatchMode, target: float
) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)  # reads 50 % SoC
    dispatch = controller(tmp_path, clock, gateway)

    status = await dispatch.submit(
        "main", DispatchCommand(mode, target, 2000.0, clock.now() + timedelta(hours=1))
    )

    assert status.state is DispatchState.IDLE
    assert status.mode is None
    assert status.stop_reason is StopReason.TARGET_REACHED
    assert gateway.calls == [("read_soc", "main")]  # not one write, not even a snapshot read


# --- test plan item 14: mode replacement ---------------------------------------------------------


async def test_charge_to_hold_keeps_the_original_snapshot(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    store = DispatchStore(tmp_path / "dispatch.db", "s" * 48)
    store.initialize()
    dispatch = controller_with_store(store, clock, gateway)
    first = await dispatch.submit("main", charge(clock))
    before = store.get("main")
    assert before is not None and before.snapshot is not None
    gateway.calls.clear()

    replaced = await dispatch.submit("main", hold(clock, expected_operation_id=first.operation_id))

    assert replaced.replaced is True
    assert replaced.state is DispatchState.HOLDING
    assert ("snapshot", "main") not in gateway.calls  # the captured device state is never re-read
    after = store.get("main")
    assert after is not None and after.snapshot == before.snapshot
    assert plan_names(store) == ["control_mode", "grid_charge", "setpoint"]


async def test_hold_to_charge_keeps_the_original_snapshot_and_restores_the_soc_target_step(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    store = DispatchStore(tmp_path / "dispatch.db", "s" * 48)
    store.initialize()
    dispatch = controller_with_store(store, clock, gateway)
    first = await dispatch.submit("main", hold(clock))
    before = store.get("main")
    assert before is not None and before.snapshot is not None
    gateway.calls.clear()

    replaced = await dispatch.submit("main", charge(clock, expected_operation_id=first.operation_id))

    assert replaced.state is DispatchState.CHARGING
    assert ("snapshot", "main") not in gateway.calls
    after = store.get("main")
    assert after is not None and after.snapshot == before.snapshot
    assert plan_names(store) == ["control_mode", "soc_target", "grid_charge", "setpoint"]
    assert ("grid_charge", True) in gateway.calls


# --- test plan item 18: the policy write is a dispatch-table write -------------------------------


async def test_a_policy_write_is_refused_while_an_operation_of_that_device_is_active(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    status = await dispatch.submit("main", hold(clock))

    with pytest.raises(CapabilityConflict) as excinfo:
        await dispatch.set_soc_target_policy("main", SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC))

    assert excinfo.value.operation_id == status.operation_id
    assert excinfo.value.mode is DispatchMode.HOLD
    # Refused means unchanged: the derivation the running operation applied under stays in force.
    assert dispatch.soc_target_policy("main").mode is SocTargetMode.BUSINESS_TARGET

    await dispatch.cancel("main")
    policy = SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC, note="candidate only")
    await dispatch.set_soc_target_policy("main", policy)
    assert dispatch.soc_target_policy("main") == policy
    # Persisted, not only taken over in memory.
    assert dispatch._store.get_soc_target_policies()["main"] == policy


async def test_a_policy_for_another_device_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    with pytest.raises(ValueError, match="another device"):
        await dispatch.set_soc_target_policy("main", SocTargetPolicy("slave1"))
    assert dispatch._store.get_soc_target_policies() == {}


async def test_a_policy_change_reaches_the_adapter_without_a_restart(tmp_path: Path) -> None:
    """The controller and the adapter share one registry instance, so the next apply derives from
    the new policy — a second instance would be a second truth."""
    clock = ManualClock()
    policies = SocTargetPolicyRegistry()
    gateway = FakeDispatchGateway(clock, soc_target_policies=policies)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.set_soc_target_policy(
        "main", SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC, below_margin_percent=10.0)
    )
    assert policies.policy("main").mode is SocTargetMode.BELOW_CURRENT_SOC
    await dispatch.submit("main", charge(clock, target=80.0))
    assert ("soc_target", pytest.approx(0.4)) in gateway.calls


# --- the gate is per device, for HOLD as for every other mode ------------------------------------


async def test_a_hold_on_an_unreleased_device_stays_blocked(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(
        tmp_path,
        clock,
        gateway,
        capabilities=verified_registry("main"),
        limits={"main": DeviceLimits(3000, 5000), "slave1": DeviceLimits(3000, 5000)},
    )
    assert (await dispatch.submit("main", hold(clock))).state is DispatchState.HOLDING
    with pytest.raises(DispatchRejected, match="dispatch_unverified"):
        await dispatch.submit("slave1", hold(clock))
