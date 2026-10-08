#!/usr/bin/env python3
#
# tests/test_dispatch_core.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Battery dispatch strategies, persistence and state-machine safety invariants."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.dispatch.capabilities import (
    CapabilityName,
    CapabilityRecord,
    CapabilityRegistry,
    CapabilityStatus,
)
from app.dispatch.controller import (
    DispatchController,
    DispatchRejected,
    ReconfigurationRejected,
)
from app.dispatch.gating import should_write
from app.dispatch.models import (
    ControlTelemetry,
    DeviceControlSnapshot,
    DeviceLimits,
    DispatchCommand,
    DispatchConfig,
    DispatchIntent,
    DispatchMode,
    DispatchRecord,
    DispatchState,
    PowerDirection,
    PowerSetpoint,
    StopReason,
)
from app.dispatch.soc_policy import SocTargetPolicyRegistry
from app.dispatch.store import DispatchStore
from app.dispatch.strategy import calculate_setpoint
from app.errors import DeviceApiError
from app.gateway.base import WriteOutcome
from app.gateway.conventions import RctSocTargetConvention
from tests.conftest import ManualClock


def telemetry(*, soc: float = 50, grid: float = 1200, battery: float = 0) -> ControlTelemetry:
    return ControlTelemetry(
        soc,
        0,
        "device",
        grid,
        0,
        "device",
        PowerSetpoint(PowerDirection.DISCHARGE, battery) if battery else PowerSetpoint(),
        0,
        "device",
        1200,
        0,
    )


def test_discharge_strategy_tracks_grid_import_without_export() -> None:
    config = DispatchConfig(grid_import_reserve_w=100, grid_control_deadband_w=200)
    setpoint = calculate_setpoint(
        DispatchMode.DISCHARGE_TO_LOAD,
        telemetry(grid=1200),
        target_soc_percent=20,
        max_power_w=5000,
        config=config,
        last=PowerSetpoint(),
    )
    assert setpoint == PowerSetpoint(PowerDirection.DISCHARGE, 1100)
    no_export = calculate_setpoint(
        DispatchMode.DISCHARGE_TO_LOAD,
        telemetry(grid=-50),
        target_soc_percent=20,
        max_power_w=5000,
        config=config,
        last=setpoint,
    )
    assert no_export.watts == 950


def test_charge_strategy_stops_at_target() -> None:
    assert calculate_setpoint(
        DispatchMode.CHARGE_FROM_GRID,
        telemetry(soc=80),
        target_soc_percent=80,
        max_power_w=3000,
        config=DispatchConfig(),
        last=PowerSetpoint(PowerDirection.CHARGE, 3000),
    ) == PowerSetpoint()


def test_dispatch_store_is_full_sync_and_round_trips(tmp_path: Path) -> None:
    store = DispatchStore(tmp_path / "dispatch.db", "s" * 48)
    store.initialize()
    record = DispatchRecord("main", state=DispatchState.PRECHECK)
    store.put(record)
    loaded = store.get("main")
    assert loaded is not None and loaded.state is DispatchState.PRECHECK
    with store.connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_dispatch_store_rejected_put_does_not_mutate_the_caller_record_or_clobber(tmp_path: Path) -> None:
    """A rejected (stale) put() must leave the caller's object untouched, so a naive retry of the
    SAME object cannot later pass the CAS check and overwrite data a concurrent writer committed.
    """
    store = DispatchStore(tmp_path / "dispatch.db", "s" * 48)
    store.initialize()
    stale = DispatchRecord("main", state=DispatchState.PRECHECK)
    store.put(stale)  # version 0 -> 1
    assert stale.record_version == 1

    # A concurrent writer loads its own copy and advances it independently (version 1 -> 2).
    newer = store.get("main")
    assert newer is not None
    newer.state = DispatchState.CHARGING
    newer.intent = DispatchIntent(
        operation_id="concurrent-writer",
        mode=DispatchMode.CHARGE_FROM_GRID,
        target_soc_percent=80.0,
        max_power_w=2000.0,
        max_power_w_requested=2000.0,
        valid_until=datetime(2026, 10, 6, 13, 0, tzinfo=UTC),
        valid_until_requested=datetime(2026, 10, 6, 13, 0, tzinfo=UTC),
        created_at=datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
    )
    store.put(newer)
    assert newer.record_version == 2

    # The original caller still holds its object at version 1 and retries the same put().
    with pytest.raises(RuntimeError, match="stale dispatch record"):
        store.put(stale)
    assert stale.record_version == 1  # unchanged by the rejected attempt

    # The concurrent writer's data must survive: a second put() of the same (still-stale) object
    # must keep failing instead of ever being accepted after the bump the bug used to apply early.
    with pytest.raises(RuntimeError, match="stale dispatch record"):
        store.put(stale)
    assert stale.record_version == 1
    current = store.get("main")
    assert current is not None
    assert current.state is DispatchState.CHARGING
    assert current.record_version == 2


