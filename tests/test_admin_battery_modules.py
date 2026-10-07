#!/usr/bin/env python3
#
# tests/test_admin_battery_modules.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Battery module counting and per-tower metric mapping for the dashboard device cards.

The point of these tests is twofold:

1. The catalog carries seven module_sn slots (RCT_MODULE_SN_SLOTS) while the documented hardware
   takes at most six modules per tower (RCT_MAX_MODULES_PER_TOWER). Seven populated slots are
   therefore a data anomaly, never a seven-module tower.
2. A slot that was never read (cached_reading() returns None) is UNKNOWN, not EMPTY. The periodic
   loop fills all seven slots over several poll cycles, so a mid-scan read has some slots UNKNOWN
   and must never be interpreted as a smaller real tower - the completeness gate in
   `_raw_battery_module_report` only derives a topology once every slot has been read, and
   `_stabilize_battery_module_report` holds the previously established (last-known-good) topology
   while a scan is incomplete, exactly the behaviour that stops the dashboard tower from flickering
   between e.g. 3 and 6 modules while the slots are still being filled in.
"""

from types import SimpleNamespace

from app.admin.api import (
    _BATTERY_MODULE_STABILITY_READS,
    RCT_MAX_MODULES_PER_TOWER,
    RCT_MODULE_SN_SLOTS,
    RCT_NON_MODULE_SLOTS,
    _battery_metric_names,
    _battery_module_report,
    _battery_module_slot_states,
    _battery_populated_module_slots,
    _battery_tower_present,
)
from app.cache import CacheFreshness


def _runtime(serials: dict[str, str], *, known: set[str] | None = None):
    """Runtime stub whose catalog knows `known` (default: every battery name used here).

    A slot name present in `serials` is a slot that WAS read (even if its value is blank/garbage -
    "empty", not "unknown"). A slot name absent from `serials` but present in `known`/unrestricted
    catalog existence is a slot that is defined but never read yet - UNKNOWN - matching
    cached_reading()'s real None-means-unread contract.
    """
    names = known if known is not None else None

    def exists(name: str) -> bool:
        return name in names if names is not None else True

    def cached_reading(device_id: str, name: str):
        if name not in serials:
            return None
        return (serials[name], CacheFreshness.FRESH)

    return SimpleNamespace(catalog=SimpleNamespace(exists=exists),
                           gateway=SimpleNamespace(cached_reading=cached_reading))


def _slots(prefix: str, count: int) -> dict[str, str]:
    """A COMPLETE read of a tower with `count` battery modules: count + RCT_NON_MODULE_SLOTS populated
    slots (the base/top part carries a serial too) followed by explicitly-read-empty slots up to
    RCT_MODULE_SN_SLOTS - i.e. a finished scan, not a mid-scan snapshot. Use `_partial_slots` to
    build a snapshot that still has UNKNOWN slots."""
    populated_slots = count + RCT_NON_MODULE_SLOTS
    populated = {f"{prefix}_module_sn_{i}": f"SN-{i}" for i in range(populated_slots)}
    rest_empty = {f"{prefix}_module_sn_{i}": "" for i in range(populated_slots, RCT_MODULE_SN_SLOTS)}
    return {**populated, **rest_empty}


def _partial_slots(prefix: str, count: int) -> dict[str, str]:
    """A read of `count` populated slots with the remaining slots still UNREAD (not in the dict at
    all) - the mid-scan state the periodic loop produces while the other slots have not been
    refreshed yet."""
    return {f"{prefix}_module_sn_{i}": f"SN-{i}" for i in range(count)}


def _report(runtime, prefix: str = "battery") -> dict:
    """Same step devices() performs: capture the slot-state snapshot, then interpret/stabilize it."""
    return _battery_module_report(runtime, "dev", prefix)


def test_catalog_slots_and_hardware_limit_are_separate_numbers() -> None:
    assert RCT_MODULE_SN_SLOTS == 7  # size of the battery_module_sn_0..6 array
    assert RCT_MAX_MODULES_PER_TOWER == 6  # documented modules per tower (RCT Power Battery / BMS V2)
    assert RCT_MODULE_SN_SLOTS > RCT_MAX_MODULES_PER_TOWER


def test_contiguous_run_within_the_hardware_limit_is_trusted() -> None:
    for count in range(1, RCT_MAX_MODULES_PER_TOWER + 1):
        report = _report(_runtime(_slots("battery", count)), "battery")
        assert report["module_count"] == count, count
        assert report["module_count_status"] == "ok"
        assert report["populated_module_slots"] == list(range(count + RCT_NON_MODULE_SLOTS))


def test_no_serial_read_yet_is_pending_not_zero_modules() -> None:
    report = _report(_runtime({}), "battery")
    assert report["module_count"] is None
    assert report["module_count_status"] == "pending"
    assert report["populated_module_slots"] == []


def test_blank_serials_do_not_count_as_populated() -> None:
    # All seven slots READ (completeness gate satisfied): one real serial, the rest read as blank.
    serials = {"battery_module_sn_0": "SN-0", "battery_module_sn_1": "   ", "battery_module_sn_2": "",
               "battery_module_sn_3": "", "battery_module_sn_4": "", "battery_module_sn_5": "",
               "battery_module_sn_6": ""}
    report = _report(_runtime(serials), "battery")
    assert report["module_count"] == 0, "the only populated slot is the base/top part, not a module"
    assert report["module_count_status"] == "ok"


def test_null_and_control_byte_garbage_does_not_count_as_populated() -> None:
    # Real hardware can decode an unpopulated t_string register to garbage that is not an empty
    # string after .strip() - e.g. a run of non-NUL control characters - rather than "". Such a
    # slot must still be treated as unpopulated, not as a real module serial. All seven slots are
    # READ here so the completeness gate does not mask the assertion.
    serials = {
        "battery_module_sn_0": "SN-0",
        "battery_module_sn_1": "\x00\x00\x00\x00",
        "battery_module_sn_2": "\x01\x02\x03",
        "battery_module_sn_3": "", "battery_module_sn_4": "", "battery_module_sn_5": "",
        "battery_module_sn_6": "",
    }
    report = _report(_runtime(serials), "battery")
    assert report["module_count"] == 0, "the only populated slot is the base/top part, not a module"
    assert report["module_count_status"] == "ok"
    assert report["populated_module_slots"] == [0]


def test_all_seven_slots_populated_is_the_six_module_maximum_plus_the_base_part() -> None:
    report = _report(_runtime(_slots("battery", RCT_MAX_MODULES_PER_TOWER)), "battery")
    assert report["module_count"] == RCT_MAX_MODULES_PER_TOWER
    assert report["module_count_status"] == "ok"
    assert report["populated_module_slots"] == list(range(RCT_MODULE_SN_SLOTS))


_GAPPED_SERIALS = {"battery_module_sn_0": "a", "battery_module_sn_1": "b", "battery_module_sn_4": "e",
                   "battery_module_sn_2": "", "battery_module_sn_3": "", "battery_module_sn_5": "",
                   "battery_module_sn_6": ""}


def test_a_gap_in_the_middle_is_an_anomaly() -> None:
    # Complete read (all 7 slots): 0, 1, 4 populated, 2, 3, 5, 6 explicitly read as empty.
    serials = {"battery_module_sn_0": "a", "battery_module_sn_1": "b", "battery_module_sn_4": "e",
               "battery_module_sn_2": "", "battery_module_sn_3": "", "battery_module_sn_5": "",
               "battery_module_sn_6": ""}
    report = _report(_runtime(serials), "battery")
    assert report["module_count"] is None
    assert report["module_count_status"] == "anomaly"
    assert report["populated_module_slots"] == [0, 1, 4]


def test_a_populated_slot_six_alone_is_an_anomaly() -> None:
    serials = {"battery_module_sn_6": "x", "battery_module_sn_0": "", "battery_module_sn_1": "",
               "battery_module_sn_2": "", "battery_module_sn_3": "", "battery_module_sn_4": "",
               "battery_module_sn_5": ""}
    report = _report(_runtime(serials), "battery")
    assert report["module_count"] is None
    assert report["module_count_status"] == "anomaly"
    assert report["populated_module_slots"] == [6]


def test_anomalous_slot_pattern_is_logged_as_a_warning(caplog) -> None:
    with caplog.at_level("WARNING"):
        _report(_runtime(_GAPPED_SERIALS), "battery")
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_second_tower_is_counted_from_its_own_slots() -> None:
    serials = {**_slots("battery", 5), **_slots("battery_placeholder_0", 4)}
    runtime = _runtime(serials)
    assert _report(runtime, "battery")["module_count"] == 5
    assert _report(runtime, "battery_placeholder_0")["module_count"] == 4


def test_first_tower_carries_the_device_wide_roles_and_the_second_does_not() -> None:
    # The catalog has battery_cycles and battery_soc_target but no battery_placeholder_0_* equivalent,
    # and power_mng_bat_next_calib_date belongs to the power manager; those roles are therefore
    # reported once instead of being invented for the second tower.
    known = {"battery_soc", "battery_temperature", "battery_status2", "battery_cycles",
             "battery_soc_target", "power_mng_bat_next_calib_date",
             "battery_placeholder_0_soc", "battery_placeholder_0_temperature",
             "battery_placeholder_0_status2"}
    runtime = _runtime({}, known=known)
    first = _battery_metric_names(runtime, "battery", include_device_wide=True)
    second = _battery_metric_names(runtime, "battery_placeholder_0", include_device_wide=False)
    assert first == {"soc": "battery_soc", "temperature": "battery_temperature",
                     "status": "battery_status2", "cycles": "battery_cycles",
                     "soc_target": "battery_soc_target",
                     "next_calibration": "power_mng_bat_next_calib_date"}
    assert second == {"soc": "battery_placeholder_0_soc",
                      "temperature": "battery_placeholder_0_temperature",
                      "status": "battery_placeholder_0_status2"}
    # No shared name leaks into the second tower: that was the two-identical-towers bug.
    assert not set(second.values()) & set(first.values())


def test_placeholder_tower_needs_a_module_serial_but_the_primary_one_does_not() -> None:
    # A device answers a periodic read for an unwired register with a value, so soc/temperature alone
    # cannot prove a second tower exists; a populated module serial can. Presence is decided from the
    # TRUSTED (stabilized) report's populated_module_slots, same as devices() does.
    readings = {"battery_soc": "0.5", "battery_placeholder_0_soc": "0.0"}
    runtime = _runtime(readings)
    primary_slots = _battery_populated_module_slots(_battery_module_slot_states(runtime, "dev", "battery"))
    placeholder_slots = _battery_populated_module_slots(
        _battery_module_slot_states(runtime, "dev", "battery_placeholder_0"))
    assert _battery_tower_present(runtime, "dev", "battery", primary_slots) is True
    assert _battery_tower_present(runtime, "dev", "battery_placeholder_0", placeholder_slots) is False
    with_serial = _runtime({**readings, **_slots("battery_placeholder_0", 4)})
    trusted = _report(with_serial, "battery_placeholder_0")
    assert _battery_tower_present(with_serial, "dev", "battery_placeholder_0",
                                   trusted["populated_module_slots"]) is True


def test_second_tower_does_not_vanish_on_a_single_transient_missing_serial() -> None:
    # The scenario the user-reported bug names explicitly: an established second tower must not
    # disappear just because one poll's serial read for it came back empty/incomplete.
    serials = {**_slots("battery", 5), **_slots("battery_placeholder_0", 4)}
    runtime = _runtime(serials)
    trusted = _report(runtime, "battery_placeholder_0")
    assert _battery_tower_present(runtime, "dev", "battery_placeholder_0",
                                   trusted["populated_module_slots"]) is True
    # One poll loses every placeholder serial (transient read failure/mid-refresh).
    for i in range(RCT_MODULE_SN_SLOTS):
        serials.pop(f"battery_placeholder_0_module_sn_{i}", None)
    flaky = _report(runtime, "battery_placeholder_0")
    assert flaky["module_count"] == 4, "last-known-good must be held, not dropped to pending"
    assert _battery_tower_present(runtime, "dev", "battery_placeholder_0",
                                   flaky["populated_module_slots"]) is True, \
        "the second tower card must not vanish on a transient missing serial"


def test_metric_names_skip_roles_the_catalog_does_not_carry() -> None:
    runtime = _runtime({}, known={"battery_soc"})
    assert _battery_metric_names(runtime, "battery", include_device_wide=True) == {"soc": "battery_soc"}


# --- Completeness gate -------------------------------------------------------------------------
#
# This is the actual root-cause fix: a mid-scan snapshot (some slots UNKNOWN, i.e. never read -
# not "read as empty") must never be interpreted as a smaller real tower, no matter how clean the
# slots that HAVE been read look.


def test_an_unread_slot_is_unknown_not_empty() -> None:
    states = _battery_module_slot_states(_runtime({"battery_module_sn_0": "SN-0"}), "dev", "battery")
    assert states[0] == "populated"
    assert states[1] == "unknown"  # never read - cached_reading() returned None
    assert all(state == "unknown" for state in states[2:])


def test_a_slot_the_catalog_does_not_carry_is_empty_not_unknown() -> None:
    # A registry without module_sn slots at all must still reach "complete" eventually (keeps
    # compatibility with older/simpler registries), so a slot the catalog never defines is EMPTY,
    # not UNKNOWN - it can never be read and must not block completeness forever.
    states = _battery_module_slot_states(_runtime({}, known=set()), "dev", "battery")
    assert all(state == "empty" for state in states)


def test_a_partial_scan_reports_pending_not_a_smaller_real_count() -> None:
    # Three of seven slots read as populated, the rest never read yet - this is the exact "3 of 6
    # modules present so far" mid-scan snapshot that used to be wrongly accepted as a real
    # 3-module tower. It must come back pending/None, never module_count == 3.
    runtime = _runtime(_partial_slots("battery", 3))
    report = _report(runtime, "battery")
    assert report["module_count"] is None
    assert report["module_count_status"] == "pending"


def test_an_established_topology_is_held_while_a_later_scan_is_incomplete() -> None:
    # Full lifecycle: establish 6 modules from a complete snapshot, then simulate the scan
    # restarting (e.g. after a reconnect) with only 3 of the 7 slots re-read so far. The reported
    # topology must stay 6, never drop to 3 or to pending, until a new COMPLETE snapshot arrives.
    serials = _slots("battery", 6)
    runtime = _runtime(serials)
    trusted = _report(runtime, "battery")
    assert trusted["module_count"] == 6
    # Same gateway INSTANCE (so the stability state carries over) but a swapped-in reader that
    # actually answers from the mid-scan dict (slots 3..6 now UNKNOWN) - replacing the whole
    # gateway object would also replace cached_reading's closure and silently keep re-reading the
    # original complete snapshot, making this assertion pass even with the hold branch deleted.
    mid_scan = _partial_slots("battery", 3)  # slots 3..6 now UNKNOWN
    runtime.gateway.cached_reading = _runtime(mid_scan).gateway.cached_reading
    # Poll the SAME incomplete snapshot _BATTERY_MODULE_STABILITY_READS+1 times: without the
    # dedicated incomplete-read hold, an incomplete-but-identical-looking read would still repeat
    # enough times to pass the ordinary candidate/stability debounce and get promoted to trusted,
    # which must never happen for a read that was never complete.
    for _ in range(_BATTERY_MODULE_STABILITY_READS + 1):
        held = _report(runtime, "battery")
        assert held["module_count"] == 6, "an incomplete re-scan must hold the established topology"
        assert held["module_count_status"] == "ok"


def test_a_single_complete_but_different_read_is_not_enough_either() -> None:
    # Completeness alone does not bypass the stability debounce: a complete snapshot that differs
    # from the trusted one must still repeat _BATTERY_MODULE_STABILITY_READS times.
    runtime = _runtime(_slots("battery", 6))
    assert _report(runtime, "battery")["module_count"] == 6
    runtime.gateway.cached_reading = _runtime(_slots("battery", 3)).gateway.cached_reading
    once = _report(runtime, "battery")
    assert once["module_count"] == 6, "one complete-but-different read must not override immediately"


# --- Stability across polls -------------------------------------------------------------------
#
# The underlying periodic loop fills the seven module_sn slots over several read/refresh cycles
# (see _battery_module_report's docstring), so cached_reading() can answer differently for the same
# physically unchanging tower from one /admin/api/devices call to the next - some slots mid-refresh,
# some still at their previous value. A single read that looks different from the last trusted one
# must not immediately flip module_count; it has to repeat before it is believed.
#
# These tests share one `runtime` (and so one `runtime.gateway`) across several `_report` calls -
# unlike the tests above, which create a fresh runtime per call - because the stability state lives
# on the gateway instance and only shows its effect across repeated calls against the same one.


def test_a_single_flaky_read_does_not_flip_an_already_trusted_count() -> None:
    serials = _slots("battery", 5)
    runtime = _runtime(serials)
    first = _report(runtime, "battery")
    assert first["module_count"] == 5 and first["module_count_status"] == "ok"
    # One poll catches the loop mid-refresh: slot 3 has not been re-read yet, so it is UNKNOWN
    # again (not "read as empty") and this single poll's snapshot is incomplete, held by the
    # completeness gate rather than debounced as a changed-but-complete reading.
    del serials["battery_module_sn_3"]
    flaky = _report(runtime, "battery")
    assert flaky["module_count"] == 5, "an incomplete re-read must hold the trusted count"
    assert flaky["module_count_status"] == "ok"
    assert flaky["populated_module_slots"] == [0, 1, 2, 3, 4, 5]
    # The next poll sees the real (unchanged) hardware again; the trusted count is unaffected by the
    # one flaky read in between.
    serials["battery_module_sn_3"] = "SN-3"
    recovered = _report(runtime, "battery")
    assert recovered["module_count"] == 5
    assert recovered["module_count_status"] == "ok"


def test_alternating_reads_never_settle_on_either_alternative() -> None:
    # A tower whose raw read keeps flip-flopping between two readings, poll after poll, must keep
    # reporting the originally trusted count rather than settling on whichever reading happened last -
    # that would just be flicker with extra steps.
    serials = _slots("battery", 6)
    runtime = _runtime(serials)
    trusted = _report(runtime, "battery")
    assert trusted["module_count"] == 6
    for _ in range(6):
        del serials["battery_module_sn_5"]  # makes slot 5 UNKNOWN again -> incomplete, held
        report = _report(runtime, "battery")
        assert report["module_count"] == 6, "must not flip to the alternative reading"
        serials["battery_module_sn_5"] = "SN-5"
        report = _report(runtime, "battery")
        assert report["module_count"] == 6


def test_a_sustained_new_reading_eventually_updates_the_trusted_count() -> None:
    # A real hardware change (a module actually added) looks identical, at the protocol level, to a
    # read that happens to repeat: _BATTERY_MODULE_STABILITY_READS consecutive matching reads of the
    # new value is what tells the two apart, so a genuine change must still take effect and not be
    # frozen out forever.
    serials = _slots("battery", 4)
    runtime = _runtime(serials)
    assert _report(runtime, "battery")["module_count"] == 4
    serials.update(_slots("battery", 5))
    for i in range(_BATTERY_MODULE_STABILITY_READS):
        report = _report(runtime, "battery")
        if i < _BATTERY_MODULE_STABILITY_READS - 1:
            assert report["module_count"] == 4, "must not update before the new reading is confirmed"
    assert report["module_count"] == 5, "a sustained new reading must eventually be trusted"
    # And it stays trusted afterwards, without needing to keep re-confirming it.
    assert _report(runtime, "battery")["module_count"] == 5


def test_the_very_first_read_is_trusted_immediately_with_no_history_to_compare_against() -> None:
    # No prior poll exists yet (startup), so there is nothing to debounce against: the first read
    # for a given tower is reported as-is.
    runtime = _runtime(_slots("battery", 3))
    assert _report(runtime, "battery")["module_count"] == 3


def test_towers_and_devices_each_keep_their_own_stability_state() -> None:
    # One tower flapping must not affect the trusted state of a different tower or device, and vice
    # versa; the debounce key is (device_id, prefix), not just the gateway.
    serials = {**_slots("battery", 5), **_slots("battery_placeholder_0", 3)}
    runtime = _runtime(serials)
    assert _report(runtime, "battery")["module_count"] == 5
    assert _report(runtime, "battery_placeholder_0")["module_count"] == 3
    del serials["battery_module_sn_4"]  # flaky read on "battery" only
    flaky = _report(runtime, "battery")
    assert flaky["module_count"] == 5
    assert _report(runtime, "battery_placeholder_0")["module_count"] == 3


def test_anomaly_is_only_logged_once_the_transition_is_actually_confirmed(caplog) -> None:
    # A tower trusted as "ok" that then reads back as anomalous once must not log a warning yet -
    # same debounce as the module count itself, see test_a_single_flaky_read_does_not_flip_an_
    # already_trusted_count - and must log it once the anomalous reading repeats enough to be
    # believed.
    serials = _slots("battery", 4)
    runtime = _runtime(serials)
    assert _report(runtime, "battery")["module_count"] == 4
    serials["battery_module_sn_6"] = "x"  # turns the read into a gap anomaly: 0,1,2,3,6
    with caplog.at_level("WARNING"):
        unconfirmed = _report(runtime, "battery")
    assert unconfirmed["module_count"] == 4, "still the trusted count while the anomaly is unconfirmed"
    assert not any(record.levelname == "WARNING" for record in caplog.records), \
        "must not warn about an anomaly that has not been confirmed yet"
    caplog.clear()
    with caplog.at_level("WARNING"):
        confirmed = _report(runtime, "battery")
    assert confirmed["module_count"] is None
    assert confirmed["module_count_status"] == "anomaly"
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_slots_the_device_repeatedly_leaves_unanswered_do_not_hold_pending_forever() -> None:
    serials = _partial_slots("battery", 3)
    runtime = _runtime(serials)
    unanswered = {f"battery_module_sn_{i}" for i in range(3, RCT_MODULE_SN_SLOTS)}
    runtime.gateway.read_failed = lambda device_id, name: name in unanswered
    states = _battery_module_slot_states(runtime, "dev", "battery")
    assert states[3:] == ["empty"] * (RCT_MODULE_SN_SLOTS - 3)
    for _ in range(_BATTERY_MODULE_STABILITY_READS):
        report = _report(runtime)
    assert (report["module_count"], report["module_count_status"]) == (2, "ok")


def test_a_slot_not_yet_confirmed_unanswered_still_counts_as_unknown() -> None:
    runtime = _runtime(_partial_slots("battery", 3))
    runtime.gateway.read_failed = lambda device_id, name: False
    assert _battery_module_slot_states(runtime, "dev", "battery")[3] == "unknown"
