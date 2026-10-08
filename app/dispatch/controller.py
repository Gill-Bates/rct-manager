#!/usr/bin/env python3
#
# app/dispatch/controller.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Battery dispatch state machine and recovery loop (REQ-100..REQ-125)."""

import asyncio
import logging
import math
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta

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
from app.dispatch.models import (
    DeviceLimits,
    DispatchCommand,
    DispatchConfig,
    DispatchIntent,
    DispatchMode,
    DispatchRecord,
    DispatchRecordCorrupt,
    DispatchState,
    DispatchStatus,
    PowerSetpoint,
    StopReason,
    phase_for,
)
from app.dispatch.soc_policy import SocTargetPolicy, SocTargetPolicyRegistry
from app.dispatch.store import DispatchStore
from app.dispatch.strategy import calculate_setpoint, target_reached
from app.errors import DeviceApiError

log = logging.getLogger(__name__)

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
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._clock = clock
        self._config = config
        self._limits = limits
        # The very same registry instance the adapter reads from: a second instance would be a
        # second truth, and a capability update has to take effect live, without a restart.
        self._capabilities = capabilities
        # Same single-instance rule as the capabilities: the adapter reads the policy this
        # controller writes, so an operator change takes effect on the next apply without a restart.
        self._soc_target_policies = soc_target_policies
        self._locks: dict[str, asyncio.Lock] = {}
        self.unreadable_devices: tuple[str, ...] = ()

    def _lock(self, device_id: str) -> asyncio.Lock:
        return self._locks.setdefault(device_id, asyncio.Lock())

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

    async def submit(self, device_id: str, command: DispatchCommand) -> DispatchStatus:
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
                device_engineering_mode=limits.engineering_mode,
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
            try:
                # The SoC submit() already read is handed down: _apply() adds no device read, and
                # the derivation of the device-level SoC target needs the measured value.
                await self._apply(record, soc_percent=soc)
            except Exception as exc:
                try:
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
        """Refuse a gate change while an operation of the device is active; the caller holds the lock.

        ``names`` limits the refusal to capabilities the active mode depends on; ``None`` refuses
        any change.
        """
        current = await self._get(device_id)
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
                desired = calculate_setpoint(
                    record.intent.mode,
                    telemetry,
                    target_soc_percent=record.intent.target_soc_percent,
                    max_power_w=record.intent.max_power_w,
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

    async def tick(self, device_id: str) -> DispatchStatus:
        async with self._lock(device_id):
            record = await self._get(device_id)
            if record.state is DispatchState.FAULT_RESTORE_PENDING:
                # Retry a stuck restore once its backoff has elapsed; without this a device that
                # fails one attempt stays stuck until an operator or a restart intervenes.
                if record.next_restore_at is not None and self._clock.now() >= record.next_restore_at:
                    await self._restore(record, record.stop_reason or StopReason.DEVICE_ERROR, record.fault_code)
                return self._status(record)
            if record.state in (DispatchState.APPLYING, DispatchState.REPLACING, DispatchState.RESTORING):
                # An interrupted apply/restore (failed _put, unexpected error) left hardware
                # half-written; submit() holds this lock while it runs, so this is never live.
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
                if (
                    telemetry.soc_age_seconds > self._config.soc_telemetry_max_age_seconds
                    or telemetry.grid_age_seconds > self._config.control_telemetry_max_age_seconds
                    or telemetry.battery_age_seconds > self._config.control_telemetry_max_age_seconds
                ):
                    await self._restore(record, StopReason.TELEMETRY_STALE)
                    return self._status(record)
                if target_reached(record.intent.mode, telemetry.soc_percent, record.intent.target_soc_percent):
                    record.state = DispatchState.TARGET_REACHED
                    await self._put(record)
                    await self._restore(record, StopReason.TARGET_REACHED)
                    return self._status(record)
                desired = calculate_setpoint(
                    record.intent.mode,
                    telemetry,
                    target_soc_percent=record.intent.target_soc_percent,
                    max_power_w=record.intent.max_power_w,
                    config=self._config,
                    last=record.last_commanded,
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
                ):
                    record.plan = [{"name": "setpoint", "status": "sent"}]
                    await self._put(record)
                    await self._gateway.apply_setpoint(device_id, desired)
                    record.plan[0]["status"] = "confirmed"
                    record.last_commanded = desired
                    record.last_write_at = self._clock.now()
                    await self._put(record)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self._restore(record, StopReason.DEVICE_ERROR, getattr(exc, "code", "internal_error"))
            return self._status(record)

    async def _restore(
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
        except asyncio.CancelledError:
            raise
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

    async def cancel(self, device_id: str) -> DispatchStatus:
        async with self._lock(device_id):
            record = await self._get(device_id)
            if record.intent is None and not record.restore_required:
                raise DispatchRejected("dispatch_not_found")
            await self._restore(record, StopReason.OPERATOR_CANCELLED)
            return self._status(record)

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

    def capabilities(self, device_id: str) -> tuple[CapabilityRecord, ...]:
        return self._capabilities.all(device_id)

    def device_limits(self, device_id: str) -> DeviceLimits | None:
        return self._limits.get(device_id)

    def soc_target_policy(self, device_id: str) -> SocTargetPolicy:
        return self._soc_target_policies.policy(device_id)

    async def force_restore_or_raise(self, device_id: str) -> None:
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
            await self._restore(record, StopReason.DEVICE_RECONFIGURED)
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
        # DispatchStore.all() skips and logs an individually unreadable row. Records read here are
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
                    else:
                        # Normal post-crash case (APPLYING/REPLACING with snapshot) and, defensively,
                        # PRECHECK with a snapshot.
                        await self._restore(record, StopReason.DEVICE_ERROR, "recovery_required")
            except asyncio.CancelledError:
                raise
            except Exception:
                # One failing device must not abort the sweep for every other device.
                log.exception("Dispatch recovery failed for device %s", device_id)

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
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Shutdown restore failed for device %s", device_id)

    async def run(self, device_id: str) -> None:
        while True:
            await self._clock.sleep(self._config.cycle_interval_seconds)
            try:
                await self.tick(device_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A dead loop would stop TTL/stale/target handling for good; log and keep going.
                log.exception("dispatch tick failed for %s", device_id)
