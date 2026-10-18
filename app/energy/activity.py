#!/usr/bin/env python3
#
# app/energy/activity.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The one rule that decides whether the Energy Manager does any operational work.

An inverter whose mode is ``off`` (or that has no stored mode, which is ``off`` by default) is at
rest: no battery action, no charging-plan computation and no Energy-Manager-initiated provider call
for it. The shared forecast jobs (spot market, weather) are not tied to one device: they fetch only
while at least one inverter is ``manual``. An ``external`` inverter is controlled by its own app
over the API and does not need the GUI's spot/weather inputs, so it does not enable fetching. The
mode is the single source of truth; nothing here is persisted.
"""

import asyncio
from collections.abc import Callable, Iterable

from app.energy.models import EnergyMode


class EnergyActivity:
    """Derive the activity of devices and shared jobs from the live mode of each inverter."""

    def __init__(
        self,
        mode_of: Callable[[str], EnergyMode],
        device_ids: Callable[[], Iterable[str]],
    ) -> None:
        self._mode_of = mode_of
        self._device_ids = device_ids
        self._changed = asyncio.Event()
        self._listeners: list[Callable[[], None]] = []

    def device_energy_active(self, device_id: str) -> bool:
        return self._mode_of(device_id) is not EnergyMode.OFF

    def any_energy_active(self) -> bool:
        return any(
            self.device_energy_active(device_id) for device_id in self._device_ids()
        )

    def device_manual_active(self, device_id: str) -> bool:
        return self._mode_of(device_id) is EnergyMode.MANUAL

    def any_manual_active(self) -> bool:
        """True only while at least one inverter is ``manual``.

        The shared forecast jobs (spot market, weather) are the GUI/manager's own inputs: an
        ``external`` inverter is driven by its own app over the API and does not need them, so only
        ``manual`` enables fetching. This is narrower than :meth:`any_energy_active`, which also
        counts ``external`` and gates per-device plan/optimizer work.
        """
        return any(
            self.device_manual_active(device_id) for device_id in self._device_ids()
        )

    def add_listener(self, listener: Callable[[], None]) -> None:
        """Call ``listener`` synchronously on every mode change (loop thread only)."""
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def notify(self) -> None:
        """Announce a committed mode change; waiters and listeners re-evaluate the live state."""
        previous, self._changed = self._changed, asyncio.Event()
        previous.set()
        for listener in tuple(self._listeners):
            listener()

    async def wait_until_any_active(self) -> None:
        while True:
            changed = (
                self._changed
            )  # captured before the check, so no change can slip through
            if self.any_energy_active():
                return
            await changed.wait()

    async def wait_until_any_manual(self) -> None:
        """Rest until at least one inverter is ``manual`` (the forecast jobs' activity gate)."""
        while True:
            changed = (
                self._changed
            )  # captured before the check, so no change can slip through
            if self.any_manual_active():
                return
            await changed.wait()
