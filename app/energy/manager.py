#!/usr/bin/env python3
#
# app/energy/manager.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The Energy Manager service (design 2.3): one place where a business action becomes a dispatch
command, and one place that decides whether a device accepts such a command at all.

It talks to the dispatch layer only through ``BatteryDispatchPort`` and it never imports the vendor
gateway package: the register names it needs for the write allowlist are injected as
``required_writes``, so the service stays vendor-neutral and unit-testable without an RCT adapter.

Both callers of stage 1 — the HTTP routers and, later, the tariff engine — enter here, so every
guarantee below holds for both.
"""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import timedelta

from app.clock import Clock
from app.config import DeviceEntry
from app.dispatch.base import BatteryDispatchPort
from app.dispatch.capabilities import CapabilityRegistry, GateDecision, evaluate_gate
from app.dispatch.controller import DispatchRejected
from app.dispatch.models import (
    DeviceLimits,
    DispatchCommand,
    DispatchConfig,
    DispatchMode,
    DispatchState,
    DispatchStatus,
    PowerDirection,
)
from app.dispatch.store import DispatchStore
from app.energy.errors import EnergyRejected
from app.energy.models import (
    _ACTION_FOR_MODE,
    _STATE_FOR_DISPATCH_STATE,
    _STOP_REASON_PUBLIC,
    ARMED_BY_MAX_LENGTH,
    COMMAND_TTL_SECONDS,
    TARGET_SOC_MAX_PERCENT,
    TARGET_SOC_MIN_PERCENT,
    ActionAvailability,
    ActionReason,
    ArmedRecord,
    EnergyAction,
    EnergyCommand,
    EnergyDeviceStatus,
    EnergyState,
    TargetSocWindow,
)
from app.energy.readings import EnergyReadingsPort, absent_readings
from app.errors import UnknownDevice

log = logging.getLogger(__name__)

# The action -> dispatch mode translation (design 2.2), performed only here. `auto` has no mode: it
# is a stop, and it goes through the port's cancel() path.
_MODE_FOR_ACTION: dict[EnergyAction, DispatchMode] = {
    EnergyAction.CHARGE: DispatchMode.CHARGE_FROM_GRID,
    EnergyAction.DISCHARGE: DispatchMode.DISCHARGE_TO_LOAD,
    EnergyAction.HOLD: DispatchMode.HOLD,
}
# Hard upper bound of an explicitly requested power, before the per-device limit clamps it down.
MAX_POWER_W = 50_000.0


def _actor_name(actor: str | None) -> str | None:
    """A caller-supplied name reduced to what ``ArmedRecord`` accepts: printable ASCII, bounded.

    Dropping the unusable characters is deliberate — an odd token name must not turn an arming into
    a 500 through ``ArmedRecord``'s validation.
    """
    if actor is None:
        return None
    printable = "".join(char for char in actor if 32 <= ord(char) <= 126)[:ARMED_BY_MAX_LENGTH]
    return printable or None


class EnergyManager:
    def __init__(
        self,
        *,
        port: BatteryDispatchPort | None,
        store: DispatchStore | None,
        readings: EnergyReadingsPort,
        clock: Clock,
        config: DispatchConfig,
        devices: Mapping[str, DeviceEntry],
        write_support_enabled: bool | Callable[[], bool],
        approve_writes: Callable[[Iterable[str]], Awaitable[Sequence[str]]] | None,
        approved_writes: Callable[[], Awaitable[tuple[str, ...]]] | None,
        allowlist_candidates: Callable[[], frozenset[str]] | None,
        required_writes: tuple[str, ...],
        armed: Mapping[str, ArmedRecord] | None = None,
    ) -> None:
        self._port = port
        self._store = store
        self._readings = readings
        self._clock = clock
        self._config = config
        self._devices = devices
        # A callable follows the live admin switch; a plain bool is fixed (in-process callers, tests).
        self._write_support_enabled = (
            write_support_enabled if callable(write_support_enabled) else (lambda: write_support_enabled)
        )
        # Setter, reader and candidate set are deliberately separate and all late-bound: an operator
        # can change the approved register names on the Inverters page at any time, so a snapshot
        # taken at construction time would go stale.
        self._approve_writes = approve_writes
        self._approved_writes = approved_writes
        self._allowlist_candidates = allowlist_candidates
        self._required_writes = required_writes
        self._armed_states: dict[str, ArmedRecord] = dict(armed or {})
        # Strictly outside the controller's per-device lock: the manager calls the port, never the
        # other way round, so the one-directional order rules a lock cycle out.
        self._locks: dict[str, asyncio.Lock] = {}
        self._approval_lock = asyncio.Lock()  # approvals are shared by all devices

    def attach_dispatch(
        self,
        *,
        port: BatteryDispatchPort,
        store: DispatchStore,
        readings: EnergyReadingsPort,
        armed: Mapping[str, ArmedRecord],
    ) -> None:
        """Bind the dispatch objects built after boot, when write access is enabled live."""
        self._port = port
        self._store = store
        self._readings = readings
        self._armed_states = dict(armed)

    # --- state ------------------------------------------------------------------------------

    def _lock(self, device_id: str) -> asyncio.Lock:
        return self._locks.setdefault(device_id, asyncio.Lock())

    def _entry(self, device_id: str) -> DeviceEntry:
        entry = self._devices.get(device_id)
        if entry is None:
            raise UnknownDevice(device_id=device_id)
        return entry

    def _record(self, device_id: str) -> ArmedRecord:
        return self._armed_states.get(device_id) or ArmedRecord(device_id)

    def armed(self, device_id: str) -> bool:
        return self._record(device_id).armed

    def armed_record(self, device_id: str) -> ArmedRecord:
        """The armed row of one device, for the admin surface's display-only fields."""
        return self._record(device_id)

    async def approved_write_names(self) -> tuple[str, ...]:
        """The live write allowlist, state-independent, for the admin Setup checklist.

        This is the same reader ``_missing_write_names`` consults, so it is authoritative in every
        arm state — unlike ``ArmedRecord.added_write_names``, which only records what arming itself
        contributed. A never-armed or disarmed device whose required writes were approved on the
        Inverters page still reports them here.
        """
        return tuple(await self._approved_writes()) if self._approved_writes is not None else ()

    def required_write_names(self) -> tuple[str, ...]:
        """The register names every manual command needs, so the GUI need not repeat the list."""
        return tuple(self._required_writes)

    def gate_decisions(self, device_id: str) -> tuple[tuple[EnergyAction, GateDecision], ...]:
        """The raw gate decision per action, for the admin surface only.

        It is computed here rather than in the router so the router carries no business logic.
        ``auto`` has no gate evaluation at all — a handback is never capability-gated — so it reports
        the released decision explicitly instead of borrowing another action's.
        """
        released = GateDecision(allowed=True, engineering_mode=False, unverified=())
        if self._port is None:
            return tuple((action, released) for action in EnergyAction)
        limits = self._port.device_limits(device_id)
        engineering = limits.engineering_mode if limits is not None else False
        registry = CapabilityRegistry(self._port.capabilities(device_id))
        return tuple(
            (
                action,
                released
                if action is EnergyAction.AUTO
                else evaluate_gate(
                    _MODE_FOR_ACTION[action],
                    registry,
                    device_id=device_id,
                    device_engineering_mode=engineering,
                    limit_export=self._config.limit_export_during_discharge,
                ),
            )
            for action in EnergyAction
        )

    async def _missing_write_names(self) -> list[str]:
        """Required register names that are no longer approved.

        With no reader injected there is no allowlist to consult, and the write layer refuses on its
        own — so the check is skipped rather than guessed at.
        """
        if self._approved_writes is None:
            return []
        try:
            approved = frozenset(await self._approved_writes())  # ordered tuple in, membership set out
        except Exception:
            # An already-applied hardware command must not turn into a 500 just because the
            # allowlist read failed; fail closed and treat every required write as missing (H7).
            log.exception("Reading the approved write names failed; treating all required writes as missing")
            return list(self._required_writes)
        return [name for name in self._required_writes if name not in approved]

    # --- commands ---------------------------------------------------------------------------

    async def command(
        self, device_id: str, command: EnergyCommand, *, actor: str | None
    ) -> EnergyDeviceStatus:
        """Translate one business action into a dispatch command (design 2.3.1).

        The order of the checks is part of the contract: every refusal below happens before any
        device access.
        """
        self._entry(device_id)
        async with self._lock(device_id):
            if self._port is None:
                log.warning("Energy command refused: dispatch is not configured (device=%s)", device_id)
                raise EnergyRejected("dispatch_store_unavailable", device_id=device_id)
            if not self.armed(device_id):
                # No exception for `auto`: disarming performs the handback itself, so a disarmed
                # device has nothing left to hand back.
                log.info("Energy command refused: device %s is not armed", device_id)
                raise EnergyRejected("energy_manager_disarmed", device_id=device_id)
            # AUTO only ends our operation and replays the stored snapshot; it never submits a new
            # write command, so this preflight does not apply. The restore itself still passes the
            # gateway write allowlist (system=True skips only the request budget). The admin
            # Parameters endpoint refuses to revoke a required dispatch write name while any device
            # is armed or has an unfinished dispatch; if not, the handback fails closed below.
            if command.action is not EnergyAction.AUTO and not self._write_support_enabled():
                log.warning("Energy command refused: write support is disabled (device=%s)", device_id)
                raise EnergyRejected("energy_write_support_required", device_id=device_id)
            missing = await self._missing_write_names() if command.action is not EnergyAction.AUTO else []
            if missing:
                log.warning(
                    "Energy command refused: write approval for %s was revoked (device=%s)",
                    ", ".join(missing), device_id,
                )  # fmt: skip
                raise EnergyRejected("energy_action_unavailable", device_id=device_id, missing=missing)
            self._validate(command)
            if command.action is EnergyAction.AUTO:
                status = await self._handback(device_id)
            else:
                status = await self._port.submit(device_id, self._dispatch_command(device_id, command))
            log.info(
                "Energy command accepted: device=%s action=%s target=%s state=%s actor=%s",
                device_id, command.action.value, command.target_soc_percent, status.state.value,
                _actor_name(actor),
            )  # fmt: skip
            return await self._project(device_id, status)

    def _validate(self, command: EnergyCommand) -> None:
        """The field rules of design 2.10.1, repeated here so an in-process caller (the future
        tariff engine) gets exactly the guarantees an HTTP caller gets.
        """
        wants_target = command.action in (EnergyAction.CHARGE, EnergyAction.DISCHARGE)
        if wants_target:
            if command.target_soc_percent is None:
                raise EnergyRejected("invalid_request", field="target_soc_percent")
            self._validate_target(command.target_soc_percent)
        elif command.target_soc_percent is not None:
            raise EnergyRejected("invalid_request", field="target_soc_percent")
        if command.max_power_w is None:
            return
        if not wants_target:
            raise EnergyRejected("invalid_request", field="max_power_w")
        if not math.isfinite(command.max_power_w):
            raise EnergyRejected("value_not_finite", field="max_power_w")
        if not 0 < command.max_power_w <= MAX_POWER_W:
            raise EnergyRejected("value_out_of_range", field="max_power_w", maximum=MAX_POWER_W)

    def _validate_target(self, target: float) -> None:
        if not math.isfinite(target):
            raise EnergyRejected("value_not_finite", field="target_soc_percent")
        if not TARGET_SOC_MIN_PERCENT <= target <= TARGET_SOC_MAX_PERCENT:
            raise EnergyRejected("value_out_of_range", field="target_soc_percent")
        window = self._window()
        if not window.min <= target <= window.max:
            # The effective window is published on every status, so a caller can see what it must
            # stay inside; a target is never silently clamped, because that would change the goal.
            raise EnergyRejected(
                "value_out_of_range", field="target_soc_percent", minimum=window.min, maximum=window.max
            )

    def _dispatch_command(self, device_id: str, command: EnergyCommand) -> DispatchCommand:
        assert self._port is not None
        mode = _MODE_FOR_ACTION[command.action]
        valid_until = self._clock.now() + timedelta(seconds=COMMAND_TTL_SECONDS)
        if command.action is EnergyAction.HOLD:
            # A hold has no SoC goal and no power budget; the dispatch layer refuses anything else.
            return DispatchCommand(mode, None, 0.0, valid_until)
        limits = self._port.device_limits(device_id)
        if limits is None:
            log.warning("Energy command refused: no power limits configured for device %s", device_id)
            raise EnergyRejected("dispatch_limits_missing", device_id=device_id)
        power = command.max_power_w
        if power is None:
            power = (
                limits.max_charge_power_w
                if mode is DispatchMode.CHARGE_FROM_GRID
                else limits.max_discharge_power_w
            )
        return DispatchCommand(mode, command.target_soc_percent, power, valid_until)

    async def _handback(self, device_id: str) -> DispatchStatus:
        """``auto``: stop dispatch and check the outcome before reporting success.

        ``DispatchController.cancel()`` swallows a device error inside ``_restore()``, leaves the
        record in ``FAULT_RESTORE_PENDING`` and returns normally. Projecting that would tell an
        operator who just pressed "return to automatic operation" that the handback succeeded while
        the inverter is still under external control, so it fails closed instead. The record stays
        pending and ``tick()``'s automatic retry keeps running, which is why ``auto`` remains the
        retry.
        """
        assert self._port is not None
        try:
            status = await self._port.cancel(device_id)
        except DispatchRejected as exc:
            if exc.code != "dispatch_not_found":
                raise
            status = await self._port.status(device_id)  # idempotent: already automatic
        if status.state is DispatchState.FAULT_RESTORE_PENDING or status.restore_required:
            log.error("Energy handback incomplete: device %s still requires a restore", device_id)
            raise EnergyRejected("dispatch_restore_required", device_id=device_id)
        return status

    # --- arming ------------------------------------------------------------------------------

    async def set_armed(self, device_id: str, *, armed: bool, actor: str | None) -> EnergyDeviceStatus:
        self._entry(device_id)
        async with self._lock(device_id):
            if armed:
                await self._arm(device_id, actor)
            else:
                await self._disarm(device_id)
            status = None if self._port is None else await self._port.status(device_id)
            return await self._project(device_id, status)

    async def _arm(self, device_id: str, actor: str | None) -> None:
        """Design 2.6.1: read-only preflight, then the add-only approval, then the commit."""
        if not self._write_support_enabled():
            # Deliberately not flipped here: it is a global security switch the operator sets in Settings.
            log.warning("Arming refused: write support is disabled (device=%s)", device_id)
            raise EnergyRejected("energy_write_support_required", device_id=device_id)
        if (
            self._port is None
            or self._store is None
            or self._approve_writes is None
            or self._approved_writes is None
            or self._allowlist_candidates is None
        ):
            log.error("Arming refused: the dispatch store or the write allowlist is unavailable")
            raise EnergyRejected("dispatch_store_unavailable", device_id=device_id)
        limits: DeviceLimits | None = self._port.device_limits(device_id)
        if limits is None:
            log.warning("Arming refused: no power limits configured for device %s", device_id)
            raise EnergyRejected("dispatch_limits_missing", device_id=device_id)
        candidates = self._allowlist_candidates()
        unavailable = [name for name in self._required_writes if name not in candidates]
        if unavailable:
            # The allowlist file is operator-settable, so the shipped default is no guarantee.
            # Refuse before approving anything: without this the allowlist build raises a KeyError
            # and the operator gets a 500 instead of a refusal.
            log.error(
                "Arming refused: the configured write allowlist lacks %s (device=%s)",
                ", ".join(unavailable), device_id,
            )  # fmt: skip
            raise EnergyRejected(
                "energy_write_support_required", device_id=device_id, missing=unavailable
            )
        async with self._approval_lock:
            existing = list(await self._approved_writes())  # order as stored
            missing = [name for name in self._required_writes if name not in existing]
            if missing:
                # Add-only and order-preserving: the operator's own selection keeps its order, nothing
                # is removed, nothing is reordered, and duplicates are impossible.
                await self._approve_writes(existing + missing)
            current = self._record(device_id)
            record = ArmedRecord(
                device_id=device_id,
                armed=True,
                added_write_names=tuple(dict.fromkeys((*current.added_write_names, *missing))),
                armed_at=current.armed_at if current.armed else self._clock.now(),
                armed_by=current.armed_by if current.armed else _actor_name(actor),
            )
            if record == current:
                return  # arming twice is idempotent: no approval, no commit, no new timestamp
            try:
                await asyncio.to_thread(self._store.put_energy_state, record)
            except Exception:
                if missing:
                    # Remove only what this call added: another device may have changed the approvals.
                    still_approved = await self._approved_writes()
                    await self._approve_writes([n for n in still_approved if n not in missing])
                raise
        self._armed_states[device_id] = record  # memory only after the commit returned
        log.warning(
            "Energy Manager armed: device=%s added_writes=%s actor=%s",
            device_id, ",".join(missing) or "-", record.armed_by,
        )  # fmt: skip

    async def _disarm(self, device_id: str) -> None:
        """Design 2.6.2: hand back first, refuse while the device is still controlled, remove no
        write approval — another feature, another device or the operator's own selection may depend
        on those registers.
        """
        current = self._record(device_id)
        if not current.armed:
            return  # a never-armed device is a true no-op: an expert-API dispatch stays untouched
        if self._port is not None:
            status = await self._port.status(device_id)
            if status.state is not DispatchState.IDLE or status.restore_required:
                await self._handback(device_id)  # raises dispatch_restore_required, stays armed
        if self._store is None:
            raise EnergyRejected("dispatch_store_unavailable", device_id=device_id)
        record = ArmedRecord(
            device_id=device_id,
            armed=False,
            # Kept for display: it records what arming once contributed, and disarming revokes none
            # of it.
            added_write_names=current.added_write_names,
        )
        await asyncio.to_thread(self._store.put_energy_state, record)
        self._armed_states[device_id] = record
        log.warning("Energy Manager disarmed: device=%s (write approvals left untouched)", device_id)

    # --- status ------------------------------------------------------------------------------

    async def status(self, device_id: str) -> EnergyDeviceStatus:
        self._entry(device_id)
        status = None if self._port is None else await self._port.status(device_id)
        return await self._project(device_id, status)

    def _window(self) -> TargetSocWindow:
        return TargetSocWindow(
            min=max(TARGET_SOC_MIN_PERCENT, self._config.min_soc),
            max=min(TARGET_SOC_MAX_PERCENT, self._config.max_soc),
        )

    def _gate(self, device_id: str, mode: DispatchMode, limits: DeviceLimits):
        """A throwaway read-only capability snapshot per preflight: the controller stays the single
        writer of the registry, so the manager never holds one of its own.
        """
        assert self._port is not None
        registry = CapabilityRegistry(self._port.capabilities(device_id))
        return evaluate_gate(
            mode,
            registry,
            device_id=device_id,
            device_engineering_mode=limits.engineering_mode,
            limit_export=self._config.limit_export_during_discharge,
        )

    async def _project(self, device_id: str, status: DispatchStatus | None) -> EnergyDeviceStatus:
        action = _ACTION_FOR_MODE.get(status.mode) if status is not None and status.mode else None
        state = (
            _STATE_FOR_DISPATCH_STATE[status.state] if status is not None else EnergyState.AUTOMATIC
        )
        stop_reason = (
            _STOP_REASON_PUBLIC.get(status.stop_reason)
            if status is not None and status.stop_reason is not None
            else None
        )
        try:
            readings = self._readings.readings(device_id)
        except Exception:
            # A status must not be able to fail on an advisory figure: a stale or broken reading
            # cannot be allowed to block the stop command an operator needs.
            log.warning("Energy readings for device %s could not be read", device_id, exc_info=True)
            readings = absent_readings()
        return EnergyDeviceStatus(
            device_id=device_id,
            armed=self.armed(device_id),
            state=state,
            action=action,
            target_soc_percent=status.target_soc_percent if status is not None else None,
            # No power budget applies while idle or while holding; the commanded pair carries the
            # zero instead.
            power_limit_w=(
                status.max_power_w
                if status is not None and action in (EnergyAction.CHARGE, EnergyAction.DISCHARGE)
                else None
            ),
            power_limit_clamped=(
                status is not None and status.max_power_w < status.max_power_w_requested
            ),
            commanded_power_w=status.commanded_power_w if status is not None else 0.0,
            commanded_direction=(
                status.commanded_direction if status is not None else PowerDirection.NONE
            ),
            until=status.valid_until if status is not None else None,
            stop_reason=stop_reason,
            time_limited=self._time_limited(device_id, status),
            target_soc_window=self._window(),
            readings=readings,
            actions=await self.action_availability(device_id, status),
        )

    def _time_limited(self, device_id: str, status: DispatchStatus | None) -> bool:
        """True when the running command only runs because the per-device engineering switch
        released unverified hardware — it will stop sooner, without naming which capability is in
        doubt (the raw reason stays on the admin surface).
        """
        if self._port is None or status is None or status.mode is None:
            return False
        limits = self._port.device_limits(device_id)
        if limits is None:
            return False
        return self._gate(device_id, status.mode, limits).engineering_mode

    async def action_availability(
        self, device_id: str, status: DispatchStatus | None
    ) -> tuple[ActionAvailability, ...]:
        """One read-only preflight per action, with the five business reasons of design 2.3.3.

        ``auto`` is never blocked by the capability gate — handing control back must not depend on a
        verification — and a pending restore makes it the retry, so it stays available there too.
        Its availability is exactly ``armed``, which is what keeps a GUI from offering a button that
        answers 409.
        """
        reason = await self._blocking_reason(device_id)
        return tuple(
            ActionAvailability(action, available=False, reason=reason)
            if reason is not None
            and not (action is EnergyAction.AUTO and reason is ActionReason.WRITE_NOT_PERMITTED)
            else self._availability(device_id, action, status)
            for action in EnergyAction
        )

    async def _blocking_reason(self, device_id: str) -> ActionReason | None:
        """The reason that blocks every action of this device, ``None`` when none does."""
        if not self.armed(device_id) or self._port is None:
            return ActionReason.NOT_ARMED
        if await self._missing_write_names():
            return ActionReason.WRITE_NOT_PERMITTED
        return None

    def _availability(
        self, device_id: str, action: EnergyAction, status: DispatchStatus | None
    ) -> ActionAvailability:
        assert self._port is not None
        if action is EnergyAction.AUTO:
            return ActionAvailability(action, available=True)
        limits = self._port.device_limits(device_id)
        if limits is None:
            return ActionAvailability(action, available=False, reason=ActionReason.LIMITS_MISSING)
        if status is not None and status.state is DispatchState.FAULT_RESTORE_PENDING:
            return ActionAvailability(action, available=False, reason=ActionReason.RESTORE_REQUIRED)
        if not self._gate(device_id, _MODE_FOR_ACTION[action], limits).allowed:
            return ActionAvailability(
                action, available=False, reason=ActionReason.HARDWARE_NOT_VERIFIED
            )
        return ActionAvailability(action, available=True)
