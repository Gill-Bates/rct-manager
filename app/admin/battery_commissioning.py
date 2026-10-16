#!/usr/bin/env python3
#
# app/admin/battery_commissioning.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Derive each inverter's commissioning date once and persist it per inverter serial.

``BatteryCommissioningService`` is a background service outside the hot path. For every device whose
serial is known and that has no stored commissioning date yet, it asks a history provider for the
earliest logged energy (read-only, budget-exempt device queries) and stores the answer with source
``logger_derived``. A device that cannot answer is retried with exponential backoff; a device whose
logs hold no energy is asked again only after a long interval. An operator-entered ``manual`` date
needs no derivation and always wins when read.

UNVERIFIED without hardware: see ``app.gateway.rct_history.RctCommissioningHistory``.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from app.admin.store import AdminStore
from app.clock import Clock
from app.errors import DeviceApiError

log = logging.getLogger(__name__)

STARTUP_DELAY_SECONDS = 90.0
CHECK_INTERVAL_SECONDS = 900.0
BACKOFF_BASE = timedelta(minutes=15)
BACKOFF_MAX = timedelta(hours=12)
EMPTY_RETRY = timedelta(hours=24)  # the logs held no energy yet (or nothing readable)


class CommissioningHistory(Protocol):
    async def earliest_energy(
        self, device_id: str, *, now: datetime
    ) -> datetime | None: ...


class BatteryCommissioningService:
    def __init__(
        self,
        provider: CommissioningHistory,
        store: AdminStore,
        clock: Clock,
        device_ids: Callable[[], list[str]],
        serial_of: Callable[[str], str | None],
        is_ready: Callable[[str], bool],
    ) -> None:
        self._provider = provider
        self._store = store
        self._clock = clock
        self._device_ids = device_ids
        self._serial_of = serial_of
        self._is_ready = is_ready
        self._failures: dict[str, int] = {}
        self._next_try: dict[str, datetime] = {}
        self._attached: set[tuple[str, str]] = set()

    async def run(self) -> None:
        """Check periodically; never raises except on cancellation."""
        await self._clock.sleep(STARTUP_DELAY_SECONDS)
        while True:
            try:
                await self.run_due_once()
            except Exception:
                log.exception("Battery commissioning cycle failed")
            await self._clock.sleep(CHECK_INTERVAL_SECONDS)

    async def run_due_once(self) -> None:
        for device_id in sorted(self._device_ids()):
            serial = self._serial_of(device_id)
            if not serial:
                continue  # the serial is the identity; there is no device_id fallback
            if (device_id, serial) not in self._attached:
                await asyncio.to_thread(
                    self._store.attach_inverter_serial, device_id, serial
                )
                self._attached.add((device_id, serial))
            if (
                await asyncio.to_thread(self._store.get_inverter_commissioning, serial)
                is not None
            ):
                continue
            due = self._next_try.get(serial)
            if not self._is_ready(device_id) or (
                due is not None and self._clock.now() < due
            ):
                continue
            await self.derive(device_id, serial)

    async def derive(self, device_id: str, serial: str) -> bool:
        """One derivation attempt; True when a date was stored."""
        now = self._clock.now()
        try:
            earliest = await self._provider.earliest_energy(device_id, now=now)
        except asyncio.CancelledError:
            raise
        except DeviceApiError as exc:
            self._back_off(serial, now)
            log.info(
                "Inverter %s: commissioning date not readable (%s); retrying later",
                device_id,
                exc.code,
            )
            return False
        except Exception:
            self._back_off(serial, now)
            log.exception(
                "Inverter %s: commissioning date derivation failed", device_id
            )
            return False
        self._failures.pop(serial, None)
        if earliest is None:
            self._next_try[serial] = now + EMPTY_RETRY
            log.info(
                "Inverter %s: no logged energy found yet; commissioning date stays unknown",
                device_id,
            )
            return False
        await asyncio.to_thread(
            self._store.set_inverter_commissioning, serial, earliest, "logger_derived"
        )
        self._next_try.pop(serial, None)
        log.info(
            "Inverter %s: commissioning date derived from the energy loggers: %s",
            device_id,
            earliest.date().isoformat(),
        )
        return True

    def _back_off(self, serial: str, now: datetime) -> None:
        count = self._failures.get(serial, 0) + 1
        self._failures[serial] = count
        self._next_try[serial] = now + min(
            BACKOFF_BASE * 2 ** min(count - 1, 10), BACKOFF_MAX
        )
