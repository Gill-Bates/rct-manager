#!/usr/bin/env python3
#
# app/energy/plan_handoff.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The default-OFF execution handoff (design Part 4).

:class:`PlanDispatchHandoff` can offer the active plan slot to the existing Energy Manager command
path, **disabled by default**. It is the "external" controller: it always calls
``EnergyManager.command(device_id, EnergyCommand(...), actor=_HANDOFF_ACTOR)`` with the fixed,
non-admin actor name :data:`_HANDOFF_ACTOR`, so every existing guard (mode authorization, write
enable, allowlist, capability gate, static limits, SoC re-validation, ``auto`` recovery) applies
unchanged — the handoff adds no bypass.

Master switch (default OFF): the ``enable_automatic_dispatch`` config flag. While it is OFF — the
shipped state — the handoff computes and *logs* the command it would send and the slot it covers,
but issues no ``command()`` call at all. This work does not enable automatic hardware commands.

Idempotence: the handoff keeps a per-device record of the last-offered intent (mode, target SoC,
slot start/end) and offers a command only on a change of the effective desired action for the
current slot. A missing slot or any action other than explicit grid charging is an ``AUTO``
handback, so a command cannot outlive its plan slot. On restart it holds no in-memory state and
reads the live dispatch status first, offering only when the device is not already running the
desired intent, so a restart cannot double-apply. On late data / recompute an unchanged intent is
a no-op.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from app.energy.base import EnergyManagerPort
from app.energy.errors import EnergyRejected
from app.energy.models import (
    ADMIN_ACTOR,
    CHANGED_BY_MAX_LENGTH,
    EnergyCommand,
    EnergyState,
)
from app.energy.models import EnergyAction as ExecutorAction
from app.optimizer.models import EnergyAction, EnergyPlanSlot

log = logging.getLogger(__name__)

# The fixed, non-admin actor the handoff always passes to EnergyManager.command(). Printable ASCII,
# length-bounded by CHANGED_BY_MAX_LENGTH and != ADMIN_ACTOR, so _actor_name() passes it through
# unaltered and it drives the "external" mode (never the admin surface).
_HANDOFF_ACTOR = "charging-strategy"

assert _HANDOFF_ACTOR != ADMIN_ACTOR
assert len(_HANDOFF_ACTOR) <= CHANGED_BY_MAX_LENGTH
assert all(32 <= ord(char) <= 126 for char in _HANDOFF_ACTOR)

# V1 executes explicit grid charging only. Natural PV charging and ordinary self-consumption stay
# under the inverter's automatic strategy, so every non-CHARGE planning state is an explicit
# handback. This also fails closed if a future planner accidentally emits discharge or hold while
# the automatic executor is enabled.
_EXECUTOR_ACTION_FOR: dict[EnergyAction, ExecutorAction] = {
    EnergyAction.IDLE: ExecutorAction.AUTO,
    EnergyAction.CHARGE: ExecutorAction.CHARGE,
    EnergyAction.DISCHARGE: ExecutorAction.AUTO,
    EnergyAction.HOLD: ExecutorAction.AUTO,
    EnergyAction.AUTO: ExecutorAction.AUTO,
}

# EnergyRejected codes that are expected and non-fatal: the device is off/manual, writes are
# disabled, the hardware is unverified, or a restore is still pending. They are logged, never
# retried aggressively; the next recompute re-evaluates.
_EXPECTED_REFUSALS = frozenset(
    {
        "energy_manager_off",
        "energy_manager_not_external",
        "energy_write_support_required",
        "energy_action_unavailable",
        "dispatch_restore_required",
    }
)


@dataclass(frozen=True, slots=True)
class _Intent:
    """The effective desired operation for a slot, used for per-device dedup."""

    action: ExecutorAction
    target_soc_percent: float | None
    max_power_w: float | None
    slot_start: datetime | None
    slot_end: datetime | None