_V1_SCHEMA = """
    CREATE TABLE dispatch_operations (
        device_id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        restore_required INTEGER NOT NULL CHECK(restore_required IN (0,1)),
        updated_at TEXT NOT NULL,
        record_version INTEGER NOT NULL,
        encrypted BLOB NOT NULL
    ) STRICT;
    PRAGMA user_version = 1;
"""

# A row written verbatim by the user_version 1 code: it knows none of the keys later packages add.
_V1_ROW = {
    "device_id": "main",
    "state": "charging",
    "intent": {
        "operation_id": "1f2e3d4c",
        "mode": "charge_from_grid",
        "target_soc_percent": 80.0,
        "max_power_w": 3000.0,
        "max_power_w_requested": 4000.0,
        "valid_until": "2026-10-06T13:00:00+00:00",
        "valid_until_requested": "2026-10-06T14:00:00+00:00",
        "created_at": "2026-10-06T12:00:00+00:00",
    },
    "snapshot": {
        "battery_setpoint": {"direction": "none", "watts": 0.0},
        "soc_target_ratio": 0.5,
        "soc_strategy_code": 4,
        "grid_charge_enabled": False,
        "read_at": "2026-10-06T12:00:00+00:00",
        "all_fresh": True,
    },
    "last_commanded": {"direction": "charge", "watts": 2000.0},
    "last_write_at": "2026-10-06T12:00:05+00:00",
    "stop_reason": None,
    "fault_code": None,
    "restore_required": False,
    "plan": [{"name": "setpoint", "status": "confirmed"}],
    "record_version": 7,
}


def version_1_database(path: Path, secret: str, row: dict | None = None) -> DispatchStore:
    """A dispatch database exactly as the user_version 1 code left it, optionally with one row."""
    store = DispatchStore(path, secret)
    with store.connect() as db:
        db.executescript(_V1_SCHEMA)
        if row is not None:
            db.execute(
                """INSERT INTO dispatch_operations(device_id,state,restore_required,updated_at,
                       record_version,encrypted)
                   VALUES(?,?,?,?,?,?)""",
                (
                    row["device_id"],
                    row["state"],
                    int(row["restore_required"]),
                    "2026-10-06T12:00:05+00:00",
                    row["record_version"],
                    store._encrypt(row["device_id"], row),
                ),
            )
    return store


def test_dispatch_store_upgrade_adds_only_the_new_tables_and_keeps_the_old_row(tmp_path: Path) -> None:
    """The migration must not rewrite dispatch_operations: a persisted restore duty is safety state."""
    store = version_1_database(tmp_path / "dispatch.db", "s" * 48, _V1_ROW)
    store.initialize()
    with store.connect() as db:
        # A version 1 file now lands on the current schema version in one go (see
        # tests/test_energy_store.py for the 2 -> 3 step on its own).
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 3
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"dispatch_operations", "dispatch_capabilities", "dispatch_device_config"} <= tables
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("SELECT record_version FROM dispatch_operations").fetchone()[0] == 7
    loaded = store.get("main")
    assert loaded is not None
    assert loaded.state is DispatchState.CHARGING
    assert loaded.intent is not None and loaded.intent.operation_id == "1f2e3d4c"
    assert loaded.snapshot is not None and loaded.snapshot.soc_strategy_code == 4
    assert loaded.record_version == 7
    store.initialize()  # idempotent: a second start must not fail on the existing tables


