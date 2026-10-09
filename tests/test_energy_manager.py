#!/usr/bin/env python3
#
# tests/test_energy_manager.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The Energy Manager's business projection of the dispatch layer.

The three projection tables are a published contract, so they must be total: a dispatch state, mode
or stop reason added later must fail a test here rather than leak an internal name — or a KeyError —
into a public response.
"""

import pytest

from app.dispatch.models import DispatchCommand, DispatchMode, DispatchState, StopReason
from app.energy.models import (
    _ACTION_FOR_MODE,
    _STATE_FOR_DISPATCH_STATE,
    _STOP_REASON_PUBLIC,
    ADMIN_ACTOR,
    CHANGED_BY_MAX_LENGTH,
    COMMAND_TTL_SECONDS,
    TARGET_SOC_MAX_PERCENT,
    TARGET_SOC_MIN_PERCENT,
    EnergyAction,
    EnergyCommand,
    EnergyMode,
    EnergyState,
    ModeRecord,
)

# The ten published values, written out here so a change to the contract has to be made twice.
PUBLISHED_STOP_REASONS = {
    "target_reached",
    "time_limit_reached",
    "telemetry_unavailable",
    "stopped_by_operator",
    "device_state_unreadable",
    "write_not_permitted",
    "device_error",
    "service_unavailable",
    "service_shutdown",
    "device_changed",
}


@pytest.mark.parametrize("state", list(DispatchState))
def test_every_dispatch_state_has_a_business_state(state: DispatchState) -> None:
    assert isinstance(_STATE_FOR_DISPATCH_STATE[state], EnergyState)


def test_the_business_states_are_the_expected_grouping() -> None:
    assert _STATE_FOR_DISPATCH_STATE[DispatchState.IDLE] is EnergyState.AUTOMATIC
    assert _STATE_FOR_DISPATCH_STATE[DispatchState.HOLDING] is EnergyState.HOLDING
    assert _STATE_FOR_DISPATCH_STATE[DispatchState.REPLACING] is EnergyState.STARTING
    assert _STATE_FOR_DISPATCH_STATE[DispatchState.TARGET_REACHED] is EnergyState.STOPPING
    assert _STATE_FOR_DISPATCH_STATE[DispatchState.RESTORING] is EnergyState.STOPPING
    assert _STATE_FOR_DISPATCH_STATE[DispatchState.FAULT_RESTORE_PENDING] is EnergyState.FAULT


@pytest.mark.parametrize("mode", list(DispatchMode))
def test_every_dispatch_mode_is_covered_by_the_action_table(mode: DispatchMode) -> None:
    """Covered, not necessarily an action: export_to_grid maps to None on purpose, and the lookup
    must stay a .get() so an unmapped future mode degrades to null instead of a 500."""
    assert mode in _ACTION_FOR_MODE
    mapped = _ACTION_FOR_MODE[mode]
    assert mapped is None or isinstance(mapped, EnergyAction)


def test_export_to_grid_is_not_an_energy_manager_action() -> None:
    assert _ACTION_FOR_MODE[DispatchMode.EXPORT_TO_GRID] is None
    assert _ACTION_FOR_MODE[DispatchMode.HOLD] is EnergyAction.HOLD
    assert _ACTION_FOR_MODE.get(DispatchMode.CHARGE_FROM_GRID) is EnergyAction.CHARGE
    assert _ACTION_FOR_MODE.get(DispatchMode.DISCHARGE_TO_LOAD) is EnergyAction.DISCHARGE


@pytest.mark.parametrize("reason", list(StopReason))
def test_every_stop_reason_has_a_business_value(reason: StopReason) -> None:
    assert _STOP_REASON_PUBLIC[reason] in PUBLISHED_STOP_REASONS


def test_the_published_stop_reason_vocabulary_is_exactly_the_table() -> None:
    assert set(_STOP_REASON_PUBLIC.values()) == PUBLISHED_STOP_REASONS
    assert len(_STOP_REASON_PUBLIC) == len(StopReason) == 10


def test_no_internal_stop_reason_name_leaks_into_the_public_vocabulary() -> None:
    """`ttl_expired`, `telemetry_stale`, `operator_cancelled`, `snapshot_stale` and `shutdown` are
    internal names; the published value says the same thing in business language."""
    leaking = {
        StopReason.TTL_EXPIRED,
        StopReason.TELEMETRY_STALE,
        StopReason.OPERATOR_CANCELLED,
        StopReason.SNAPSHOT_STALE,
        StopReason.SHUTDOWN,
    }
    assert all(_STOP_REASON_PUBLIC[reason] != reason.value for reason in leaking)


# --- the command model and the mode record ------------------------------------------------------


def test_the_command_model_carries_business_values_only() -> None:
    command = EnergyCommand(EnergyAction.CHARGE, target_soc_percent=80)
    assert command.max_power_w is None  # absent means "the per-device configured limit"
    assert EnergyCommand(EnergyAction.HOLD).target_soc_percent is None
    assert TARGET_SOC_MIN_PERCENT == 7.0
    assert TARGET_SOC_MAX_PERCENT == 97.0
    assert COMMAND_TTL_SECONDS == 3600.0


def test_a_mode_record_defaults_to_off() -> None:
    record = ModeRecord("main")
    assert record.mode is EnergyMode.OFF and record.armed is False
    assert record.added_write_names == ()
    assert record.changed_at is None and record.changed_by is None


def test_changed_by_is_bounded_printable_ascii() -> None:
    ModeRecord("main", mode=EnergyMode.MANUAL, changed_by="a" * CHANGED_BY_MAX_LENGTH)
    with pytest.raises(ValueError, match="at most"):
        ModeRecord("main", mode=EnergyMode.MANUAL, changed_by="a" * (CHANGED_BY_MAX_LENGTH + 1))
    with pytest.raises(ValueError, match="printable ASCII"):
        ModeRecord("main", mode=EnergyMode.MANUAL, changed_by="admin\n")


# --- the service: translation, validation, arming, availability -----------------------------------
#
# The fixtures below give the manager a *recording* port instead of a device, because every
# guarantee of design 2.3.1 is about what the manager does before anything reaches hardware. The
# required register names are injected as vendor-neutral placeholders on purpose: the manager must
# work without knowing a single RCT name.

import asyncio
from datetime import timedelta
from pathlib import Path

from app.dispatch.capabilities import (
    CapabilityName,
    CapabilityRecord,
    CapabilityRegistry,
)
from app.dispatch.controller import DispatchRejected
from app.dispatch.models import (
    DeviceLimits,
    DispatchConfig,
    DispatchPhase,
    DispatchStatus,
    PowerDirection,
)
from app.dispatch.soc_policy import SocTargetPolicy
from app.dispatch.store import DispatchStore
from app.energy.errors import EnergyRejected
from app.energy.manager import EnergyManager
from app.energy.models import ActionReason, EnergyDeviceStatus
from app.energy.readings import DeviceReading, EnergyReadings, absent_readings
from app.errors import DeviceApiError, UnknownDevice
from tests.conftest import ManualClock
from tests.test_dispatch_core import FakeDispatchGateway, controller, verified_registry

DEFAULT_LIMITS = DeviceLimits(3000, 5000)
REQUIRED_WRITES = ("write_alpha", "write_beta")
# Deliberately NOT alphabetical: the arming union has to preserve the operator's own order, which a
# set-based implementation cannot do.
EXISTING_WRITES = ("zulu_write", "alpha_write")


def dispatch_status(**overrides) -> DispatchStatus:
    base = {
        "device_id": "main",
        "operation_id": None,
        "mode": None,
        "state": DispatchState.IDLE,
        "phase": DispatchPhase.IDLE,
        "control_state": "restored",
        "restore_required": False,
        "target_soc_percent": None,
        "max_power_w": 0.0,
        "max_power_w_requested": 0.0,
        "valid_until": None,
        "commanded_direction": PowerDirection.NONE,
        "commanded_power_w": 0.0,
    }
    return DispatchStatus(**{**base, **overrides})


class SpyPort:
    """A dispatch port that records what it was asked to do and touches no device at all."""

    def __init__(
        self,
        *,
        limits: DeviceLimits | None = DEFAULT_LIMITS,
        capabilities: CapabilityRegistry | None = None,
        status: DispatchStatus | None = None,
        cancel_raises: DeviceApiError | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.submitted: list[DispatchCommand] = []
        self._limits = limits
        self._capabilities = capabilities if capabilities is not None else verified_registry("main")
        self._status = status or dispatch_status()
        self._cancel_raises = cancel_raises

    async def submit(self, device_id: str, command: DispatchCommand) -> DispatchStatus:
        self.calls.append("submit")
        self.submitted.append(command)
        state = {
            DispatchMode.CHARGE_FROM_GRID: DispatchState.CHARGING,
            DispatchMode.DISCHARGE_TO_LOAD: DispatchState.DISCHARGING,
            DispatchMode.HOLD: DispatchState.HOLDING,
        }[command.mode]
        direction = {
            DispatchMode.CHARGE_FROM_GRID: PowerDirection.CHARGE,
            DispatchMode.DISCHARGE_TO_LOAD: PowerDirection.DISCHARGE,
            DispatchMode.HOLD: PowerDirection.NONE,
        }[command.mode]
        watts = 0.0 if command.mode is DispatchMode.HOLD else command.max_power_w
        return dispatch_status(
            operation_id="op-1",
            mode=command.mode,
            state=state,
            phase=DispatchPhase.CONTROLLING,
            control_state="controlled",
            target_soc_percent=command.target_soc_percent,
            max_power_w=command.max_power_w,
            max_power_w_requested=command.max_power_w,
            valid_until=command.valid_until,
            commanded_direction=direction,
            commanded_power_w=watts,
        )

    async def cancel(self, device_id: str) -> DispatchStatus:
        self.calls.append("cancel")
        if self._cancel_raises is not None:
            raise self._cancel_raises
        return self._status

    async def status(self, device_id: str) -> DispatchStatus:
        self.calls.append("status")
        return self._status

    def capabilities(self, device_id: str) -> tuple[CapabilityRecord, ...]:
        return self._capabilities.all(device_id)

    def device_limits(self, device_id: str) -> DeviceLimits | None:
        return self._limits

    def soc_target_policy(self, device_id: str) -> SocTargetPolicy:
        return SocTargetPolicy(device_id=device_id)


class StubReadings:
    def __init__(self, readings: EnergyReadings | None = None, *, raises: bool = False) -> None:
        self._readings = readings or absent_readings()
        self._raises = raises

    def readings(self, device_id: str) -> EnergyReadings:
        if self._raises:
            raise RuntimeError("the cache blew up")
        return self._readings


class ApprovalSpy:
    """Stands in for the Inverters page's own write-name persistence."""

    def __init__(self, existing: tuple[str, ...] = EXISTING_WRITES) -> None:
        self.names = list(existing)
        self.calls: list[list[str]] = []

    async def approve(self, names) -> list[str]:
        self.calls.append(list(names))
        self.names = list(names)
        return self.names

    async def read(self) -> tuple[str, ...]:
        return tuple(self.names)