class PlanDispatchHandoff:
    """Offer the active plan slot to the Energy Manager, gated OFF by default.

    ``enable_automatic_dispatch`` is a plain bool fixed at construction (no live re-read); while it is ``False``
    the handoff computes and logs the command it would send but issues no ``command()`` call.
    """

    def __init__(
        self,
        *,
        manager: EnergyManagerPort,
        enable_automatic_dispatch: bool,
    ) -> None:
        """Construct the handoff.

        ``enable_automatic_dispatch`` is a plain ``bool`` captured once here (stored as
        ``self._enabled``); it is not a callable and is never re-read live. OFF (``False``) is the
        shipped default, in which the handoff only computes and logs the command it would send.
        Changing the flag at runtime has no effect on an existing instance.
        """
        self._manager = manager
        self._enabled = enable_automatic_dispatch
        # Per-device last-offered intent. Empty on process start (restart holds no state).
        self._last_offered: dict[str, _Intent] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def offer(self, device_id: str, slot: EnergyPlanSlot | None) -> bool:
        """Serialize offers for one device so concurrent recomputes cannot double-command."""
        async with self._locks.setdefault(device_id, asyncio.Lock()):
            return await self._offer_locked(device_id, slot)

    async def _offer_locked(self, device_id: str, slot: EnergyPlanSlot | None) -> bool:
        """Offer the active slot for ``device_id``; return whether a ``command()`` call was issued.

        A ``None`` slot or any action other than explicit grid charging requests an ``AUTO``
        handback. With automatic dispatch OFF the intended command is logged but never sent. With
        dedup, an unchanged intent is a no-op.
        """
        executor_action = ExecutorAction.AUTO if slot is None else _EXECUTOR_ACTION_FOR[slot.action]
        is_handback = executor_action is ExecutorAction.AUTO

        intent = _Intent(
            action=executor_action,
            target_soc_percent=None if is_handback else slot.target_soc_percent,
            max_power_w=None if is_handback else slot.max_power_w,
            # One stable handback intent spans every idle/no-plan slot; otherwise a new idle slot
            # would unnecessarily repeat AUTO after the device was already released.
            slot_start=None if is_handback else slot.start,
            slot_end=None if is_handback else slot.end,
        )
        command = EnergyCommand(
            action=executor_action,
            target_soc_percent=intent.target_soc_percent,
            max_power_w=intent.max_power_w,
        )

        if not self._enabled:
            # Default, shipped state: compute and log, issue no command.
            log.info(
                "Handoff is OFF; would offer %s (target=%s power=%s) for slot %s..%s on device %s",
                executor_action.value, intent.target_soc_percent, intent.max_power_w,
                intent.slot_start.isoformat() if intent.slot_start else "none",
                intent.slot_end.isoformat() if intent.slot_end else "none", device_id,
            )  # fmt: skip
            return False

        if self._last_offered.get(device_id) == intent:
            log.debug("Handoff: intent unchanged for device %s, no command issued", device_id)
            return False

        if device_id not in self._last_offered and await self._already_running(device_id, intent):
            # Restart: the device is already running this slot's intent; record it and do not
            # re-apply, so a restart cannot double-command.
            self._last_offered[device_id] = intent
            log.info("Handoff: device %s already running the slot intent on restart; no command", device_id)
            return False

        try:
            await self._manager.command(device_id, command, actor=_HANDOFF_ACTOR)
        except EnergyRejected as exc:
            if exc.code in _EXPECTED_REFUSALS:
                log.warning("Handoff command refused (expected): code=%s device=%s", exc.code, device_id)
                return False
            raise
        self._last_offered[device_id] = intent
        log.info("Handoff offered %s for slot %s..%s on device %s", executor_action.value,
                 intent.slot_start.isoformat() if intent.slot_start else "none",
                 intent.slot_end.isoformat() if intent.slot_end else "none", device_id)  # fmt: skip
        return True

    async def _already_running(self, device_id: str, intent: _Intent) -> bool:
        """Whether the device is already running ``intent`` (checked once, on restart).

        The Energy Manager + DispatchController persist and recover the authoritative device
        operation, so a restart reads the live status and only skips a re-apply when the running
        mode and target already match the slot's intent.
        """
        try:
            status = await self._manager.status(device_id)
        except Exception:
            log.warning("Handoff restart check: status for device %s could not be read", device_id, exc_info=True)
            return False
        running = status.action
        if intent.action is ExecutorAction.AUTO:
            # ``action is None`` is also used for executor modes outside the Energy Manager's
            # published action vocabulary (for example export), so only the projected automatic
            # state proves that no handback is required.
            return running is None and status.state is EnergyState.AUTOMATIC
        if running is None or running is not intent.action:
            return False
        return (
            status.target_soc_percent == intent.target_soc_percent
            and status.power_limit_w == intent.max_power_w
        )