def test_dispatch_store_round_trips_capabilities_and_device_configs(tmp_path: Path) -> None:
    store = DispatchStore(tmp_path / "dispatch.db", "s" * 48)
    store.initialize()
    record = CapabilityRecord(
        device_id="main",
        name=CapabilityName.WRITE_PATH,
        status=CapabilityStatus.VERIFIED,
        soc_strategy_external_code=2,
        enum_byte_width=1,
        bool_byte_width=1,
        write_frame_layout_verified=True,
        apply_sequence_verified=True,
        sequence_order_relevant=True,
        verified_device_model="RCT-Power-Storage-DC",
        verified_firmware="1.0.0",
        verified_at=datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
        verified_by="tester",
    )
    store.put_capability(record)
    store.put_capability(CapabilityRecord("slave1", CapabilityName.GRID_POWER_SIGN))
    assert sorted(store.get_capabilities(), key=lambda r: r.device_id) == [record, CapabilityRecord("slave1", CapabilityName.GRID_POWER_SIGN)]

    store.put_device_config("main", DeviceLimits(3000, 5000, engineering_mode=True))
    store.put_device_config("main", DeviceLimits(2000, 4000))  # upsert, not a second row
    assert store.get_device_configs() == {"main": DeviceLimits(2000, 4000, engineering_mode=False)}


def test_dispatch_store_status_and_engineering_mode_stay_readable_without_the_secret(tmp_path: Path) -> None:
    """Whether a device is released, and whether it runs in engineering mode, must be visible even
    when the service is down or HMAC_SECRET was rotated."""
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, "s" * 48)
    store.initialize()
    store.put_capability(
        CapabilityRecord("main", CapabilityName.WRITE_PATH, status=CapabilityStatus.VERIFIED)
    )
    store.put_device_config("main", DeviceLimits(3000, 5000, engineering_mode=True))
    other = DispatchStore(path, "r" * 48)
    with other.connect() as db:
        assert db.execute("SELECT status FROM dispatch_capabilities").fetchone()[0] == "verified"
        assert db.execute("SELECT engineering_mode FROM dispatch_device_config").fetchone()[0] == 1
    with pytest.raises(ValueError, match="cannot be decrypted"):
        other.get_capabilities()


def test_dispatch_store_detects_a_capability_blob_moved_to_another_row(tmp_path: Path) -> None:
    """The payload is bound to device_id/name, so a copied blob cannot release a second device."""
    store = DispatchStore(tmp_path / "dispatch.db", "s" * 48)
    store.initialize()
    store.put_capability(
        CapabilityRecord("main", CapabilityName.WRITE_PATH, status=CapabilityStatus.VERIFIED)
    )
    with store.connect() as db:
        blob = db.execute("SELECT encrypted FROM dispatch_capabilities").fetchone()[0]
        db.execute(
            "INSERT INTO dispatch_capabilities(device_id,name,status,encrypted) VALUES(?,?,?,?)",
            ("slave1", CapabilityName.WRITE_PATH.value, "verified", blob),
        )
    with pytest.raises(ValueError, match="integrity check failed"):
        store.get_capabilities()


class _NoBarrier:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc) -> None:
        return None


