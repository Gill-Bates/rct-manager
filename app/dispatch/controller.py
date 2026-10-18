#!/usr/bin/env python3
#
# app/dispatch/controller.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Battery dispatch state machine and recovery loop (REQ-100..REQ-125)."""

import asyncio
import contextlib
import logging
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Iterable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TypeVar

from app.clock import Clock
from app.dispatch.base import BatteryDispatchGateway
from app.dispatch.capabilities import (
    CapabilityName,
    CapabilityRecord,
    CapabilityRegistry,
    evaluate_gate,
    required_for,
)
from app.dispatch.gating import should_write
from app.dispatch.live_limit import LiveChargeLimitProvider, NullLiveChargeLimitProvider
from app.dispatch.models import (
    ControlTelemetry,
    DeviceLimits,
    DispatchCommand,
    DispatchConfig,
    DispatchIntent,
    DispatchMode,
    DispatchRecord,
    DispatchRecordCorrupt,
    DispatchState,
    DispatchStatus,
    PowerDirection,
    PowerSetpoint,
    StopReason,
    phase_for,
)
from app.dispatch.soc_policy import SocTargetPolicy, SocTargetPolicyRegistry
from app.dispatch.store import DispatchStore
from app.dispatch.strategy import calculate_setpoint, effective_charge_limit_w, target_reached
from app.errors import DeviceApiError

log = logging.getLogger(__name__)

_T = TypeVar("_T")

# Automatic restore retries back off exponentially so a persistently failing device is not hammered.
_RESTORE_BACKOFF_BASE_SECONDS = 1.0
_RESTORE_BACKOFF_MAX_SECONDS = 30.0  # the battery stays externally controlled meanwhile

# The state a successfully applied command leaves the device in. A table rather than a conditional
# so a new mode without a state cannot silently inherit another mode's.
_STATE_FOR_MODE = {
    DispatchMode.CHARGE_FROM_GRID: DispatchState.CHARGING,
    DispatchMode.DISCHARGE_TO_LOAD: DispatchState.DISCHARGING,
    DispatchMode.HOLD: DispatchState.HOLDING,
}


class DispatchRejected(DeviceApiError):
    pass


class _TelemetryStale(Exception):
    """Control telemetry read for the first setpoint write was older than the staleness policy.

    Internal to the controller: ``submit()`` maps it to a ``TELEMETRY_STALE`` restore and a
    ``dispatch_snapshot_stale`` rejection, exactly like the snapshot-stale gate (D5). It must never
    propagate as a generic device error, so a stale reading can never drive the first write.
    """


def _guarded_names(mode: DispatchMode) -> frozenset[CapabilityName]:
    # limit_export=True is a superset of limit_export=False (it only adds EXPORT_LIMIT).
    return required_for(mode, limit_export=True)


class CapabilityConflict(DeviceApiError):
    """A capability/limit write was refused because an active operation of the same device would
    be affected by it (D2 — immutable-during-active-operation). Carries the blocking operation's
    id and mode so the admin layer can report what the caller must wait for or cancel.
    """

    code = "dispatch_capability_conflict"

    def __init__(self, device_id: str, operation_id: str, mode: DispatchMode) -> None:
        super().__init__(device_id=device_id, operation_id=operation_id, mode=mode.value)
        self.device_id = device_id
        self.operation_id = operation_id
        self.mode = mode


class ReconfigurationRejected(Exception):
    """A device-list change was aborted because an affected device could not be cleanly restored.

    Raised by ``force_restore_or_raise`` before any teardown of the old transport/graph has
    started, so the caller (``reconfigure_devices`` in ``app_factory.py``) can leave the old graph
    and dispatch state completely untouched and surface a clear error instead of swapping the graph
    out from under a dispatch that is stuck mid-restore.
    """

    def __init__(self, device_id: str, fault_code: str | None) -> None:
        self.device_id = device_id
        self.fault_code = fault_code
        super().__init__(f"dispatch for device {device_id!r} could not be restored: {fault_code}")


