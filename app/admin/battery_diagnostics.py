#!/usr/bin/env python3
#
# app/admin/battery_diagnostics.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Read-only evidence recorder for the battery's monthly capacity calibration.

It writes what the inverter reports before, during and after a calibration to daily JSONL files, so
a later hardware review can decide which register (if any) proves a completed calibration. Nothing
here feeds the planner, Battery Health or the calibration samples; it only reads and records.

Missing or failed values are written as such and never filled in. A cached value carries its own
sample identity (``sample_monotonic``), so an energy integration over ``battery_power`` counts each
measurement once, however many records repeat it.

``candidate_change`` and ``calibration_window_hint`` records are marked ``evidence_only`` and
``verified: false``: they flag where to look, not that a calibration happened or what it measured.
"""

import asyncio
import gzip
import json
import logging
import math
import shutil
from collections.abc import Callable, Sequence
from datetime import UTC, date, timedelta
from pathlib import Path

from app.api.runtime import READY_STATES
from app.clock import Clock
from app.errors import DeviceApiError
from app.gateway.cache import CacheFreshness

log = logging.getLogger(__name__)

DIRECTORY_NAME = "battery-diagnostics"
FILE_PREFIX = "battery-diagnostics-"
FAST_INTERVAL_SECONDS = 15.0
COUNTER_INTERVAL_SECONDS = 60.0
SLOW_INTERVAL_SECONDS = 300.0
CONFIG_INTERVAL_SECONDS = 86400.0
RETENTION_DAYS = 60
# Startup belongs to discovery and the first live reads; the recorder joins after it.
STARTUP_DELAY_SECONDS = 60.0

# Already read periodically for the dashboard and the Energy Manager: taken from the cache only.
CACHED_NAMES = ("battery_soc", "battery_power", "battery_status2")
# Lifetime throughput counters, read fresh once a minute.
COUNTER_NAMES = (
    "battery_stored_energy",
    "battery_used_energy",
    "battery_charged_amp_hours",
    "battery_discharged_amp_hours",
)
# Slow-moving calibration candidates and BMS status, read fresh every five minutes.
SLOW_NAMES = (
    "battery_ah_capacity",
    "power_mng_amp_hours_measured",
    "power_mng_amp_hours",
    "battery_soh",
    "battery_cycles",
    "battery_status2",
    "battery_status",
    "battery_bat_status",
    "battery_efficiency",
    "power_mng_bat_next_calib_date",
    "power_mng_calib_charge_power",
    "battery_bms_sn",
)
# Configuration is read at the first tick, every 24 hours, and on status transitions.
# These settings are writable on the device; the recorder only reads them.
CONFIG_NAMES = (
    "power_mng_bat_calib_reqularity",
    "power_mng_bat_calib_days_in_advance",
    "bat_mng_struct_bat_calib_soc_thresh",
    "bat_mng_struct_bat_calib_soc_threshold",
)
STATUS_NAME = "battery_status2"
BMS_NAME = "battery_bms_sn"
# A change between two fresh reads of these is written as an unverified candidate_change.
CANDIDATE_NAMES = ("power_mng_bat_next_calib_date",)
# Community-observed "Calibrating" bits of battery_status2 (see app.admin.api); not RCT-documented.
CALIBRATING_STATUS_BITS = 0x008 | 0x400


def _plain(value):
    # JSON has no NaN or infinity; a non-finite float is recorded as missing, not as a number.
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _calibrating(status) -> bool:
    return (
        isinstance(status, int)
        and status > 0
        and bool(status & CALIBRATING_STATUS_BITS)
    )


class BatteryDiagnosticsRecorder:
    def __init__(
        self,
        gateway,
        clock: Clock,
        directory: Path,
        device_ids: Callable[[], Sequence[str]],
        name_exists: Callable[[str], bool],
        capacity_source=None,
    ) -> None:
        self._gateway = gateway
        self._clock = clock
        self._directory = directory
        self._device_ids = device_ids
        self._name_exists = name_exists
        self._capacity = capacity_source
        self._next_counters: dict[str, float] = {}
        self._next_slow: dict[str, float] = {}
        self._next_config: dict[str, float] = {}
        self._last_status: dict[str, object] = {}
        self._last_candidates: dict[tuple[str, str], dict] = {}
        self._failing: set[tuple[str, str]] = set()
        self._housekept: date | None = None
        self._write_failed = False

    async def run(self) -> None:
        """Record on a fixed monotonic raster; a slow cycle skips slots instead of shifting them."""
        await self._clock.sleep(STARTUP_DELAY_SECONDS)
        deadline = self._clock.monotonic()
        while True:
            try:
                for device_id in sorted(self._device_ids()):
                    await self.tick(device_id)
            except Exception:
                log.exception("Battery diagnostics cycle failed")
            deadline += FAST_INTERVAL_SECONDS
            while deadline <= self._clock.monotonic():
                deadline += FAST_INTERVAL_SECONDS  # a skipped slot stays a visible gap
            await self._clock.sleep(deadline - self._clock.monotonic())

    async def tick(self, device_id: str) -> None:
        monotonic = self._clock.monotonic()
        topology = self._topology(device_id)
        cached = {name: self._cached(device_id, name) for name in CACHED_NAMES}
        records = [self._record("periodic_fast", device_id, cached, topology)]

        status = cached[STATUS_NAME]["value"]
        previous = self._last_status.get(device_id)
        if status is not None:
            self._last_status[device_id] = status
        if status is not None and previous is not None and status != previous:
            snapshot = await self._fresh(
                device_id, (*COUNTER_NAMES, *SLOW_NAMES, *CONFIG_NAMES)
            )
            records.append(
                self._record(
                    "status_transition",
                    device_id,
                    {**cached, **snapshot},
                    topology,
                    battery_status2_before=previous,
                    battery_status2_after=status,
                )
            )
            if any(_calibrating(value) for value in (previous, status)):
                records.append(
                    self._record(
                        "calibration_window_hint",
                        device_id,
                        {},
                        topology,
                        evidence_only=True,
                        verified=False,
                        basis="community_status2_calibrating_bits",
                        battery_status2_before=previous,
                        battery_status2_after=status,
                    )
                )
            records.extend(
                self._candidates(device_id, snapshot, "status_transition", topology)
            )
        if monotonic >= self._next_counters.get(device_id, monotonic):
            self._next_counters[device_id] = monotonic + COUNTER_INTERVAL_SECONDS
            values = await self._fresh(device_id, COUNTER_NAMES)
            records.append(
                self._record("periodic_counters", device_id, values, topology)
            )
        if monotonic >= self._next_slow.get(device_id, monotonic):
            self._next_slow[device_id] = monotonic + SLOW_INTERVAL_SECONDS
            values = await self._fresh(device_id, SLOW_NAMES)
            records.append(self._record("periodic_slow", device_id, values, topology))
            records.extend(
                self._candidates(device_id, values, "periodic_slow", topology)
            )
        if monotonic >= self._next_config.get(device_id, monotonic):
            self._next_config[device_id] = monotonic + CONFIG_INTERVAL_SECONDS
            values = await self._fresh(device_id, CONFIG_NAMES)
            records.append(self._record("periodic_config", device_id, values, topology))
        await asyncio.to_thread(self._write, records)

    def _candidates(
        self, device_id: str, values: dict, trigger: str, topology: dict
    ) -> list[dict]:
        """Unverified change records between two fresh reads; no meaning is attached to them."""
        records = []
        for name in CANDIDATE_NAMES:
            after = values.get(name)
            if after is None or after["status"] != "fresh" or after["value"] is None:
                continue  # a gap is no change: compare only measured values
            before = self._last_candidates.get((device_id, name))
            self._last_candidates[(device_id, name)] = after
            if before is None or before["value"] == after["value"]:
                continue
            records.append(
                self._record(
                    "candidate_change",
                    device_id,
                    {},
                    topology,
                    evidence_only=True,
                    verified=False,
                    register=name,
                    trigger=trigger,
                    before=before,
                    after=after,
                )
            )
        return records

    def _topology(self, device_id: str) -> dict:
        """The settled module inventory as the service already holds it; never a device rescan."""
        if self._capacity is None:
            return {}
        counts = getattr(self._capacity, "module_counts", None)
        settling = getattr(self._capacity, "is_settling", None)
        modules = counts(device_id) if counts is not None else None
        return {
            "module_counts": list(modules) if modules is not None else None,
            "rated_usable_capacity_wh": self._capacity.verified_usable_capacity_wh(
                device_id
            ),
            "topology_settling": settling(device_id) if settling is not None else None,
        }

    def _cached(self, device_id: str, name: str) -> dict:
        if not self._name_exists(name):
            return {"value": None, "status": "not_in_catalog"}
        found = self._gateway.cached_entry(device_id, name)
        if found is None:
            return {"value": None, "status": "missing"}
        entry, freshness = found
        return {
            "value": _plain(entry.value),
            "status": "fresh" if freshness is CacheFreshness.FRESH else "stale",
            "age_seconds": round(
                max(0.0, self._clock.monotonic() - entry.received_monotonic), 3
            ),
            # The same cached measurement keeps these two fields in every record that repeats it.
            "sample_monotonic": round(entry.received_monotonic, 3),
            "sample_measured_at": entry.measured_at.astimezone(UTC).isoformat(),
        }

    async def _fresh(self, device_id: str, names: Sequence[str]) -> dict:
        values: dict[str, dict] = {}
        for name in names:
            if not self._name_exists(name):
                values[name] = {"value": None, "status": "not_in_catalog"}
                continue
            status = self._gateway.device_status(device_id)
            # The device queue is shared with live reads: wait for an idle, reachable device.
            if status.state not in READY_STATES or status.queue_length:
                values[name] = {"value": None, "status": "skipped_busy"}
                continue
            try:
                reading = await self._gateway.read_system(device_id, name)
            except DeviceApiError as exc:
                values[name] = {"value": None, "status": "error", "error": exc.code}
                continue
            except Exception as exc:
                # One register must not cost the records already gathered in this tick.
                if (device_id, name) not in self._failing:
                    log.exception(
                        "Battery diagnostics read of %s failed on %s", name, device_id
                    )
                self._failing.add((device_id, name))
                values[name] = {
                    "value": None,
                    "status": "error",
                    "error": type(exc).__name__,
                }
                continue
            self._failing.discard((device_id, name))
            values[name] = {
                "value": _plain(reading.value),
                "status": "fresh",
                "measured_at": reading.measured_at.astimezone(UTC).isoformat(),
            }
        return values

    def _record(
        self, kind: str, device_id: str, values: dict, topology: dict, **extra
    ) -> dict:
        bms = values.get(BMS_NAME)
        if bms is None or bms.get("value") is None:
            bms = self._cached(device_id, BMS_NAME)
        return {
            "type": kind,
            "timestamp": self._clock.now().astimezone(UTC).isoformat(),
            "monotonic": round(self._clock.monotonic(), 3),
            "device_id": device_id,
            "inverter_serial": self._gateway.reported_serial(device_id),
            "battery_bms_sn": bms.get("value"),
            "device_state": self._gateway.device_status(device_id).state.value,
            **topology,
            **extra,
            "values": values,
        }

    def _write(self, records: list[dict]) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            today = self._clock.now().astimezone(UTC).date()
            # A record belongs to the day of its own timestamp, even when the tick crossed midnight.
            by_day: dict[str, list[dict]] = {}
            for record in records:
                by_day.setdefault(record["timestamp"][:10], []).append(record)
            for day, day_records in by_day.items():
                path = self._directory / f"{FILE_PREFIX}{day}.jsonl"
                with path.open("a", encoding="utf-8") as handle:
                    for record in day_records:
                        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            # A new day, or a late record of a finished one, needs packing and pruning.
            if self._housekept != today or min(by_day) < today.isoformat():
                self._housekeep(today)
                self._housekept = today
            self._write_failed = False
        except OSError:
            # Log the first failure of a run only; the recorder keeps trying every cycle.
            if not self._write_failed:
                log.exception(
                    "Battery diagnostics could not be written to %s", self._directory
                )
            self._write_failed = True

    def _housekeep(self, today: date) -> None:
        """Compress finished days and drop files beyond the retention window."""
        cutoff = today - timedelta(days=RETENTION_DAYS)
        for path in self._directory.glob(f"{FILE_PREFIX}*.jsonl*"):
            stem = path.name.removeprefix(FILE_PREFIX).split(".", 1)[0]
            try:
                day = date.fromisoformat(stem)
            except ValueError:
                continue  # not one of ours: leave it alone
            if day < cutoff:
                path.unlink(missing_ok=True)
            elif day < today and path.suffix == ".jsonl":
                packed = path.with_name(path.name + ".gz")
                # Append as a further gzip member: a late record of an already packed day survives.
                with path.open("rb") as source, gzip.open(packed, "ab") as target:
                    shutil.copyfileobj(source, target)
                path.unlink()