class FakeDispatchGateway:
    def restore_barrier(self, device_id: str):
        return _NoBarrier()

    def __init__(self, clock: ManualClock, *, soc_target_policies: SocTargetPolicyRegistry | None = None) -> None:
        self.clock = clock
        self.calls: list[tuple] = []
        self.fail_restore = False
        # The same registry instance the controller writes to, exactly as in production.
        self.soc_target_policies = soc_target_policies or SocTargetPolicyRegistry()

    def outcome(self, name: str, value=0) -> WriteOutcome:
        return WriteOutcome(name, value, value, True, False, self.clock.now())

    async def read_soc(self, device_id: str) -> float:
        self.calls.append(("read_soc", device_id))
        return 50

    async def read_control_telemetry(self, device_id: str) -> ControlTelemetry:
        self.calls.append(("telemetry", device_id))
        return telemetry()

    async def read_snapshot(self, device_id: str) -> DeviceControlSnapshot:
        self.calls.append(("snapshot", device_id))
        return DeviceControlSnapshot(PowerSetpoint(), 0.5, 0, False, self.clock.now())

    async def apply_setpoint(self, device_id: str, setpoint: PowerSetpoint) -> WriteOutcome:
        self.calls.append(("setpoint", setpoint.direction, setpoint.watts))
        return self.outcome("power", setpoint.watts)

    async def apply_soc_target(
        self,
        device_id: str,
        *,
        dispatch_mode: DispatchMode,
        stop_target_percent: float,
        soc_percent: float,
    ) -> WriteOutcome:
        # The fake stands in for the RCT adapter, so it records what the adapter would write: the
        # derived register ratio, never the business stop goal it was handed.
        policy = self.soc_target_policies.policy(device_id)
        ratio = RctSocTargetConvention(policy.mode, policy.below_margin_percent).register_ratio(
            dispatch_mode, stop_target_percent=stop_target_percent, soc_percent=soc_percent
        )
        self.calls.append(("soc_target", ratio))
        return self.outcome("soc", ratio)

    async def apply_control_mode(self, device_id: str, *, external: bool) -> WriteOutcome:
        self.calls.append(("control", external))
        return self.outcome("control", external)

    async def apply_grid_charge(self, device_id: str, *, enabled: bool) -> WriteOutcome:
        self.calls.append(("grid_charge", enabled))
        return self.outcome("grid", enabled)

    async def restore(self, device_id: str, snapshot: DeviceControlSnapshot, step: int) -> WriteOutcome:
        self.calls.append(("restore", step))
        if self.fail_restore:
            raise DeviceApiError("device_unreachable")
        return self.outcome(f"restore-{step}")

    def required_metric_names(self) -> tuple[str, ...]:
        return ()


def verified_registry(*device_ids: str, external_code: int = 1) -> CapabilityRegistry:
    """Every gated capability of ``device_ids`` verified: a test that drives hardware has to say so.

    Only the named devices are released — an unnamed device stays blocked, which is what makes the
    per-device nature of the gate observable in the tests below.
    """
    return CapabilityRegistry(
        CapabilityRecord(
            device_id=device_id,
            name=name,
            status=CapabilityStatus.VERIFIED,
            soc_strategy_external_code=external_code if name is CapabilityName.WRITE_PATH else None,
            enum_byte_width=1,
            bool_byte_width=1,
            write_frame_layout_verified=True,
            apply_sequence_verified=True,
            sequence_order_relevant=True,
            export_limit_zero_blocks_export=True,
            verified_device_model="RCT-Power-Storage-DC",
            verified_firmware="1.0.0",
            verified_by="tester",
        )
        for device_id in device_ids
        for name in CapabilityName
    )


def controller_with_store(
    store: DispatchStore,
    clock: ManualClock,
    gateway: FakeDispatchGateway,
    *,
    capabilities: CapabilityRegistry | None = None,
    limits: dict[str, DeviceLimits] | None = None,
    config: DispatchConfig | None = None,
) -> DispatchController:
    return DispatchController(
        gateway,
        store,
        clock,
        config or DispatchConfig(),
        limits if limits is not None else {"main": DeviceLimits(3000, 5000)},
        capabilities=capabilities if capabilities is not None else verified_registry("main"),
        soc_target_policies=gateway.soc_target_policies,
    )


def controller(tmp_path: Path, clock: ManualClock, gateway: FakeDispatchGateway, **kwargs) -> DispatchController:
    store = DispatchStore(tmp_path / "dispatch.db", "s" * 48)
    store.initialize()
    return controller_with_store(store, clock, gateway, **kwargs)