class RecordingStore:
    def __init__(self) -> None:
        self.records: list[ModeRecord] = []

    def put_energy_state(self, record: ModeRecord) -> None:
        self.records.append(record)


def manager(
    port,
    *,
    mode: EnergyMode = EnergyMode.EXTERNAL,
    store=None,
    approvals: ApprovalSpy | None = None,
    write_support: bool = True,
    candidates: tuple[str, ...] | None = None,
    readings=None,
    clock: ManualClock | None = None,
    config: DispatchConfig | None = None,
    approved_writes: bool = True,
    devices: tuple[str, ...] = ("main", "slave1"),
) -> EnergyManager:
    spy = approvals or ApprovalSpy()
    offered = REQUIRED_WRITES if candidates is None else candidates
    return EnergyManager(
        port=port,
        store=store if store is not None else RecordingStore(),
        readings=readings or StubReadings(),
        clock=clock or ManualClock(),
        config=config or DispatchConfig(),
        devices={device_id: object() for device_id in devices},
        write_support_enabled=write_support,
        approve_writes=spy.approve,
        approved_writes=spy.read if approved_writes else None,
        allowlist_candidates=lambda: frozenset(offered),
        required_writes=REQUIRED_WRITES,
        modes={"main": ModeRecord("main", mode=mode)},
    )