class DispatchController:
    def __init__(
        self,
        gateway: BatteryDispatchGateway,
        store: DispatchStore,
        clock: Clock,
        config: DispatchConfig,
        limits: dict[str, DeviceLimits],
        *,
        capabilities: CapabilityRegistry,
        soc_target_policies: SocTargetPolicyRegistry,
        live_charge_limit: LiveChargeLimitProvider | None = None,
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._clock = clock
        self._config = config
        self._limits = limits
        # The Live Charge Envelope seam. Defaults to the inert Null provider: the contract, clamp
        # chain, freshness rule and immediate-derate write are all live, but no live ceiling is
        # emitted until a producer whose hardware register semantics are verified replaces it. A
        # missing/stale/unverified limit is never invented — dispatch falls back to the configured
        # static DeviceLimits.max_charge_power_w (the RCT/BMS keeps its own internal protection).
        self._live_charge_limit = live_charge_limit or NullLiveChargeLimitProvider()
        # The very same registry instance the adapter reads from: a second instance would be a
        # second truth, and a capability update has to take effect live, without a restart.
        self._capabilities = capabilities
        # Same single-instance rule as the capabilities: the adapter reads the policy this
        # controller writes, so an operator change takes effect on the next apply without a restart.
        self._soc_target_policies = soc_target_policies
        self._locks: dict[str, asyncio.Lock] = {}
        self.unreadable_devices: tuple[str, ...] = ()
        # Live set of devices whose restore is outstanding (ended in FAULT_RESTORE_PENDING). Kept
        # current by _restore_guarded on every success/failure, so an automatic tick() retry that
        # heals a device clears it here too; readiness derives from this instead of a latched
        # startup snapshot (so a post-startup self-heal is not stuck red until restart).
        self._restore_pending: set[str] = set()
        # The startup recovery sweep has to run before readiness may turn green (C2): until then a
        # possibly half-written hardware state has not been reconciled, even though no device sits
        # in _restore_pending yet.
        self._recovery_complete = False

    def _lock(self, device_id: str) -> asyncio.Lock:
        return self._locks.setdefault(device_id, asyncio.Lock())

    async def _shield_hardware_critical(self, coro: Awaitable[_T]) -> _T:
        """Run a hardware apply/restore section so a cancelled caller cannot interrupt it mid-write.

        Same idiom ``run()`` uses to protect ``tick()`` (C1): the work runs as a shielded task, and
        if the awaiting caller is cancelled the task is still awaited to completion before the
        ``CancelledError`` propagates, so the record never stays in APPLYING/RESTORING with the
        hardware left partially externally controlled. The ``except Exception`` restore paths do not
        catch ``CancelledError`` under Python 3.13, which is why the shield is needed here and not
        only in ``run()``. The caller holds the per-device lock across this call; the task does not
        re-acquire it.
        """
        task = asyncio.ensure_future(coro)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The caller was cancelled: let the shielded apply/restore finish first, then re-raise.
            # The task's own result/exception is irrelevant once the caller is being cancelled.
            with contextlib.suppress(Exception):
                await task
            raise

    async def _get(self, device_id: str) -> DispatchRecord:
        try:
            record = await asyncio.to_thread(self._store.get, device_id)
        except DispatchRecordCorrupt as exc:
            # Never synthesize a clean IDLE record here: that would reintroduce the fail-open
            # hazard (C1) one layer up, just for a device whose persisted state is unreadable.
            raise DeviceApiError("dispatch_record_corrupt", device_id=device_id, reason=str(exc)) from exc
        return record or DispatchRecord(device_id)

    async def _put(self, record: DispatchRecord) -> None:
        await asyncio.to_thread(self._store.put, record)

    def _status(self, record: DispatchRecord, *, replaced: bool = False) -> DispatchStatus:
        intent = record.intent
        if record.state is DispatchState.IDLE:
            control_state = "restored"
        elif record.state is DispatchState.FAULT_RESTORE_PENDING:
            control_state = "unknown"
        else:
            control_state = "controlled"
        return DispatchStatus(
            device_id=record.device_id,
            operation_id=intent.operation_id if intent else None,
            mode=intent.mode if intent else None,
            state=record.state,
            phase=phase_for(record.state),
            control_state=control_state,
            restore_required=record.restore_required,
            target_soc_percent=intent.target_soc_percent if intent else None,
            max_power_w=intent.max_power_w if intent else 0.0,
            max_power_w_requested=intent.max_power_w_requested if intent else 0.0,
            valid_until=intent.valid_until if intent else None,
            commanded_direction=record.last_commanded.direction,
            commanded_power_w=record.last_commanded.watts,
            stop_reason=record.stop_reason,
            fault_code=record.fault_code,
            replaced=replaced,
        )

    async def status(self, device_id: str) -> DispatchStatus:
        return self._status(await self._get(device_id))

    def _owns_device(self, record: DispatchRecord) -> bool:
        """Whether dispatch currently owns the control registers of ``record``'s device.

        Anything other than a clean IDLE — an active operation, a pending/required restore, or a
        lingering intent — means the dispatch layer controls these registers and a parallel raw
        write must be refused.
        """
        return (
            record.state is not DispatchState.IDLE
            or record.restore_required
            or record.intent is not None
        )

    @contextlib.asynccontextmanager
    async def guard_raw_write(self, device_id: str) -> AsyncIterator[None]:
        """Hold the per-device dispatch lock across a raw caller write and refuse it if dispatch owns
        the device, re-checked UNDER the lock.

        Closes the TOCTOU window in the generic write path: the ownership check and the write must be
        atomic against ``submit()``/``tick()``, which take the same lock. Without this the check and
        the write straddle a window in which a dispatch could start and interleave its control-mode/
        setpoint writes with the raw write on the same registers. The caller performs the device
        write inside the ``with`` block, while this lock keeps dispatch from starting meanwhile.
        """
        async with self._lock(device_id):
            if self._owns_device(await self._get(device_id)):
                raise DispatchRejected("dispatch_register_locked", device_id=device_id)
            yield

    async def submit_verification_probe(self, device_id: str, command: DispatchCommand) -> DispatchStatus:
        """Run one short HOLD on unverified hardware for the guided verification.

        Only a HOLD (0 W, no SoC goal) qualifies: it moves no energy on purpose, and it goes through
        the same snapshot, durable intent, TTL cap and restore path as any dispatch. The gate is
        passed as if engineering mode were on, without persisting that switch. The probe runs only
        on a genuinely idle device: ``submit`` re-asserts IDLE under the device lock and raises
        ``dispatch_operation_conflict`` if an operation started after the caller's preflight check.
        """
        if command.mode is not DispatchMode.HOLD:
            raise DispatchRejected("invalid_request")
        return await self.submit(device_id, command, _verification_probe=True)

    async def submit(
        self, device_id: str, command: DispatchCommand, *, _verification_probe: bool = False
    ) -> DispatchStatus:
        async with self._lock(device_id):
            if command.mode is DispatchMode.EXPORT_TO_GRID:
                raise DispatchRejected("dispatch_mode_unavailable")
            now = self._clock.now()
            if command.valid_until.tzinfo is None or command.valid_until <= now:
                raise DispatchRejected("value_out_of_range")
            if command.mode is DispatchMode.HOLD:
                # A hold has no SoC goal and no power budget; anything else is a programming error
                # upstream, not an operator value out of range.
                if command.target_soc_percent is not None or command.max_power_w != 0.0:
                    raise DispatchRejected("invalid_request")
            else:
                if command.target_soc_percent is None:
                    raise DispatchRejected("invalid_request")
                if not math.isfinite(command.target_soc_percent) or not math.isfinite(command.max_power_w):
                    raise DispatchRejected("value_not_finite")
                if not self._config.min_soc <= command.target_soc_percent <= self._config.max_soc:
                    raise DispatchRejected("value_out_of_range")
                if command.max_power_w <= 0:
                    raise DispatchRejected("value_out_of_range")
            limits = self._limits.get(device_id)
            if limits is None:
                raise DispatchRejected("dispatch_limits_missing")
            # Capability gate before the first device access: a blocked dispatch must not even read
            # the device, and must never reach a write. Decided once here and carried on.
            gate = evaluate_gate(
                command.mode,
                self._capabilities,
                device_id=device_id,
                device_engineering_mode=limits.engineering_mode or _verification_probe,
                limit_export=self._config.limit_export_during_discharge,
            )
            if not gate.allowed:
                # Context is for the log, not the response: the published problem keeps its generic
                # text, so a refusal never describes the device's internals outward.
                raise DispatchRejected(
                    "dispatch_unverified",
                    unverified=[name.value for name in gate.unverified],
                    reason=gate.reject_detail,
                )
            current = await self._get(device_id)
            if _verification_probe and (
                current.state is not DispatchState.IDLE
                or current.intent is not None
                or current.restore_required
            ):
                # A verification probe may only run on a genuinely idle device. The caller's
                # preflight check holds no lock, so a real charge/discharge can start between that
                # check and here; under this lock we re-assert idle-only and refuse rather than let
                # the probe fall into the normal replacement path and overwrite a live operation.
                raise DispatchRejected("dispatch_operation_conflict")
            if current.state in (
                DispatchState.APPLYING,
                DispatchState.REPLACING,
                DispatchState.PRECHECK,
            ):
                # An interrupted apply/replace left hardware possibly half-written; tick()/recover()
                # treat exactly these as "restore first". submit() must not accept a new order on top
                # of that half-written state while recovery has not run yet (its correctness must not
                # depend on the startup sweep winning the CPU race). Drive the restore under this
                # lock, then reject so the caller retries against a clean record.
                await self._restore(current, StopReason.DEVICE_ERROR, "recovery_required")
                raise DispatchRejected("dispatch_restore_required")
            if current.state in (
                DispatchState.RESTORING,
                DispatchState.FAULT,
                DispatchState.FAULT_RESTORE_PENDING,
            ):
                raise DispatchRejected("dispatch_restore_required")
            current_id = current.intent.operation_id if current.intent else None
            if command.expected_operation_id is not None and command.expected_operation_id != current_id:
                raise DispatchRejected("dispatch_operation_conflict")
            if current.intent and (
                current.intent.mode is command.mode
                and current.intent.target_soc_percent == command.target_soc_percent
                and current.intent.max_power_w_requested == command.max_power_w
                and current.intent.valid_until_requested == command.valid_until
            ):
                return self._status(current)
            soc = await self._gateway.read_soc(device_id)
            if not math.isfinite(soc) or not 0.0 <= soc <= 100.0:
                # The controller is the last validation boundary for device data (H4): a NaN or
                # out-of-range SoC must never reach target_reached() or apply_soc_target().
                raise DeviceApiError("protocol_error", device_id=device_id)
            if target_reached(command.mode, soc, command.target_soc_percent):
                if current.intent is not None or current.restore_required:
                    await self._restore(current, StopReason.TARGET_REACHED)
                    return self._status(current)
                current.state = DispatchState.IDLE
                current.stop_reason = StopReason.TARGET_REACHED
                await self._put(current)
                return self._status(current)
            if command.mode is DispatchMode.HOLD:
                # DeviceLimits is still required above (the gate and the TTL cap need it); only the
                # power lookup is skipped, because a hold commands no power at all.
                effective_power = 0.0
            else:
                maximum = (
                    limits.max_charge_power_w
                    if command.mode is DispatchMode.CHARGE_FROM_GRID
                    else limits.max_discharge_power_w
                )
                effective_power = min(command.max_power_w, maximum)
            # Engineering mode drives unverified hardware, so an unattended setpoint must expire
            # sooner than in normal operation.
            ttl_cap = (
                self._config.max_operation_duration_engineering_seconds
                if gate.engineering_mode
                else self._config.max_operation_duration_seconds
            )
            effective_until = min(command.valid_until, now + timedelta(seconds=ttl_cap))
            replacing = current.intent is not None and current.state is not DispatchState.IDLE
            intent = DispatchIntent(
                operation_id=uuid.uuid4().hex,
                mode=command.mode,
                target_soc_percent=command.target_soc_percent,
                max_power_w=effective_power,
                max_power_w_requested=command.max_power_w,
                valid_until=effective_until,
                valid_until_requested=command.valid_until,
                created_at=now,
            )
            record = DispatchRecord(
                device_id=device_id,
                state=DispatchState.REPLACING if replacing else DispatchState.PRECHECK,
                intent=intent,
                snapshot=current.snapshot if replacing else None,
                record_version=current.record_version,
            )
            await self._put(record)  # durable intent before the first hardware access
            if record.snapshot is None:
                snapshot = await self._gateway.read_snapshot(device_id)
                if not snapshot.all_fresh:
                    # D5: a snapshot that is not fully fresh must not become the basis of a new
                    # dispatch (it may later be restored from). The half-written intent above is
                    # cleaned up the same way a device error during apply is: via _restore().
                    # The stale snapshot stays local: nothing was written, so there is nothing
                    # to restore and _restore() takes its no-snapshot reset-to-IDLE path.
                    await self._restore(record, StopReason.SNAPSHOT_STALE)
                    raise DispatchRejected("dispatch_snapshot_stale")
                record.snapshot = snapshot
            record.state = DispatchState.REPLACING if replacing else DispatchState.APPLYING
            record.restore_required = True
            steps = ["control_mode"]
            if command.mode is not DispatchMode.HOLD:
                # A hold has no SoC goal, so there is no SoC target to write. _apply() executes
                # exactly this durable plan by step name, so plan and execution cannot drift.
                steps.append("soc_target")
            steps += ["grid_charge", "setpoint"]
            record.plan = [{"name": name, "status": "planned"} for name in steps]
            await self._put(record)  # snapshot and complete plan before writes
            preflight_passed = False
            try:
                telemetry = await self._gateway.read_control_telemetry(device_id)
                if self._telemetry_stale(telemetry):
                    raise _TelemetryStale
                preflight_passed = True
                # The SoC submit() already read is handed down: _apply() adds no device read, and
                # the derivation of the device-level SoC target needs the measured value. Shielded
                # so a cancelled caller cannot interrupt the multi-step write sequence mid-apply and
                # leave the record in APPLYING with the hardware half-written (P2-1).
                await self._shield_hardware_critical(self._apply(record, soc_percent=soc))
            except _TelemetryStale:
                # A rejected first preflight made no hardware change; an apply-phase rejection
                # still needs the saved snapshot restored.
                if not preflight_passed and not replacing:
                    record.snapshot = None
                await self._restore(record, StopReason.TELEMETRY_STALE)
                raise DispatchRejected("dispatch_snapshot_stale") from None
            except Exception as exc:
                try:
                    if not preflight_passed and not replacing:
                        record.snapshot = None
                    await self._restore(record, StopReason.DEVICE_ERROR, getattr(exc, "code", "internal_error"))
                except Exception:
                    # The record stays RESTORING/APPLYING, so tick()/recover() finish the restore;
                    # the apply error below is the one the caller needs to see.
                    log.exception("Restore after failed apply of device %s raised", device_id)
                raise
            record.state = _STATE_FOR_MODE[command.mode]
            record.restore_required = False
            await self._put(record)
            return self._status(record, replaced=replacing)

    def _active_mode_blocking(self, record: DispatchRecord) -> DispatchMode | None:
        """The mode of the record's active operation, or ``None`` if the device is idle/faulted.

        Mirrors the admin API's former ``_active_operation_mode`` but works on an already-fetched
        ``DispatchRecord`` under the per-device lock, instead of re-fetching status outside it
        (closes the TOCTOU window, D2).
        """
        if record.state in (
            DispatchState.IDLE,
            DispatchState.FAULT,
            DispatchState.FAULT_RESTORE_PENDING,
        ):
            return None
        return record.intent.mode if record.intent is not None else None

    async def _raise_if_active(self, device_id: str, names: Iterable[CapabilityName] | None = None) -> None:
        """Refuse a gate change while an operation is active OR a restore is outstanding; the caller
        holds the lock.

        ``names`` limits the refusal to capabilities the active mode depends on; ``None`` refuses
        any change. An outstanding restore (``FAULT_RESTORE_PENDING``) refuses unconditionally,
        regardless of ``names``: the pending restore reapplies the WHOLE captured snapshot, so any
        capability, limit or policy change made meanwhile could make that restore inconsistent (D2).
        """
        current = await self._get(device_id)
        if current.restore_required and current.intent is not None:
            # A restore is still outstanding (e.g. FAULT_RESTORE_PENDING awaiting its backoff
            # retry). Block every change until the device is cleanly restored, so the snapshot the
            # restore replays is not changed under it.
            raise CapabilityConflict(device_id, current.intent.operation_id, current.intent.mode)
        blocking_mode = self._active_mode_blocking(current)
        if blocking_mode is None:
            return
        if names is not None:
            guarded = _guarded_names(blocking_mode)
            if not any(name in guarded for name in names):
                return
        assert current.intent is not None
        raise CapabilityConflict(device_id, current.intent.operation_id, blocking_mode)

    async def _step(self, record: DispatchRecord, index: int, write) -> None:
        record.plan[index]["status"] = "sent"
        await self._put(record)
        await write()
        record.plan[index]["status"] = "confirmed"
        await self._put(record)

    def _writer(
        self,
        record: DispatchRecord,
        name: str,
        *,
        desired: PowerSetpoint,
        grid_charge_enabled: bool,
        soc_percent: float,
    ):
        """The write one named plan step performs. An unknown name is a ``KeyError``: that is a
        programming error, not operator input, and loud is correct.
        """
        intent = record.intent
        assert intent is not None
        if name == "control_mode":
            return lambda: self._gateway.apply_control_mode(record.device_id, external=True)
        if name == "soc_target":
            # The business stop goal, the mode and the measured SoC go to the adapter; which raw
            # value the device's SoC-target register wants is the adapter's decision alone.
            return lambda: self._gateway.apply_soc_target(
                record.device_id,
                dispatch_mode=intent.mode,
                stop_target_percent=intent.target_soc_percent,
                soc_percent=soc_percent,
            )
        if name == "grid_charge":
            return lambda: self._gateway.apply_grid_charge(record.device_id, enabled=grid_charge_enabled)
        if name == "setpoint":
            return lambda: self._gateway.apply_setpoint(record.device_id, desired)
        raise KeyError(name)

    async def _apply(self, record: DispatchRecord, *, soc_percent: float) -> None:
        assert record.intent is not None and record.snapshot is not None
        if record.intent.mode is DispatchMode.CHARGE_FROM_GRID:
            grid_charge_enabled = True
        elif record.intent.mode is DispatchMode.HOLD:
            grid_charge_enabled = False  # holding must not let the grid charge the battery
        else:
            grid_charge_enabled = record.snapshot.grid_charge_enabled
        desired = PowerSetpoint()
        # Dispatch by step name over the durable plan: an omitted step must not shift the index of
        # every later step against record.plan's own bookkeeping.
        for index, step in enumerate(record.plan):
            if step["name"] == "setpoint":
                # The telemetry read stays immediately before the setpoint write.
                telemetry = await self._gateway.read_control_telemetry(record.device_id)
                # Gate the first setpoint on the same freshness policy tick() enforces: a stale or
                # missing reading must not drive the first write any more than a later one.
                if self._telemetry_stale(telemetry):
                    raise _TelemetryStale
                live_limit_w = self._live_charge_limit_w(
                    record.device_id, record.intent.mode, self._clock.now()
                )
                # Transient execution ceiling: the durable intent.max_power_w is never lowered here,
                # so a later increase of the live limit can rise back to it (AC-9).
                execution_ceiling = effective_charge_limit_w(record.intent.max_power_w, live_limit_w)
                desired = calculate_setpoint(
                    record.intent.mode,
                    telemetry,
                    target_soc_percent=record.intent.target_soc_percent,
                    max_power_w=execution_ceiling,
                    config=self._config,
                    last=PowerSetpoint(),
                )
            await self._step(
                record,
                index,
                self._writer(
                    record,
                    step["name"],
                    desired=desired,
                    grid_charge_enabled=grid_charge_enabled,
                    soc_percent=soc_percent,
                ),
            )
        record.last_commanded = desired
        record.last_write_at = self._clock.now()

    def _telemetry_stale(self, telemetry: ControlTelemetry) -> bool:
        """Whether a control-telemetry reading is older than the staleness policy allows.

        One policy for every write: the per-tick gate and the first setpoint write in _apply() both
        refuse a reading older than the configured bounds, so a stale/missing reading can never
        drive a write — the first one included.
        """
        return (
            telemetry.soc_age_seconds > self._config.soc_telemetry_max_age_seconds
            or telemetry.grid_age_seconds > self._config.control_telemetry_max_age_seconds
            or telemetry.battery_age_seconds > self._config.control_telemetry_max_age_seconds
        )

    def _live_charge_limit_w(self, device_id: str, mode: DispatchMode, now: datetime) -> float | None:
        """The verified+fresh live charge ceiling in watts, or ``None`` to leave the static bound.

        Only applies to grid charging. The provider returns ``None`` for a missing/invalid/
        unverified limit; a returned limit must additionally be fresh against
        ``live_charge_limit_max_age_seconds``. Anything short of verified AND fresh yields ``None``
        (fall back to the configured static bound), and no value is ever invented.
        """
        if mode is not DispatchMode.CHARGE_FROM_GRID:
            return None
        limit = self._live_charge_limit.live_charge_limit(device_id)
        if limit is None:
            return None
        if not limit.is_fresh(now, self._config.live_charge_limit_max_age_seconds):
            return None
        return limit.value_w

    async def tick(self, device_id: str) -> DispatchStatus:
        async with self._lock(device_id):
            record = await self._get(device_id)
            if record.state is DispatchState.FAULT_RESTORE_PENDING:
                # Retry a stuck restore once its backoff has elapsed; without this a device that
                # fails one attempt stays stuck until an operator or a restart intervenes.
                if record.next_restore_at is not None and self._clock.now() >= record.next_restore_at:
                    await self._restore(record, record.stop_reason or StopReason.DEVICE_ERROR, record.fault_code)
                return self._status(record)
            if record.state in (
                DispatchState.PRECHECK,
                DispatchState.APPLYING,
                DispatchState.REPLACING,
                DispatchState.RESTORING,
            ):
                # An interrupted precheck/apply/restore left hardware possibly half-written;
                # submit() holds this lock while it runs, so this is never live. PRECHECK is
                # included because submit() persists the intent as PRECHECK *before* read_snapshot()
                # (D-08); if that read raised there is no cleanup path, so without this the record
                # stays PRECHECK forever and its TTL is never processed. For PRECHECK+snapshot=None
                # _restore_guarded() resets to IDLE without a hardware restore (nothing was written
                # yet); a PRECHECK that already carries a snapshot is restored like any other.
                await self._restore(record, StopReason.DEVICE_ERROR, "recovery_required")
                return self._status(record)
            if record.state not in (
                DispatchState.CHARGING,
                DispatchState.DISCHARGING,
                DispatchState.HOLDING,
            ):
                return self._status(record)
            assert record.intent is not None
            if self._clock.now() >= record.intent.valid_until:
                await self._restore(record, StopReason.TTL_EXPIRED)
                return self._status(record)
            try:
                telemetry = await self._gateway.read_control_telemetry(device_id)
                if self._telemetry_stale(telemetry):
                    await self._restore(record, StopReason.TELEMETRY_STALE)
                    return self._status(record)
                if target_reached(record.intent.mode, telemetry.soc_percent, record.intent.target_soc_percent):
                    record.state = DispatchState.TARGET_REACHED
                    await self._put(record)
                    await self._restore(record, StopReason.TARGET_REACHED)
                    return self._status(record)
                live_limit_w = self._live_charge_limit_w(device_id, record.intent.mode, self._clock.now())
                # Transient execution ceiling, recomputed every tick from the current live limit;
                # the durable intent.max_power_w is unchanged so an increased live limit can recover.
                execution_ceiling = effective_charge_limit_w(record.intent.max_power_w, live_limit_w)
                desired = calculate_setpoint(
                    record.intent.mode,
                    telemetry,
                    target_soc_percent=record.intent.target_soc_percent,
                    max_power_w=execution_ceiling,
                    config=self._config,
                    last=record.last_commanded,
                )
                # A live charge ceiling that drops below the current setpoint is a safety derate and
                # must be written immediately; an increase obeys the normal deadband/interval.
                charge_derate = (
                    record.intent.mode is DispatchMode.CHARGE_FROM_GRID
                    and desired.direction is PowerDirection.CHARGE
                    and desired.watts < record.last_commanded.watts
                )
                if should_write(
                    desired,
                    record.last_commanded,
                    now=self._clock.now(),
                    last_write_at=record.last_write_at,
                    config=self._config,
                    export_cut=(
                        record.intent.mode is DispatchMode.DISCHARGE_TO_LOAD and telemetry.grid_import_w <= 0
                    ),
                    charge_derate=charge_derate,
                ):
                    record.plan = [{"name": "setpoint", "status": "sent"}]
                    await self._put(record)
                    await self._gateway.apply_setpoint(device_id, desired)
                    record.plan[0]["status"] = "confirmed"
                    record.last_commanded = desired
                    record.last_write_at = self._clock.now()
                    await self._put(record)
            except Exception as exc:  # noqa: BLE001
                await self._restore(record, StopReason.DEVICE_ERROR, getattr(exc, "code", "internal_error"))
            return self._status(record)

    async def _restore(
        self, record: DispatchRecord, reason: StopReason, fault_code: str | None = None
    ) -> None:
        # Restore writes hardware, so a cancelled caller (cancel(), submit()'s error paths,
        # force_restore_or_raise, switch_to_internal) must not interrupt it mid-sequence and leave
        # the record in RESTORING with the battery partially externally controlled (P2-1). Shield it
        # with the same idiom run() uses for tick(); re-shielding when already inside a shielded
        # tick() is harmless.
        await self._shield_hardware_critical(self._restore_critical(record, reason, fault_code))

    async def _restore_critical(
        self, record: DispatchRecord, reason: StopReason, fault_code: str | None = None
    ) -> None:
        # The barrier spans the whole sequence, failures included, so a caller write queued before
        # the restore can never run after it and overwrite the restored values.
        async with self._gateway.restore_barrier(record.device_id):
            await self._restore_guarded(record, reason, fault_code)

    async def _restore_guarded(
        self, record: DispatchRecord, reason: StopReason, fault_code: str | None = None
    ) -> None:
        if record.snapshot is None:
            record.state = DispatchState.IDLE
            record.intent = None
            record.restore_required = False
            record.stop_reason = reason
            record.fault_code = None
            record.next_restore_at = None
            record.restore_attempts = 0
            await self._put(record)
            self._restore_pending.discard(record.device_id)
            return
        record.state = DispatchState.RESTORING
        record.restore_required = True
        record.stop_reason = reason
        record.fault_code = fault_code
        record.plan = [{"name": f"restore_{step}", "status": "planned"} for step in range(4)]
        await self._put(record)
        try:
            # Stop external power before restoring the original framework values.
            record.plan.insert(0, {"name": "stop", "status": "sent"})
            await self._put(record)
            await self._gateway.apply_setpoint(record.device_id, PowerSetpoint())
            record.plan[0]["status"] = "confirmed"
            await self._put(record)
            for step in range(4):
                await self._step(
                    record,
                    step + 1,
                    lambda step=step: self._gateway.restore(record.device_id, record.snapshot, step),
                )
        except Exception as exc:  # noqa: BLE001
            record.state = DispatchState.FAULT_RESTORE_PENDING
            record.restore_required = True
            record.fault_code = getattr(exc, "code", fault_code or "internal_error")
            record.restore_attempts += 1
            delay = min(
                _RESTORE_BACKOFF_BASE_SECONDS * 2 ** min(record.restore_attempts - 1, 20),
                _RESTORE_BACKOFF_MAX_SECONDS,
            )
            record.next_restore_at = self._clock.now() + timedelta(seconds=delay)
            log.warning(
                "Restore of device %s failed (attempt %d), next retry in %.0f s",
                record.device_id,
                record.restore_attempts,
                delay,
            )
            await self._put(record)
            self._restore_pending.add(record.device_id)
            return
        record.state = DispatchState.IDLE
        record.intent = None
        record.snapshot = None
        record.last_commanded = PowerSetpoint()
        record.restore_required = False
        record.plan = []
        record.fault_code = None
        record.next_restore_at = None
        record.restore_attempts = 0
        await self._put(record)
        self._restore_pending.discard(record.device_id)

    async def cancel(self, device_id: str) -> DispatchStatus:
        async with self._lock(device_id):
            record = await self._get(device_id)
            if record.intent is None and not record.restore_required:
                raise DispatchRejected("dispatch_not_found")
            await self._restore(record, StopReason.OPERATOR_CANCELLED)
            return self._status(record)

    async def switch_to_internal(self, device_id: str, *, idle_only: bool = False) -> None:
        async with self._lock(device_id):
            record = await self._get(device_id)
            async with self._gateway.restore_barrier(device_id):
                if record.state is not DispatchState.IDLE or record.intent is not None or record.restore_required:
                    if idle_only:
                        raise DispatchRejected("dispatch_operation_conflict")
                    await self._restore_guarded(record, StopReason.OPERATOR_CANCELLED)
                    if record.state is not DispatchState.IDLE or record.restore_required:
                        raise DispatchRejected("dispatch_restore_required")
                await self._gateway.apply_internal_strategy(device_id)

    def set_limits(self, limits: dict[str, DeviceLimits]) -> None:
        """Replace the per-device power limits in place, for a live device-list reconfiguration."""
        self._limits = limits

    async def set_capability(self, device_id: str, record: CapabilityRecord) -> None:
        """Persist one capability of one device and only then take it over into the registry.

        This controller is the single writer of the dispatch database. The write runs off the event
        loop (no fsync in it) and under the same per-device lock as ``submit()``/``tick()``, so a
        gate change can never land in the middle of an apply sequence of the same device. If the
        commit fails the exception propagates and the in-memory state stays exactly as it was.
        """
        if record.device_id != device_id:
            raise ValueError("capability record belongs to another device")
        async with self._lock(device_id):
            # D2 (variant A, immutable during an active operation): unconditional — this also
            # closes the VERIFIED->VERIFIED bypass, which only ever existed because the old
            # admin-layer check skipped the VERIFIED branch entirely.
            await self._raise_if_active(device_id, [record.name])
            await asyncio.to_thread(self._store.put_capability, record)
            self._capabilities.replace(record)

    async def set_capabilities(self, device_id: str, records: list[CapabilityRecord]) -> None:
        """Persist several capabilities of one device in one transaction, then take them all over.

        Same lock and active-operation guard as ``set_capability()``; a failed commit leaves the
        registry untouched, so a verification can never end up half applied.
        """
        if any(record.device_id != device_id for record in records):
            raise ValueError("capability record belongs to another device")
        async with self._lock(device_id):
            await self._raise_if_active(device_id, [record.name for record in records])
            await asyncio.to_thread(self._store.put_capabilities, records)
            for record in records:
                self._capabilities.replace(record)

    async def set_device_limits(self, device_id: str, limits: DeviceLimits) -> None:
        """Persist the power limits and the engineering-mode switch of one device, then take over."""
        async with self._lock(device_id):
            # A limits/engineering-mode change could invalidate the effective_power the controller
            # already applied for the active operation (same hazard class as a capability flip).
            await self._raise_if_active(device_id)
            await asyncio.to_thread(self._store.put_device_config, device_id, limits)
            self._limits[device_id] = limits

    async def set_soc_target_policy(self, device_id: str, policy: SocTargetPolicy) -> None:
        """Persist how one device derives its device-level SoC target, then take it over.

        The single writer of the policy table, for the same reasons ``set_capability`` is the single
        writer of the capability table: the write runs off the event loop under the per-device lock,
        it is refused while an operation of that device is active (the derivation would change under
        an applied setpoint), and the registry the adapter reads is updated only after the commit.
        """
        if policy.device_id != device_id:
            raise ValueError("soc target policy belongs to another device")
        async with self._lock(device_id):
            await self._raise_if_active(device_id)
            await asyncio.to_thread(self._store.put_soc_target_policy, policy)
            self._soc_target_policies.replace(policy)

    async def reset_identity_bound_state(self, device_ids: Iterable[str]) -> None:
        """Clear the identity-bound persistent state of every device in ``device_ids`` atomically.

        For a device-list reconfiguration that frees or re-addresses a device_id: the old physical
        device's capability evidence, engineering-mode switch and SoC-target policy must not survive
        onto whatever device now answers at that id. All of it is reset in ONE store transaction, so
        a mid-sequence failure can no longer leave a persisted mix (verification wiped but policy
        kept, or engineering mode off but capabilities intact) that the reconfiguration cannot roll
        back. The in-memory registries are updated only after the commit returns, the same
        commit-then-publish order the single-device setters follow. The power limits are a
        site/installation property, so they are kept; only the engineering switch is cleared.

        Takes every affected device's lock first (in a deterministic order to avoid a lock cycle)
        and refuses while any of them has an active operation, exactly like the single-device
        setters: a reset must not land in the middle of an apply sequence.
        """
        ids = sorted(set(device_ids))
        if not ids:
            return
        async with contextlib.AsyncExitStack() as stack:
            for device_id in ids:
                await stack.enter_async_context(self._lock(device_id))
            for device_id in ids:
                await self._raise_if_active(device_id)
            await asyncio.to_thread(self._store.reset_identity_bound_state, ids)
            for device_id in ids:
                self._capabilities.reset(device_id)
                self._soc_target_policies.reset(device_id)
                limits = self._limits.get(device_id)
                if limits is not None and limits.engineering_mode:
                    self._limits[device_id] = replace(limits, engineering_mode=False)

    def capabilities(self, device_id: str) -> tuple[CapabilityRecord, ...]:
        return self._capabilities.all(device_id)

    def device_limits(self, device_id: str) -> DeviceLimits | None:
        return self._limits.get(device_id)

    def verified_live_charge_limit_w(self, device_id: str) -> float | None:
        """The verified, fresh live grid-charge ceiling in watts, or ``None`` when there is none.

        Read-only view for planning; ``None`` is "no verified limit", never "unlimited".
        """
        return self._live_charge_limit_w(device_id, DispatchMode.CHARGE_FROM_GRID, self._clock.now())

    def soc_target_policy(self, device_id: str) -> SocTargetPolicy:
        return self._soc_target_policies.policy(device_id)

    async def force_restore_or_raise(
        self, device_id: str, reason: StopReason = StopReason.DEVICE_RECONFIGURED
    ) -> None:
        """Synchronously drive ``device_id`` to a clean, restored state before its transport is
        closed by a device-list reconfiguration. A no-op if there is nothing to restore.

        Raises ``ReconfigurationRejected`` if the restore does not end in a clean ``IDLE`` state
        (e.g. it lands in ``FAULT_RESTORE_PENDING``), so the caller can abort the whole
        reconfiguration before any teardown of the old graph has started — the old transport is
        still live at this point, which is what the restore writes through.
        """
        async with self._lock(device_id):
            record = await self._get(device_id)
            if record.intent is None and not record.restore_required:
                return
            await self._restore(record, reason)
            if record.state is not DispatchState.IDLE:
                raise ReconfigurationRejected(device_id, record.fault_code)

    async def _read_candidates(self) -> list[DispatchRecord]:
        records, skipped = await asyncio.to_thread(self._store.all_with_skipped)
        self.unreadable_devices = tuple(skipped)
        for device_id in skipped:
            # No restore is possible for a record that cannot be read; an operator has to look.
            log.critical(
                "Dispatch record of device %s is unreadable: its battery settings are NOT restored "
                "automatically and the device stays blocked for dispatch",
                device_id,
            )
        return records

    async def restore_retry_info(self, device_id: str) -> tuple[int, datetime | None]:
        """Failed restore attempts and the next automatic retry time (admin/energy status only)."""
        record = await self._get(device_id)
        return record.restore_attempts, record.next_restore_at

    async def recover(self) -> None:
        # DispatchStore.all_with_skipped() skips an individually unreadable row. Records read here are
        # only candidates: each one is re-read under its device lock, because a tick may have
        # advanced it since, and a stale object would fail the store's CAS check.
        candidates = await self._read_candidates()
        for candidate in candidates:
            if candidate.state is DispatchState.IDLE and not candidate.restore_required:
                continue
            device_id = candidate.device_id
            try:
                async with self._lock(device_id):
                    record = await self._get(device_id)
                    if record.state is DispatchState.IDLE and not record.restore_required:
                        continue
                    # PRECHECK-with-no-snapshot is the narrow crash window right after the first
                    # durable-intent write in submit(), before read_snapshot() runs: nothing to
                    # restore from yet, so the record is reset to IDLE.
                    if record.state is DispatchState.PRECHECK and record.snapshot is None:
                        record.state = DispatchState.IDLE
                        record.intent = None
                        await self._put(record)
                        self._restore_pending.discard(device_id)
                    else:
                        # Normal post-crash case (APPLYING/REPLACING with snapshot) and, defensively,
                        # PRECHECK with a snapshot. _restore_guarded() maintains self._restore_pending
                        # (add on FAULT_RESTORE_PENDING, discard on a clean restore), so a device that
                        # a later tick() heals clears itself without a restart (readiness unlatched).
                        await self._restore(record, StopReason.DEVICE_ERROR, "recovery_required")
            except Exception:
                # One failing device must not abort the sweep for every other device. A device whose
                # restore raised outright is treated as still restore-pending until it heals.
                log.exception("Dispatch recovery failed for device %s", device_id)
                self._restore_pending.add(device_id)
        self._recovery_complete = True

    @property
    def restore_pending_devices(self) -> tuple[str, ...]:
        """Devices whose battery restore is still outstanding, derived live from the controller
        rather than a one-off startup snapshot: an automatic tick() retry that heals a device drops
        it here, so readiness recovers without an application restart (C2 self-heal).
        """
        return tuple(sorted(self._restore_pending))

    def recovery_ready(self) -> bool:
        """True once the startup sweep has run and no device is left unreadable or with an
        outstanding restore. Read live by /readiness so a post-startup self-heal flips readiness
        back to green on its own, while a sweep that has not completed yet keeps it red (C2).
        """
        return self._recovery_complete and not (self.unreadable_devices or self._restore_pending)

    async def shutdown_restore(self) -> None:
        """Best-effort restore while serializers still accept device work."""
        candidates = await self._read_candidates()
        for candidate in candidates:
            if candidate.intent is None and not candidate.restore_required:
                continue
            device_id = candidate.device_id
            try:
                async with self._lock(device_id):
                    record = await self._get(device_id)
                    if record.intent is None and not record.restore_required:
                        continue
                    await self._restore(record, StopReason.SHUTDOWN)
            except Exception:
                log.exception("Shutdown restore failed for device %s", device_id)

    async def run(self, device_id: str) -> None:
        while True:
            await self._clock.sleep(self._config.cycle_interval_seconds)
            # Cancellation during the sleep above takes effect immediately; a tick already under
            # way must not be interrupted mid hardware-write (C1), so it is shielded and, on
            # cancellation, awaited to completion before the CancelledError propagates.
            tick_task = asyncio.create_task(self.tick(device_id))
            try:
                await asyncio.shield(tick_task)
            except asyncio.CancelledError:
                try:
                    await tick_task
                except Exception:
                    # A dead loop would stop TTL/stale/target handling for good; log and keep going.
                    log.exception("dispatch tick failed while stopping %s", device_id)
                raise
            except Exception:
                # A dead loop would stop TTL/stale/target handling for good; log and keep going.
                log.exception("dispatch tick failed for %s", device_id)