async def test_a_version_1_row_without_the_new_keys_recovers_through_the_old_restore_sequence(
    tmp_path: Path,
) -> None:
    """AK-22/E-10: the first recover() after the update must restore, not raise on a missing key."""
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    store = version_1_database(tmp_path / "dispatch.db", "s" * 48, _V1_ROW)
    store.initialize()
    dispatch = controller_with_store(store, clock, gateway)
    await dispatch.recover()
    assert [call for call in gateway.calls if call[0] == "restore"] == [
        ("restore", 0),
        ("restore", 1),
        ("restore", 2),
        ("restore", 3),
    ]
    assert (await dispatch.status("main")).state is DispatchState.IDLE


async def test_charge_apply_and_cancel_restores_snapshot(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    status = await dispatch.submit(
        "main",
        DispatchCommand(
            DispatchMode.CHARGE_FROM_GRID,
            80,
            4000,
            clock.now() + timedelta(hours=1),
        ),
    )
    assert status.state is DispatchState.CHARGING
    assert status.max_power_w == 3000
    assert gateway.calls[:6] == [
        ("read_soc", "main"),
        ("snapshot", "main"),
        ("control", True),
        ("soc_target", 0.8),
        ("grid_charge", True),
        ("telemetry", "main"),
    ]
    stopped = await dispatch.cancel("main")
    assert stopped.state is DispatchState.IDLE
    assert stopped.restore_required is False
    assert [call for call in gateway.calls if call[0] == "restore"] == [
        ("restore", 0),
        ("restore", 1),
        ("restore", 2),
        ("restore", 3),
    ]


async def test_failed_restore_never_becomes_idle(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit(
        "main",
        DispatchCommand(
            DispatchMode.DISCHARGE_TO_LOAD,
            20,
            2000,
            clock.now() + timedelta(hours=1),
        ),
    )
    gateway.fail_restore = True
    status = await dispatch.cancel("main")
    assert status.state is DispatchState.FAULT_RESTORE_PENDING
    assert status.restore_required is True


async def test_replace_keeps_snapshot_and_clears_grid_charge_for_discharge(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    first = await dispatch.submit(
        "main",
        DispatchCommand(
            DispatchMode.CHARGE_FROM_GRID,
            80,
            2000,
            clock.now() + timedelta(hours=1),
        ),
    )
    gateway.calls.clear()
    replaced = await dispatch.submit(
        "main",
        DispatchCommand(
            DispatchMode.DISCHARGE_TO_LOAD,
            20,
            2000,
            clock.now() + timedelta(hours=2),
            expected_operation_id=first.operation_id,
        ),
    )
    assert replaced.replaced is True
    assert replaced.operation_id != first.operation_id
    assert ("snapshot", "main") not in gateway.calls
    assert ("grid_charge", False) in gateway.calls


async def test_already_reached_replace_restores_running_operation(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    first = await dispatch.submit(
        "main",
        DispatchCommand(
            DispatchMode.DISCHARGE_TO_LOAD,
            20,
            2000,
            clock.now() + timedelta(hours=1),
        ),
    )

    async def reached_soc(device_id: str) -> float:
        return 90

    gateway.read_soc = reached_soc  # type: ignore[method-assign]
    stopped = await dispatch.submit(
        "main",
        DispatchCommand(
            DispatchMode.CHARGE_FROM_GRID,
            80,
            2000,
            clock.now() + timedelta(hours=2),
            expected_operation_id=first.operation_id,
        ),
    )
    assert stopped.state is DispatchState.IDLE
    assert stopped.stop_reason is not None and stopped.stop_reason.value == "target_reached"
    assert any(call[0] == "restore" for call in gateway.calls)


async def test_expired_recovery_restores_instead_of_reapplying(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit(
        "main",
        DispatchCommand(
            DispatchMode.DISCHARGE_TO_LOAD,
            20,
            2000,
            clock.now() + timedelta(minutes=1),
        ),
    )
    gateway.calls.clear()
    clock.advance(120)
    await dispatch.recover()
    assert not any(call[0] == "control" for call in gateway.calls)
    assert any(call[0] == "restore" for call in gateway.calls)


async def test_shutdown_restores_before_transport_teardown(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit(
        "main",
        DispatchCommand(
            DispatchMode.CHARGE_FROM_GRID,
            80,
            2000,
            clock.now() + timedelta(hours=1),
        ),
    )
    gateway.calls.clear()
    await dispatch.shutdown_restore()
    assert (await dispatch.status("main")).state is DispatchState.IDLE
    assert any(call[0] == "restore" for call in gateway.calls)


@pytest.mark.parametrize("watts", [-1, -0.01])
def test_power_setpoint_rejects_negative_magnitude(watts: float) -> None:
    with pytest.raises(ValueError):
        PowerSetpoint(PowerDirection.CHARGE, watts)


async def test_force_restore_or_raise_is_a_noop_when_nothing_is_active(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.force_restore_or_raise("main")  # no intent, no restore_required: must not raise
    assert gateway.calls == []


async def test_force_restore_or_raise_restores_an_active_dispatch(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit(
        "main",
        DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1)),
    )
    await dispatch.force_restore_or_raise("main")
    status = await dispatch.status("main")
    assert status.state is DispatchState.IDLE
    assert status.restore_required is False
    assert status.stop_reason is StopReason.DEVICE_RECONFIGURED


async def test_force_restore_or_raise_raises_and_leaves_the_record_in_fault_when_restore_fails(
    tmp_path: Path,
) -> None:
    """This is the P0 safety invariant: a reconfiguration must abort, not swap the graph, when the
    affected device cannot be cleanly restored first."""
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit(
        "main",
        DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1)),
    )
    gateway.fail_restore = True
    with pytest.raises(ReconfigurationRejected) as excinfo:
        await dispatch.force_restore_or_raise("main")
    assert excinfo.value.device_id == "main"
    status = await dispatch.status("main")
    assert status.state is DispatchState.FAULT_RESTORE_PENDING
    assert status.restore_required is True  # the old dispatch state must stay intact, not be dropped


def test_set_limits_replaces_the_live_dict(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    new_limits = {"extra": DeviceLimits(1000, 1000)}
    dispatch.set_limits(new_limits)
    assert dispatch._limits is new_limits


# --- A2: FAULT_RESTORE_PENDING must retry automatically, not stay stuck forever -----------------


async def test_fault_restore_pending_retries_automatically_once_eligible(tmp_path: Path) -> None:
    """Before the fix: a device that fails one restore attempt stays in FAULT_RESTORE_PENDING
    forever, until an operator explicitly calls cancel()/submit() again. tick() must now retry it
    on its own once ``next_restore_at`` has passed.
    """
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit(
        "main",
        DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1)),
    )
    gateway.fail_restore = True
    stuck = await dispatch.cancel("main")
    assert stuck.state is DispatchState.FAULT_RESTORE_PENDING

    gateway.fail_restore = False
    clock.advance(1)  # next_restore_at was set to the failure time; any advance clears it
    status = await dispatch.tick("main")
    assert status.state is DispatchState.IDLE
    assert status.restore_required is False


async def test_status_never_triggers_a_restore_retry_as_a_side_effect(tmp_path: Path) -> None:
    """status() must stay a pure read; only tick() may act on an eligible retry."""
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit(
        "main",
        DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1)),
    )
    gateway.fail_restore = True
    await dispatch.cancel("main")
    gateway.fail_restore = False
    gateway.calls.clear()
    status = await dispatch.status("main")
    assert status.state is DispatchState.FAULT_RESTORE_PENDING
    assert gateway.calls == []


# --- A1: defensive PRECHECK-with-snapshot handling in recover() (currently unreachable) ---------


async def test_recover_restores_a_defensively_constructed_precheck_with_snapshot_record(
    tmp_path: Path,
) -> None:
    """submit()'s current write order never leaves PRECHECK with a snapshot already set, so this
    exercises recover()'s defensive branch directly rather than a reachable crash window.
    """
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    store = DispatchStore(tmp_path / "dispatch.db", "s" * 48)
    store.initialize()
    record = DispatchRecord(
        "main",
        state=DispatchState.PRECHECK,
        intent=DispatchIntent(
            operation_id="op-1",
            mode=DispatchMode.CHARGE_FROM_GRID,
            target_soc_percent=80.0,
            max_power_w=2000.0,
            max_power_w_requested=2000.0,
            valid_until=clock.now() + timedelta(hours=1),
            valid_until_requested=clock.now() + timedelta(hours=1),
            created_at=clock.now(),
        ),
        snapshot=DeviceControlSnapshot(PowerSetpoint(), 0.5, 1, False, clock.now(), all_fresh=True),
        restore_required=True,
    )
    store.put(record)
    dispatch = controller_with_store(store, clock, gateway)
    await dispatch.recover()
    assert any(call[0] == "restore" for call in gateway.calls)
    assert (await dispatch.status("main")).state is DispatchState.IDLE


# --- D5: submit() must reject a new dispatch on a non-fresh snapshot, not just the model default -

async def test_submit_rejects_a_new_dispatch_when_the_fresh_snapshot_read_is_stale(
    tmp_path: Path,
) -> None:
    """A snapshot read with all_fresh=False must not become the basis of a new dispatch: the
    half-written intent is cleaned up via _restore() and the caller sees dispatch_snapshot_stale,
    with the record ending back in IDLE and no setpoint applied.
    """
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)

    async def stale_snapshot(device_id: str) -> DeviceControlSnapshot:
        gateway.calls.append(("snapshot", device_id))
        return DeviceControlSnapshot(PowerSetpoint(), 0.5, 0, False, clock.now(), all_fresh=False)

    gateway.read_snapshot = stale_snapshot  # type: ignore[method-assign]

    with pytest.raises(DispatchRejected, match="dispatch_snapshot_stale"):
        await dispatch.submit(
            "main",
            DispatchCommand(
                DispatchMode.CHARGE_FROM_GRID,
                80,
                2000,
                clock.now() + timedelta(hours=1),
            ),
        )

    # _apply() never ran: no control/soc_target/grid_charge writes from the intended operation.
    assert not any(call[0] in ("control", "soc_target", "grid_charge") for call in gateway.calls)
    # The only setpoint write permitted is _restore()'s own safety stop-to-zero, never a hazard
    # setpoint derived from the (never computed) intent.
    assert all(
        call[1:] == (PowerDirection.NONE, 0.0) for call in gateway.calls if call[0] == "setpoint"
    )
    status = await dispatch.status("main")
    assert status.state is DispatchState.IDLE
    assert status.restore_required is False