def active_manager(port, **kwargs) -> EnergyManager:
    """A device in external mode whose required register names are approved — the normal case."""
    spy = kwargs.pop("approvals", ApprovalSpy(EXISTING_WRITES + REQUIRED_WRITES))
    return manager(port, approvals=spy, **kwargs)


# --- item 4: the action translation table ---------------------------------------------------------


async def test_charge_translates_to_a_grid_charge_with_the_configured_power_limit() -> None:
    clock = ManualClock()
    port = SpyPort()
    status = await active_manager(port, clock=clock).command(
        "main", EnergyCommand(EnergyAction.CHARGE, 80.0), actor="tester"
    )
    command = port.submitted[0]
    assert command.mode is DispatchMode.CHARGE_FROM_GRID
    assert command.target_soc_percent == 80.0
    assert command.max_power_w == 3000.0  # absent max_power_w -> the device's charge limit
    assert command.valid_until == clock.now() + timedelta(seconds=COMMAND_TTL_SECONDS)
    assert status.state is EnergyState.CHARGING
    assert status.action is EnergyAction.CHARGE


async def test_discharge_defaults_to_the_discharge_limit() -> None:
    port = SpyPort()
    await active_manager(port).command(
        "main", EnergyCommand(EnergyAction.DISCHARGE, 30.0), actor=None
    )
    command = port.submitted[0]
    assert command.mode is DispatchMode.DISCHARGE_TO_LOAD
    assert command.target_soc_percent == 30.0
    assert command.max_power_w == 5000.0


async def test_an_explicit_power_is_handed_down_unchanged() -> None:
    """Clamping is the dispatch layer's job; the manager must not pre-empt it."""
    port = SpyPort()
    await active_manager(port).command(
        "main", EnergyCommand(EnergyAction.CHARGE, 80.0, 1500.0), actor=None
    )
    assert port.submitted[0].max_power_w == 1500.0


async def test_hold_carries_no_target_and_no_power_budget() -> None:
    port = SpyPort()
    status = await active_manager(port).command("main", EnergyCommand(EnergyAction.HOLD), actor=None)
    command = port.submitted[0]
    assert command.mode is DispatchMode.HOLD
    assert command.target_soc_percent is None
    assert command.max_power_w == 0.0
    assert status.state is EnergyState.HOLDING
    # A hold has no power budget: 0 W as a *limit* would read as "limited to zero".
    assert status.power_limit_w is None
    assert status.commanded_power_w == 0.0
    assert status.commanded_direction is PowerDirection.NONE


async def test_auto_cancels_instead_of_submitting() -> None:
    port = SpyPort()
    status = await active_manager(port).command("main", EnergyCommand(EnergyAction.AUTO), actor=None)
    assert port.calls == ["cancel"]
    assert port.submitted == []
    assert status.state is EnergyState.AUTOMATIC
    assert status.action is None


async def test_auto_on_an_idle_device_is_not_an_error() -> None:
    """cancel() raises dispatch_not_found on an idle device; the handback is idempotent."""
    port = SpyPort(cancel_raises=DispatchRejected("dispatch_not_found"))
    status = await active_manager(port).command("main", EnergyCommand(EnergyAction.AUTO), actor=None)
    assert port.calls == ["cancel", "status"]
    assert status.state is EnergyState.AUTOMATIC


async def test_a_dispatch_refusal_passes_through_unchanged() -> None:
    port = SpyPort(cancel_raises=DispatchRejected("dispatch_record_corrupt"))
    with pytest.raises(DispatchRejected) as info:
        await active_manager(port).command("main", EnergyCommand(EnergyAction.AUTO), actor=None)
    assert info.value.code == "dispatch_record_corrupt"


