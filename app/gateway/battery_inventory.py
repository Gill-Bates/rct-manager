#!/usr/bin/env python3
#
# app/gateway/battery_inventory.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Battery tower topology and conservative capacity availability from cached readings.

Cache-only: nothing here reads a device. The admin dashboard and planning source use the
module-serial classification. The gateway drops last-known-good topology when inventory changes.
"""

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

# Prefixes of the per-tower module_sn string slots (app/catalog/objects.json). Each prefix is one
# physical battery tower that could share the inverter. The registry carries entries for both
# prefixes unconditionally, so catalog presence alone cannot tell whether a given tower actually
# exists on this device; _battery_tower_present answers that from readings.
_BATTERY_TOWER_PREFIXES = ("battery", "battery_placeholder_0")
CAPACITY_AH_METRICS = ("battery_ah_capacity", "power_mng_amp_hours_measured")
# BMS serial of the battery; a changed value marks a swapped battery in the calibration history.
BMS_SERIAL_METRIC = "battery_bms_sn"
CAPACITY_AH_FRESH_SECONDS = 2 * 24 * 60 * 60

# RCT_MAX_MODULES_PER_TOWER is hardware (2 to 6 modules per tower, per RCT docs).
# RCT_MODULE_SN_SLOTS is the protocol array size (indices 0-6), not an installable module count.
# The device has no module-count register; counting and hardware validation stay separate, see _battery_module_report.
RCT_MAX_MODULES_PER_TOWER = 6
RCT_MODULE_SN_SLOTS = 7
RCT_RATED_USABLE_WH_PER_MODULE = 1920 * 0.9
# Every populated serial slot is one battery module: a Power Battery 11.5 (6 modules per the RCT
# datasheet) reports exactly six populated slots, and the BMS serial lives in its own register.

# A plain numeric/status reading alone does not prove the *placeholder* tower is real: a device
# answers a periodically-registered read with some value even for a register nothing is wired to
# (an unpopulated numeric register reads back as 0, not as "no reading"), so soc/temperature/
# cycles/status2 are only a presence signal for the primary tower, which every single-battery
# device has always had (keeps compatibility with registries that carry no module_sn slots at
# all). A placeholder tower is only real when it has at least one populated module serial — the
# one signal a device cannot answer for a slot nothing is plugged into.
_BATTERY_PRESENCE_SUFFIXES = ("soc", "temperature", "cycles", "status2")


def _battery_tower_present(
    runtime, device_id: str, prefix: str, trusted_module_slots: list[int]
) -> bool:
    """`trusted_module_slots` must be the STABILIZED report's populated_module_slots (see
    _battery_module_report), never the current poll's raw _battery_populated_module_slots() - a
    transient missing/incomplete serial read must not make an established second tower vanish.
    """
    if trusted_module_slots:
        return True
    if prefix != "battery":
        return False  # a placeholder tower with no module serials is not installed
    for suffix in _BATTERY_PRESENCE_SUFFIXES:
        name = f"{prefix}_{suffix}"
        if (
            runtime.catalog.exists(name)
            and runtime.gateway.cached_reading(device_id, name) is not None
        ):
            return True
    return False


def _clean_module_serial(value: str) -> str:
    """Strip control characters (not just whitespace) that an unpopulated t_string register can
    decode to on real hardware - e.g. a slot full of non-NUL control bytes that str.strip() leaves
    untouched because strip() only removes whitespace by default. What is left is what a real
    serial number actually looks like: printable characters."""
    return re.sub(r"[\x00-\x1f\x7f]", "", value).strip()


# A slot's cached_reading() answer distinguishes three states, not two. cached_reading() returns
# None for a slot that has never been read (or whose cache entry expired) - that is UNKNOWN, not
# "empty". Only a slot that WAS read and decoded to a blank/garbage string is EMPTY. The periodic
# loop fills all seven module_sn slots over several poll cycles (see _battery_module_report's
# docstring), so a mid-scan snapshot has some slots at one of these states and the rest still
# UNKNOWN; conflating UNKNOWN with EMPTY is exactly what let a 3-of-6-read tower be accepted as a
# genuine 3-module tower before the rest had been read.
_SLOT_UNKNOWN = "unknown"
_SLOT_EMPTY = "empty"
_SLOT_POPULATED = "populated"


def _battery_module_slot_states(runtime, device_id: str, prefix: str) -> list[str]:
    """One of _SLOT_UNKNOWN/_SLOT_EMPTY/_SLOT_POPULATED per protocol slot (index = slot number). A
    slot the catalog does not carry at all can never resolve to anything else, so it is EMPTY
    rather than UNKNOWN - it must not block a registry without module_sn slots from ever being
    judged "complete"."""
    states = []
    run_ended = False
    for i in range(RCT_MODULE_SN_SLOTS):
        name = f"{prefix}_module_sn_{i}"
        if not runtime.catalog.exists(name):
            states.append(_SLOT_EMPTY)
            continue
        reading = runtime.gateway.cached_reading(device_id, name)
        if reading is None:
            # A register the device repeatedly leaves unanswered (e.g. slot 6 on a 6-module tower)
            # is never going to be cached; counting it UNKNOWN would hold "pending" forever.
            read_failed = getattr(runtime.gateway, "read_failed", None)
            unanswered = read_failed is not None and read_failed(device_id, name)
            # The inventory scan stops at the first unanswered slot of a tower (and never reads the
            # placeholder tower's higher slots after an unanswered slot 0), so slots behind it stay
            # unread by design and must not hold the tower "unknown" forever.
            states.append(_SLOT_EMPTY if unanswered or run_ended else _SLOT_UNKNOWN)
            run_ended = run_ended or unanswered
            continue
        value = reading[0]
        if isinstance(value, str) and _clean_module_serial(value):
            states.append(_SLOT_POPULATED)
        else:
            states.append(_SLOT_EMPTY)
    return states


def _battery_populated_module_slots(states: list[str]) -> list[int]:
    """Indices of the slots classified POPULATED in a slot-state snapshot."""
    return [i for i, state in enumerate(states) if state == _SLOT_POPULATED]


def _raw_battery_module_report(states: list[str]) -> dict:
    """Pure classification of one read's slot-state snapshot - no memory of any previous read.

    Completeness gates everything else: while any slot is still UNKNOWN, the snapshot is a
    mid-scan read and no topology may be derived from it at all, no matter how clean the slots that
    HAVE been read look - a 3-module tower mid-scan looks identical to a genuine 3-module tower
    that finished scanning first. Only once every slot has been read (none UNKNOWN left) does the
    populated-slot pattern get interpreted: a contiguous run 0..n-1 with the rest empty is
    unambiguous and is trusted; anything else - a gap in the middle, or more populated slots than
    the documented hardware takes - is a data anomaly and not a taller tower.

    `module_count_status` tells the client which case it is looking at:
      "ok"      - complete snapshot, trusted count, render that many modules
      "pending" - either the snapshot is still incomplete (slots arrive over several poll cycles,
                  the normal startup state), or it is complete but genuinely empty
      "anomaly" - complete snapshot whose populated slots cannot describe a documented tower

    The internal "_complete" key drives `_stabilize_battery_module_report`'s last-known-good hold
    and is stripped before a report reaches a caller outside this module (see
    `_battery_module_report`).
    """
    complete = _SLOT_UNKNOWN not in states
    populated = _battery_populated_module_slots(states)
    if not complete:
        return {
            "module_count": None,
            "module_count_status": "pending",
            "populated_module_slots": populated,
            "_complete": False,
        }
    if not populated:
        return {
            "module_count": None,
            "module_count_status": "pending",
            "populated_module_slots": [],
            "_complete": True,
        }
    contiguous = populated == list(range(len(populated)))
    if contiguous and len(populated) <= RCT_MAX_MODULES_PER_TOWER:
        # A count of 1 is below the documented minimum of 2, but that is a legitimate complete
        # read (e.g. a tower stripped down to one module for service), not a mid-scan artifact -
        # the completeness gate above already ruled the mid-scan case out.
        return {
            "module_count": len(populated),
            "module_count_status": "ok",
            "populated_module_slots": populated,
            "_complete": True,
        }
    return {
        "module_count": None,
        "module_count_status": "anomaly",
        "populated_module_slots": populated,
        "_complete": True,
    }


# A changed module count must be read twice in a row before it replaces the trusted one, because
# cache expiry and staggered refreshes can flip cached_reading() between calls.
_BATTERY_MODULE_STABILITY_READS = 2

_battery_module_lock = threading.Lock()
# Attribute on runtime.gateway that holds the stability state. RctGateway owns and creates it and
# clears it in replace_devices() together with its module inventory, so a re-addressed device never
# inherits the old hardware's trusted topology; a stub gateway without it gets one created lazily.
_BATTERY_MODULE_STATE_ATTR = "module_topology_state"


def _battery_report_signature(report: dict) -> tuple:
    # Completeness is part of the topology: a mid-scan empty snapshot must not equal a finished
    # empty scan, or an early incomplete trusted state is never replaced.
    return (
        report["_complete"],
        report["module_count_status"],
        report["module_count"],
        tuple(report["populated_module_slots"]),
    )


def _stabilize_battery_module_report(
    runtime, device_id: str, prefix: str, raw: dict
) -> dict:
    """Holds the last-known-good topology against two things that must never flip it: a mid-scan
    (incomplete) snapshot, and a single changed-but-complete read that has not repeated yet.

    An incomplete `raw` (`raw["_complete"] is False`) can never become trusted and never even
    starts a candidate - the scan that produced it has not finished, so there is nothing yet to
    debounce. Only a COMPLETE snapshot may start or advance a candidate, and - same as before -
    it must repeat `_BATTERY_MODULE_STABILITY_READS` times before replacing the trusted topology,
    so a single flaky complete-looking read is not enough either.
    """
    key = (device_id, prefix)
    with _battery_module_lock:
        per_gateway = getattr(runtime.gateway, _BATTERY_MODULE_STATE_ATTR, None)
        if per_gateway is None:
            per_gateway = {}
            setattr(runtime.gateway, _BATTERY_MODULE_STATE_ATTR, per_gateway)
        state = per_gateway.get(key)
        if state is None:
            # Nothing established yet - even an incomplete read becomes the initial state, since
            # there is no better answer to show while the first scan is still in progress.
            per_gateway[key] = {"trusted": raw, "candidate": None, "candidate_count": 0}
            return raw
        trusted = state["trusted"]
        if not raw["_complete"]:
            # Mid-scan snapshot: hold last-known-good and drop any in-progress candidate, because
            # the scan behind that candidate has not finished either.
            state["candidate"] = None
            state["candidate_count"] = 0
            return trusted
        if _battery_report_signature(raw) == _battery_report_signature(trusted):
            state["candidate"] = None
            state["candidate_count"] = 0
            return trusted
        if state["candidate"] is not None and _battery_report_signature(
            raw
        ) == _battery_report_signature(state["candidate"]):
            state["candidate_count"] += 1
        else:
            state["candidate"] = raw
            state["candidate_count"] = 1
        if state["candidate_count"] < _BATTERY_MODULE_STABILITY_READS:
            return trusted
        state["trusted"] = raw
        state["candidate"] = None
        state["candidate_count"] = 0
        return raw


_PENDING_LOG_INTERVAL_SECONDS = 60.0
_pending_log_last: dict[tuple[str, str], float] = {}


def _log_pending_slots(device_id: str, prefix: str, states: list[str]) -> None:
    """Rate-limited diagnosis of slots that stay UNKNOWN, i.e. keep the tower on "Detecting modules"."""
    unknown = [i for i, state in enumerate(states) if state == _SLOT_UNKNOWN]
    if not unknown:
        return
    now = time.monotonic()
    key = (device_id, prefix)
    with _battery_module_lock:
        if (
            now - _pending_log_last.get(key, -_PENDING_LOG_INTERVAL_SECONDS)
            < _PENDING_LOG_INTERVAL_SECONDS
        ):
            return
        _pending_log_last[key] = now
    log.debug(
        "%s on %s: module serial slots %s never read yet (states: %s)",
        prefix,
        device_id,
        unknown,
        states,
    )


def _battery_module_report(
    runtime, device_id: str, prefix: str, *, include_complete: bool = False
) -> dict:
    """Trusted module-count report for one tower: captures the current slot-state snapshot (see
    `_battery_module_slot_states`), classifies it (`_raw_battery_module_report`), and runs it
    through the last-known-good stabilizer so neither a mid-scan nor a single flaky complete read
    can override an already-established topology (see `_stabilize_battery_module_report`).
    """
    states = _battery_module_slot_states(runtime, device_id, prefix)
    raw = _raw_battery_module_report(states)
    _log_pending_slots(device_id, prefix, states)
    report = _stabilize_battery_module_report(runtime, device_id, prefix, raw)
    if report is raw and report["module_count_status"] == "anomaly":
        log.warning(
            "%s on %s reports module serials in slots %s: not a contiguous run of at most %d slots, so no "
            "module count is derived from it (catalog carries %d slots; documented hardware takes %d modules)",
            prefix,
            device_id,
            report["populated_module_slots"],
            RCT_MAX_MODULES_PER_TOWER,
            RCT_MODULE_SN_SLOTS,
            RCT_MAX_MODULES_PER_TOWER,
        )
    return (
        report
        if include_complete
        else {k: v for k, v in report.items() if k != "_complete"}
    )


@dataclass(frozen=True, slots=True)
class CapacityCalibrationSample:
    device_id: str
    inverter_serial: str
    measured_at: datetime
    battery_cycles: int | None
    reference_ah: float
    measured_ah: float
    rated_usable_capacity_wh: float
    current_usable_capacity_wh: float
    remaining_capacity_percent: float
    module_counts: tuple[int, ...]
    source_contract: str = "bms_ah_ratio_v1"
    battery_bms_sn: str | None = None


class MeasuredBatteryCapacity:
    """Provide a rated planning capacity from complete module inventory.

    Ah readings cannot establish current capacity or a permanent degradation measurement.
    """

    def __init__(self, runtime) -> None:
        self._runtime = runtime

    def verified_usable_capacity_wh(self, device_id: str) -> float | None:
        counts = self.module_counts(device_id)
        if counts is None:
            return None
        if len(counts) == 1 and 2 <= counts[0] <= RCT_MAX_MODULES_PER_TOWER:
            return counts[0] * RCT_RATED_USABLE_WH_PER_MODULE
        if len(counts) == 2 and counts[0] == counts[1] and counts[0] in (4, 5, 6):
            return sum(counts) * RCT_RATED_USABLE_WH_PER_MODULE
        return None

    def module_counts(self, device_id: str) -> tuple[int, ...] | None:
        """Settled module count per present tower, or ``None`` while it is not known; no device I/O."""
        counts = []
        for prefix in _BATTERY_TOWER_PREFIXES:
            report = _battery_module_report(
                self._runtime, device_id, prefix, include_complete=True
            )
            if not report["_complete"]:
                return None
            if not _battery_tower_present(
                self._runtime, device_id, prefix, report["populated_module_slots"]
            ):
                if prefix == "battery":
                    return None
                continue
            count = report["module_count"]
            if report["module_count_status"] != "ok" or count is None:
                return None
            counts.append(count)
        return tuple(counts)

    def is_settling(self, device_id: str) -> bool:
        """True while the module topology that decides the capacity can still change on its own.

        Read-only: it inspects the stabilizer state without advancing the debounce. A tower is
        settled once its trusted report is complete, no candidate waits for confirmation, and the
        current raw read either matches it or is a mid-scan snapshot the stabilizer ignores.
        Towers are walked in the same order and with the same early exits as
        :meth:`verified_usable_capacity_wh`, so an unsupported but stable topology is settled.
        """
        per_gateway = (
            getattr(self._runtime.gateway, _BATTERY_MODULE_STATE_ATTR, None) or {}
        )
        for prefix in _BATTERY_TOWER_PREFIXES:
            raw = _raw_battery_module_report(
                _battery_module_slot_states(self._runtime, device_id, prefix)
            )
            with _battery_module_lock:
                state = per_gateway.get((device_id, prefix))
                trusted = None if state is None else state["trusted"]
                candidate = None if state is None else state["candidate"]
            if trusted is None or not trusted["_complete"] or candidate is not None:
                return True
            if raw["_complete"] and _battery_report_signature(
                raw
            ) != _battery_report_signature(trusted):
                return True
            if not _battery_tower_present(
                self._runtime, device_id, prefix, trusted["populated_module_slots"]
            ):
                if prefix == "battery":
                    return False
                continue
            if (
                trusted["module_count_status"] != "ok"
                or trusted["module_count"] is None
            ):
                return False
        return False

    def calibration_sample(
        self, device_id: str, measured_at: datetime
    ) -> CapacityCalibrationSample | None:
        """No sample until a proven calibration-completion signal is available."""
        return None