async def test_submit_restores_after_a_non_device_error_during_apply(tmp_path: Path) -> None:
    """Any failure after the first write (not only DeviceApiError) must roll the hardware back."""
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)

    async def broken_grid_charge(device_id: str, *, enabled: bool):
        raise ValueError("boom")

    gateway.apply_grid_charge = broken_grid_charge  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="boom"):
        await dispatch.submit(
            "main",
            DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1)),
        )
    assert [call for call in gateway.calls if call[0] == "restore"] == [("restore", step) for step in range(4)]
    assert (await dispatch.status("main")).state is DispatchState.IDLE


# --- recover()/shutdown_restore() must survive stale reads and unreadable rows ------------------


async def test_recover_rereads_the_record_under_the_lock_after_a_concurrent_tick(tmp_path: Path) -> None:
    """A tick landing between store.all() and the device lock must not turn into a stale-CAS abort."""
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit(
        "main",
        DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1)),
    )
    real_all = dispatch._store.all

    def all_then_concurrent_write():
        records = real_all()
        newer = dispatch._store.get("main")
        assert newer is not None
        dispatch._store.put(newer)  # bumps record_version behind the stale copy
        return records

    dispatch._store.all = all_then_concurrent_write
    await dispatch.recover()
    assert (await dispatch.status("main")).state is DispatchState.IDLE