async def test_a_clamped_power_limit_is_reported_as_clamped() -> None:
    port = SpyPort(
        status=dispatch_status(
            operation_id="op-1",
            mode=DispatchMode.CHARGE_FROM_GRID,
            state=DispatchState.CHARGING,
            phase=DispatchPhase.CONTROLLING,
            control_state="controlled",
            target_soc_percent=80.0,
            max_power_w=3000.0,
            max_power_w_requested=4500.0,
            commanded_direction=PowerDirection.CHARGE,
            commanded_power_w=3000.0,
        )
    )
    status = await active_manager(port).status("main")
    assert status.power_limit_w == 3000.0
    assert status.power_limit_clamped is True


async def test_the_published_window_is_the_narrower_of_the_hard_and_configured_bounds() -> None:
    status = await active_manager(SpyPort(), config=DispatchConfig(min_soc=10.0, max_soc=90.0)).status("main")
    assert (status.target_soc_window.min, status.target_soc_window.max) == (10.0, 90.0)


async def test_a_reading_that_cannot_be_read_does_not_fail_the_status() -> None:
    status = await active_manager(SpyPort(), readings=StubReadings(raises=True)).status("main")
    assert status.readings.battery_soc_percent.value is None
    assert status.readings.battery_soc_percent.stale is True


async def test_the_readings_block_is_carried_through_verbatim() -> None:
    readings = EnergyReadings(
        DeviceReading(54.0, 2.0, False),
        DeviceReading(-1200.0, 3.0, False),
        DeviceReading(2400.0, 1.0, False),
        DeviceReading(800.0, 4.0, True),
    )
    status = await active_manager(SpyPort(), readings=StubReadings(readings)).status("main")
    assert status.readings is readings


# --- item 5: validation, with no port call on refusal ---------------------------------------------


@pytest.mark.parametrize(
    ("command", "code"),
    [
        (EnergyCommand(EnergyAction.CHARGE), "invalid_request"),  # target required
        (EnergyCommand(EnergyAction.DISCHARGE), "invalid_request"),
        (EnergyCommand(EnergyAction.HOLD, 80.0), "invalid_request"),  # target forbidden
        (EnergyCommand(EnergyAction.AUTO, 80.0), "invalid_request"),
        (EnergyCommand(EnergyAction.HOLD, None, 1000.0), "invalid_request"),  # power forbidden
        (EnergyCommand(EnergyAction.AUTO, None, 1000.0), "invalid_request"),
        (EnergyCommand(EnergyAction.CHARGE, 3.0), "value_out_of_range"),  # below the hard bound
        (EnergyCommand(EnergyAction.CHARGE, 99.0), "value_out_of_range"),  # above the hard bound
        (EnergyCommand(EnergyAction.CHARGE, 96.0), "value_out_of_range"),  # outside the window
        (EnergyCommand(EnergyAction.CHARGE, float("nan")), "value_not_finite"),
        (EnergyCommand(EnergyAction.CHARGE, float("inf")), "value_not_finite"),
        (EnergyCommand(EnergyAction.CHARGE, 80.0, 0.0), "value_out_of_range"),
        (EnergyCommand(EnergyAction.CHARGE, 80.0, -500.0), "value_out_of_range"),
        (EnergyCommand(EnergyAction.CHARGE, 80.0, 50_001.0), "value_out_of_range"),
        (EnergyCommand(EnergyAction.CHARGE, 80.0, float("nan")), "value_not_finite"),
    ],
)
async def test_every_validation_row_refuses_before_any_device_access(
    command: EnergyCommand, code: str
) -> None:
    port = SpyPort()
    with pytest.raises(EnergyRejected) as info:
        await active_manager(port).command("main", command, actor=None)
    assert info.value.code == code
    assert port.calls == []  # not even a status read


async def test_an_unknown_device_is_refused_before_the_mode_check() -> None:
    port = SpyPort()
    with pytest.raises(UnknownDevice):
        await active_manager(port).command("nope", EnergyCommand(EnergyAction.AUTO), actor=None)
    assert port.calls == []


async def test_a_command_without_a_dispatch_port_is_a_service_refusal() -> None:
    with pytest.raises(EnergyRejected) as info:
        await active_manager(None).command("main", EnergyCommand(EnergyAction.AUTO), actor=None)
    assert info.value.code == "dispatch_store_unavailable"


async def test_a_device_in_mode_off_refuses_every_action_including_auto() -> None:
    port = SpyPort()
    service = manager(port, mode=EnergyMode.OFF)
    for action in EnergyAction:
        with pytest.raises(EnergyRejected) as info:
            await service.command("main", EnergyCommand(
                action, 80.0 if action in (EnergyAction.CHARGE, EnergyAction.DISCHARGE) else None
            ), actor=None)
        assert info.value.code == "energy_manager_off"
    assert port.calls == []


async def test_a_missing_device_limit_is_refused_before_the_submit() -> None:
    port = SpyPort(limits=None)
    with pytest.raises(EnergyRejected) as info:
        await active_manager(port).command("main", EnergyCommand(EnergyAction.CHARGE, 80.0), actor=None)
    assert info.value.code == "dispatch_limits_missing"
    assert port.calls == []