async def test_recover_continues_with_the_next_device_when_one_restore_raises(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(
        tmp_path, clock, gateway, limits={d: DeviceLimits(3000, 5000) for d in ("a", "b")},
        capabilities=verified_registry("a", "b"),
    )
    for device_id in ("a", "b"):
        await dispatch.submit(
            device_id,
            DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1)),
        )
    real_get = dispatch._get

    async def get_failing_for_a(device_id: str):
        if device_id == "a":
            raise RuntimeError("boom")
        return await real_get(device_id)

    dispatch._get = get_failing_for_a
    await dispatch.recover()
    dispatch._get = real_get
    assert (await dispatch.status("b")).state is DispatchState.IDLE


def test_dispatch_store_all_skips_a_row_that_cannot_be_decrypted(tmp_path: Path) -> None:
    store = DispatchStore(tmp_path / "dispatch.db", "s" * 48)
    store.initialize()
    store.put(DispatchRecord("good", state=DispatchState.PRECHECK))
    store.put(DispatchRecord("bad", state=DispatchState.PRECHECK))
    with store.connect() as db:
        db.execute("UPDATE dispatch_operations SET encrypted=? WHERE device_id='bad'", (b"not-a-fernet-token",))
    assert [record.device_id for record in store.all()] == ["good"]