# --- item 6: arming -------------------------------------------------------------------------------


async def test_switching_on_adds_the_required_names_and_keeps_the_existing_order() -> None:
    spy = ApprovalSpy(EXISTING_WRITES)
    store = RecordingStore()
    service = manager(SpyPort(), mode=EnergyMode.OFF, approvals=spy, store=store)
    status = await service.set_mode("main", EnergyMode.MANUAL, actor="tester")
    assert spy.calls == [["zulu_write", "alpha_write", "write_alpha", "write_beta"]]
    assert status.armed is True
    assert store.records[-1].added_write_names == REQUIRED_WRITES
    assert store.records[-1].changed_by == "tester"
    assert store.records[-1].changed_at is not None


async def test_switching_on_twice_changes_nothing() -> None:
    spy = ApprovalSpy(EXISTING_WRITES)
    store = RecordingStore()
    service = manager(SpyPort(), mode=EnergyMode.OFF, approvals=spy, store=store)
    await service.set_mode("main", EnergyMode.MANUAL, actor="tester")
    first = store.records[-1]
    await service.set_mode("main", EnergyMode.MANUAL, actor="someone-else")
    assert len(spy.calls) == 1  # no second approval
    assert store.records[-1] is first  # no second commit, no new timestamp, no new actor
    assert service.mode("main") is not EnergyMode.OFF


async def test_switching_on_with_write_support_off_approves_nothing() -> None:
    spy = ApprovalSpy(EXISTING_WRITES)
    service = manager(SpyPort(), mode=EnergyMode.OFF, approvals=spy, write_support=False)
    with pytest.raises(EnergyRejected) as info:
        await service.set_mode("main", EnergyMode.MANUAL, actor="tester")
    assert info.value.code == "energy_write_support_required"
    assert spy.calls == []
    assert spy.names == list(EXISTING_WRITES)  # the recorded names are untouched
    assert service.mode("main") is EnergyMode.OFF


async def test_switching_on_is_refused_when_the_allowlist_file_lacks_a_required_register() -> None:
    """A custom WRITE_ALLOWLIST_PATH is operator-settable, so the shipped default is no guarantee.
    Without this check the allowlist build raises a KeyError and the operator gets a 500."""
    spy = ApprovalSpy(EXISTING_WRITES)
    service = manager(
        SpyPort(), mode=EnergyMode.OFF, approvals=spy, candidates=("write_alpha",)  # write_beta missing
    )
    with pytest.raises(EnergyRejected) as info:
        await service.set_mode("main", EnergyMode.MANUAL, actor="tester")
    assert info.value.code == "energy_write_support_required"
    assert info.value.context["missing"] == ["write_beta"]
    assert spy.calls == []  # nothing approved
    assert service.mode("main") is EnergyMode.OFF


async def test_switching_on_without_device_limits_is_refused() -> None:
    service = manager(SpyPort(limits=None), mode=EnergyMode.OFF)
    with pytest.raises(EnergyRejected) as info:
        await service.set_mode("main", EnergyMode.MANUAL, actor="tester")
    assert info.value.code == "dispatch_limits_missing"


async def test_switching_off_removes_no_write_approval() -> None:
    spy = ApprovalSpy(EXISTING_WRITES)
    store = RecordingStore()
    service = manager(SpyPort(), mode=EnergyMode.OFF, approvals=spy, store=store)
    await service.set_mode("main", EnergyMode.MANUAL, actor="tester")
    approved_after_arming = list(spy.names)
    status = await service.set_mode("main", EnergyMode.OFF, actor="tester")
    assert status.armed is False
    assert spy.names == approved_after_arming  # the four registers stay approved
    assert len(spy.calls) == 1
    assert store.records[-1].armed is False


async def test_changed_by_is_truncated_and_cleaned_instead_of_raising() -> None:
    store = RecordingStore()
    service = manager(SpyPort(), mode=EnergyMode.OFF, store=store)
    await service.set_mode("main", EnergyMode.MANUAL, actor="t\nken-" + "x" * 200)
    assert store.records[-1].changed_by == ("tken-" + "x" * 200)[:64]


async def test_an_actor_without_a_usable_name_is_recorded_as_none() -> None:
    store = RecordingStore()
    service = manager(SpyPort(), mode=EnergyMode.OFF, store=store)
    await service.set_mode("main", EnergyMode.MANUAL, actor=None)
    assert store.records[-1].changed_by is None


# --- item 6b: a revoked approval ------------------------------------------------------------------


async def test_auto_stays_allowed_without_write_approval_but_never_submits_a_new_dispatch() -> None:
    """Invariant: `auto` only ends the manager's own operation (restores the stored snapshot) and
    starts no new write, so a revoked approval refuses charge/discharge/hold but never `auto`.
    (The gateway allowlist still guards the restore writes themselves; that is not tested here.)"""
    port = SpyPort()
    # Armed, but the operator removed the register approvals on the Inverters page afterwards.
    service = manager(port, approvals=ApprovalSpy(EXISTING_WRITES))
    status = await service.status("main")
    available = {item.action: item.available for item in status.actions}
    assert available.pop(EnergyAction.AUTO) is True
    assert not any(available.values())
    assert {item.reason for item in status.actions if item.action is not EnergyAction.AUTO} == {
        ActionReason.WRITE_NOT_PERMITTED
    }
    for refused in (EnergyCommand(EnergyAction.CHARGE, 80.0), EnergyCommand(EnergyAction.DISCHARGE, 20.0), EnergyCommand(EnergyAction.HOLD)):
        with pytest.raises(EnergyRejected) as info:
            await service.command("main", refused, actor=None)
        assert info.value.code == "energy_action_unavailable"
    assert port.calls == ["status"]  # only the status GET above, no device access for the refusals
    await service.command("main", EnergyCommand(EnergyAction.AUTO), actor=None)
    assert "submit" not in port.calls and port.submitted == []  # the handback is a cancel, never a submit


async def test_without_a_reader_the_approval_check_is_skipped() -> None:
    """With no allowlist to consult the write layer refuses on its own; the manager does not guess."""
    port = SpyPort()
    service = manager(port, approvals=ApprovalSpy(()), approved_writes=False)
    status = await service.command("main", EnergyCommand(EnergyAction.CHARGE, 80.0), actor=None)
    assert status.state is EnergyState.CHARGING


# --- item 6c: availability on a disarmed device ----------------------------------------------------


async def test_a_device_in_mode_off_reports_all_four_actions_as_mode_off() -> None:
    """The AC-7/AC-8 consistency pin: no enabled button may answer 409 energy_manager_off."""
    status = await manager(SpyPort(), mode=EnergyMode.OFF).status("main")
    assert [item.action for item in status.actions] == list(EnergyAction)
    assert all(item.available is False for item in status.actions)
    assert all(item.reason is ActionReason.MODE_OFF for item in status.actions)


async def test_auto_stays_available_on_an_armed_device_with_unverified_hardware() -> None:
    """Handing control back must never depend on a capability being verified."""
    blocked = CapabilityRegistry(
        [CapabilityRecord(device_id="main", name=name) for name in CapabilityName]
    )
    status = await active_manager(SpyPort(capabilities=blocked)).status("main")
    offered = {item.action: item for item in status.actions}
    assert offered[EnergyAction.AUTO].available is True
    assert offered[EnergyAction.CHARGE].available is False
    assert offered[EnergyAction.CHARGE].reason is ActionReason.HARDWARE_NOT_VERIFIED
    assert offered[EnergyAction.HOLD].reason is ActionReason.HARDWARE_NOT_VERIFIED


async def test_missing_limits_block_the_three_control_actions_but_not_auto() -> None:
    status = await active_manager(SpyPort(limits=None)).status("main")
    offered = {item.action: item for item in status.actions}
    assert offered[EnergyAction.AUTO].available is True
    assert offered[EnergyAction.DISCHARGE].reason is ActionReason.LIMITS_MISSING


async def test_a_pending_restore_leaves_auto_as_the_retry() -> None:
    port = SpyPort(
        status=dispatch_status(
            operation_id="op-1",
            mode=DispatchMode.CHARGE_FROM_GRID,
            state=DispatchState.FAULT_RESTORE_PENDING,
            phase=DispatchPhase.FAULT,
            control_state="unknown",
            restore_required=True,
        )
    )
    status = await active_manager(port).status("main")
    offered = {item.action: item for item in status.actions}
    assert status.state is EnergyState.FAULT
    assert offered[EnergyAction.AUTO].available is True
    assert offered[EnergyAction.CHARGE].reason is ActionReason.RESTORE_REQUIRED


# --- items 17 and 17b: a failing restore, against the real controller -----------------------------


def live_manager(port, store: DispatchStore, clock: ManualClock, mode: EnergyMode = EnergyMode.EXTERNAL) -> EnergyManager:
    spy = ApprovalSpy(EXISTING_WRITES + REQUIRED_WRITES)
    return EnergyManager(
        port=port,
        store=store,
        readings=StubReadings(),
        clock=clock,
        config=DispatchConfig(),
        devices={"main": object()},
        write_support_enabled=True,
        approve_writes=spy.approve,
        approved_writes=spy.read,
        allowlist_candidates=lambda: frozenset(REQUIRED_WRITES),
        required_writes=REQUIRED_WRITES,
        modes={"main": ModeRecord("main", mode=mode)},
    )


async def charge_then_break_the_restore(tmp_path: Path):
    """A charging device in external mode whose next restore write fails at the device."""
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    port = controller(tmp_path, clock, gateway)
    service = live_manager(port, port._store, clock)
    await service.command("main", EnergyCommand(EnergyAction.CHARGE, 80.0), actor="tester")
    gateway.fail_restore = True
    return service, port, gateway, clock


async def test_energy_command_auto_with_failed_restore_rejects(tmp_path: Path) -> None:
    """AC-7b: a handback that did not reach the device must never be reported as success.

    DispatchController.cancel() swallows the device error inside _restore(), leaves the record in
    FAULT_RESTORE_PENDING and returns normally — so a bare projection would answer 200 to an
    operator whose inverter is still under external control with an unrestored setpoint.
    """
    service, port, gateway, clock = await charge_then_break_the_restore(tmp_path)
    with pytest.raises(EnergyRejected) as info:
        await service.command("main", EnergyCommand(EnergyAction.AUTO), actor="tester")
    assert info.value.code == "dispatch_restore_required"

    record = port._store.get("main")
    assert record.state is DispatchState.FAULT_RESTORE_PENDING
    assert record.restore_required is True

    # The automatic retry keeps running: a following tick() still attempts the restore.
    attempts_before = sum(1 for call in gateway.calls if call[0] == "restore")
    clock.advance(1.0)
    await port.tick("main")
    assert sum(1 for call in gateway.calls if call[0] == "restore") > attempts_before
    assert port._store.get("main").state is DispatchState.FAULT_RESTORE_PENDING

    # And the device still reports the fault rather than a clean automatic state.
    status = await service.status("main")
    assert status.state is EnergyState.FAULT
    assert status.armed is True