async def test_failed_restore_backs_off_exponentially_with_a_cap(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit(
        "main",
        DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1)),
    )
    gateway.fail_restore = True
    await dispatch.cancel("main")
    delays = []
    for _ in range(12):
        record = await dispatch._get("main")
        assert record.next_restore_at is not None
        delays.append((record.next_restore_at - clock.now()).total_seconds())
        clock.advance(delays[-1])
        await dispatch.tick("main")
    assert delays[:4] == [1, 2, 4, 8]
    assert max(delays) == 30
    record = await dispatch._get("main")
    assert record.restore_attempts == 13


async def test_restore_retry_info_exposes_attempts_and_next_retry(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit(
        "main",
        DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1)),
    )
    assert await dispatch.restore_retry_info("main") == (0, None)
    gateway.fail_restore = True
    await dispatch.cancel("main")
    attempts, next_at = await dispatch.restore_retry_info("main")
    assert attempts == 1 and next_at == clock.now() + timedelta(seconds=1)


def test_a_negative_stored_restore_attempts_counter_is_clamped() -> None:
    record = DispatchRecord("main")
    data = record.to_dict()
    data["restore_attempts"] = -5
    assert DispatchRecord.from_dict(data).restore_attempts == 0
    del data["restore_attempts"]  # records written before the counter existed
    assert DispatchRecord.from_dict(data).restore_attempts == 0


async def test_recover_reports_an_unreadable_record_as_critical(tmp_path: Path, caplog) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    dispatch._store.put(DispatchRecord("ghost", state=DispatchState.PRECHECK))
    with dispatch._store.connect() as db:
        db.execute("UPDATE dispatch_operations SET encrypted=? WHERE device_id='ghost'", (b"garbage",))
    with caplog.at_level("CRITICAL"):
        await dispatch.recover()
    assert dispatch.unreadable_devices == ("ghost",)
    assert any(r.levelname == "CRITICAL" and "ghost" in r.getMessage() for r in caplog.records)


def test_export_cut_bypasses_deadband_and_interval_for_a_reduction() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    config = DispatchConfig()
    current = PowerSetpoint(PowerDirection.DISCHARGE, 1000)
    desired = PowerSetpoint(PowerDirection.DISCHARGE, 850)
    kwargs = {"now": now, "last_write_at": now, "config": config}
    assert not should_write(desired, current, **kwargs)
    assert should_write(desired, current, export_cut=True, **kwargs)
    # Only a reduction is urgent; an increase still honours the deadband.
    assert not should_write(PowerSetpoint(PowerDirection.DISCHARGE, 1150), current, export_cut=True, **kwargs)


async def test_a_successful_restore_clears_the_fault_code(tmp_path: Path) -> None:
    clock = ManualClock()
    gateway = FakeDispatchGateway(clock)
    dispatch = controller(tmp_path, clock, gateway)
    await dispatch.submit(
        "main",
        DispatchCommand(DispatchMode.CHARGE_FROM_GRID, 80, 2000, clock.now() + timedelta(hours=1)),
    )
    gateway.fail_restore = True
    stuck = await dispatch.cancel("main")
    assert stuck.fault_code == "device_unreachable"
    gateway.fail_restore = False
    clock.advance(1)
    status = await dispatch.tick("main")
    assert status.state is DispatchState.IDLE
    assert status.fault_code is None