async def test_auto_with_a_succeeding_restore_answers_the_automatic_state(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    port = controller(tmp_path, clock, gateway)
    service = live_manager(port, port._store, clock)
    await service.command("main", EnergyCommand(EnergyAction.CHARGE, 80.0), actor="tester")
    status = await service.command("main", EnergyCommand(EnergyAction.AUTO), actor="tester")
    assert status.state is EnergyState.AUTOMATIC
    assert status.action is None
    assert status.stop_reason == "stopped_by_operator"


async def test_switching_off_with_a_failing_restore_keeps_the_mode(tmp_path: Path) -> None:
    """Reporting "off" while the device is still under external control would hide exactly the
    state an operator has to act on."""
    service, port, _gateway, _clock = await charge_then_break_the_restore(tmp_path)
    with pytest.raises(EnergyRejected) as info:
        await service.set_mode("main", EnergyMode.OFF, actor="tester")
    assert info.value.code == "dispatch_restore_required"
    assert service.mode("main") is not EnergyMode.OFF
    assert port._store.get("main").state is DispatchState.FAULT_RESTORE_PENDING


async def test_switching_off_a_device_that_is_off_leaves_an_expert_dispatch_alone(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    port = controller(tmp_path, clock, gateway)
    await live_manager(port, port._store, clock).command(
        "main", EnergyCommand(EnergyAction.CHARGE, 80.0), actor="tester"
    )
    calls = list(gateway.calls)
    off_manager = live_manager(port, port._store, clock, mode=EnergyMode.OFF)
    await off_manager.set_mode("main", EnergyMode.OFF, actor="tester")
    assert port._store.get("main").state is not DispatchState.IDLE
    assert gateway.calls == calls  # no cancel, no restore write


async def test_an_already_reached_target_is_accepted_without_a_device_write(tmp_path: Path) -> None:
    """AC-25: the manager does not pre-check the SoC. strategy.target_reached() stays the single
    authority, and the published stop_reason says precisely why nothing happened."""
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)  # read_soc() answers 50 %
    port = controller(tmp_path, clock, gateway)
    service = live_manager(port, port._store, clock)
    status = await service.command("main", EnergyCommand(EnergyAction.CHARGE, 40.0), actor="tester")
    assert status.state is EnergyState.AUTOMATIC
    assert status.action is None
    assert status.stop_reason == "target_reached"
    assert gateway.calls == [("read_soc", "main")]  # no write at all


async def test_the_mode_is_committed_before_memory_changes(tmp_path: Path) -> None:
    """A failed commit must leave the device in mode off, the way set_capability() does it."""

    class FailingStore:
        def put_energy_state(self, record: ModeRecord) -> None:
            raise DeviceApiError("dispatch_store_unavailable")

    service = manager(SpyPort(), mode=EnergyMode.OFF, store=FailingStore())
    with pytest.raises(DeviceApiError):
        await service.set_mode("main", EnergyMode.MANUAL, actor="tester")
    assert service.mode("main") is EnergyMode.OFF


async def test_switching_on_and_a_command_of_one_device_are_serialised() -> None:
    """One lock per device, strictly outside the controller's: the two must not interleave."""
    port = SpyPort()
    service = active_manager(port)
    await asyncio.gather(
        service.command("main", EnergyCommand(EnergyAction.CHARGE, 80.0), actor=None),
        service.command("main", EnergyCommand(EnergyAction.HOLD), actor=None),
    )
    assert len(port.submitted) == 2


def test_the_status_type_carries_exactly_the_published_fields() -> None:
    published = {
        "device_id", "mode", "state", "action", "target_soc_percent", "power_limit_w",
        "power_limit_clamped", "commanded_power_w", "commanded_direction", "until", "stop_reason",
        "time_limited", "target_soc_window", "readings", "actions",
    }  # fmt: skip
    assert set(EnergyDeviceStatus.__dataclass_fields__) == published


def test_the_manager_module_names_no_register_and_imports_no_gateway() -> None:
    source = (Path(__file__).resolve().parents[1] / "app" / "energy" / "manager.py").read_text()
    assert "app.gateway" not in source
    assert "power_mng" not in source


async def test_a_failed_arming_commit_rolls_back_only_its_own_approvals() -> None:
    """An approval another feature added while the commit was in flight must survive the rollback."""
    spy = ApprovalSpy(EXISTING_WRITES)

    class FailingStore:
        def put_energy_state(self, record: ModeRecord) -> None:
            spy.names.append("approved_meanwhile")
            raise OSError("disk full")

    service = manager(SpyPort(), mode=EnergyMode.OFF, approvals=spy, store=FailingStore())
    with pytest.raises(OSError):
        await service.set_mode("main", EnergyMode.MANUAL, actor="tester")
    assert spy.names == [*EXISTING_WRITES, "approved_meanwhile"]
    assert service.mode("main") is EnergyMode.OFF


# --- operating modes: who may command ------------------------------------------------------------

PAT_ACTOR = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    ("mode", "actor", "code"),
    [
        (EnergyMode.OFF, ADMIN_ACTOR, "energy_manager_off"),
        (EnergyMode.OFF, PAT_ACTOR, "energy_manager_off"),
        (EnergyMode.MANUAL, ADMIN_ACTOR, None),
        (EnergyMode.MANUAL, PAT_ACTOR, "energy_manager_not_external"),
        (EnergyMode.MANUAL, None, "energy_manager_not_external"),
        (EnergyMode.EXTERNAL, ADMIN_ACTOR, "energy_manager_external"),
        (EnergyMode.EXTERNAL, PAT_ACTOR, None),
        (EnergyMode.EXTERNAL, None, None),
    ],
)
@pytest.mark.parametrize("action", [EnergyAction.HOLD, EnergyAction.AUTO])
async def test_the_mode_decides_which_actor_may_command(mode, actor, code, action) -> None:
    port = SpyPort()
    service = active_manager(port, mode=mode)
    command = EnergyCommand(action)
    if code is None:
        await service.command("main", command, actor=actor)
        return
    with pytest.raises(EnergyRejected) as info:
        await service.command("main", command, actor=actor)
    assert info.value.code == code
    assert port.submitted == [] and "cancel" not in port.calls  # refused before any device access


@pytest.mark.parametrize("mode", list(EnergyMode))
async def test_a_status_read_works_in_every_mode_and_reports_it(mode: EnergyMode) -> None:
    status = await active_manager(SpyPort(), mode=mode).status("main")
    assert status.mode is mode
    assert status.armed is (mode is not EnergyMode.OFF)
    assert status.accepts_commands_from.value == {"off": "none", "manual": "admin", "external": "api"}[mode.value]


async def test_switching_between_manual_and_external_hands_the_inverter_back_first(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    port = controller(tmp_path, clock, gateway)
    service = live_manager(port, port._store, clock, mode=EnergyMode.MANUAL)
    await service.command("main", EnergyCommand(EnergyAction.CHARGE, 80.0), actor=ADMIN_ACTOR)
    assert port._store.get("main").state is not DispatchState.IDLE

    status = await service.set_mode("main", EnergyMode.EXTERNAL, actor=ADMIN_ACTOR)
    assert status.mode is EnergyMode.EXTERNAL
    assert status.state is EnergyState.AUTOMATIC  # the operation of the previous owner is gone
    assert port._store.get("main").state is DispatchState.IDLE
    with pytest.raises(EnergyRejected) as info:
        await service.command("main", EnergyCommand(EnergyAction.HOLD), actor=ADMIN_ACTOR)
    assert info.value.code == "energy_manager_external"


async def test_a_failed_handback_keeps_the_previous_mode(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    port = controller(tmp_path, clock, gateway)
    service = live_manager(port, port._store, clock, mode=EnergyMode.MANUAL)
    await service.command("main", EnergyCommand(EnergyAction.CHARGE, 80.0), actor=ADMIN_ACTOR)
    gateway.fail_restore = True
    with pytest.raises(EnergyRejected) as info:
        await service.set_mode("main", EnergyMode.EXTERNAL, actor=ADMIN_ACTOR)
    assert info.value.code == "dispatch_restore_required"
    assert service.mode("main") is EnergyMode.MANUAL


async def test_switching_off_hands_back_an_operation_of_either_mode(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    port = controller(tmp_path, clock, gateway)
    service = live_manager(port, port._store, clock, mode=EnergyMode.EXTERNAL)
    await service.command("main", EnergyCommand(EnergyAction.CHARGE, 80.0), actor=PAT_ACTOR)
    status = await service.set_mode("main", EnergyMode.OFF, actor=ADMIN_ACTOR)
    assert status.mode is EnergyMode.OFF and status.state is EnergyState.AUTOMATIC


async def test_selecting_the_current_mode_again_changes_nothing() -> None:
    store = RecordingStore()
    service = manager(SpyPort(), mode=EnergyMode.OFF, store=store, approvals=ApprovalSpy(EXISTING_WRITES))
    await service.set_mode("main", EnergyMode.EXTERNAL, actor="first")
    await service.set_mode("main", EnergyMode.EXTERNAL, actor="second")
    assert len(store.records) == 1 and store.records[0].changed_by == "first"


@pytest.mark.parametrize("mode", [EnergyMode.MANUAL, EnergyMode.EXTERNAL])
async def test_both_active_modes_share_the_prerequisites(mode: EnergyMode) -> None:
    spy = ApprovalSpy(EXISTING_WRITES)
    off = manager(SpyPort(), mode=EnergyMode.OFF, approvals=spy, write_support=False)
    with pytest.raises(EnergyRejected) as info:
        await off.set_mode("main", mode, actor=ADMIN_ACTOR)
    assert info.value.code == "energy_write_support_required"
    assert tuple(spy.names) == EXISTING_WRITES  # nothing approved
    no_limits = manager(SpyPort(limits=None), mode=EnergyMode.OFF)
    with pytest.raises(EnergyRejected) as info:
        await no_limits.set_mode("main", mode, actor=ADMIN_ACTOR)
    assert info.value.code == "dispatch_limits_missing"
    assert off.mode("main") is EnergyMode.OFF and no_limits.mode("main") is EnergyMode.OFF
