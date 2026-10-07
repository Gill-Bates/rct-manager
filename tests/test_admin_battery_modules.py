#!/usr/bin/env python3
#
# tests/test_admin_battery_modules.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Battery module counting and per-tower metric mapping for the dashboard device cards.

The point of these tests is the distinction the card payload has to keep: the catalog carries seven
module_sn slots (RCT_MODULE_SN_SLOTS) while the documented hardware takes at most six modules per
tower (RCT_MAX_MODULES_PER_TOWER). Seven populated slots are therefore a data anomaly, never a
seven-module tower.
"""

from types import SimpleNamespace

from app.admin.api import (
    _BATTERY_MODULE_STABILITY_READS,
    RCT_MAX_MODULES_PER_TOWER,
    RCT_MODULE_SN_SLOTS,
    _battery_metric_names,
    _battery_module_report,
    _battery_populated_module_slots,
    _battery_tower_present,
)
from app.cache import CacheFreshness


def _runtime(serials: dict[str, str], *, known: set[str] | None = None):
    """Runtime stub whose catalog knows `known` (default: every battery name used here)."""
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
    return {f"{prefix}_module_sn_{i}": f"SN-{i}" for i in range(count)}


def _report(runtime, prefix: str = "battery") -> dict:
    """Same two-step devices() performs: collect the populated slots, then interpret them."""
    populated = _battery_populated_module_slots(runtime, "dev", prefix)
    return _battery_module_report(runtime, "dev", prefix, populated)


def test_catalog_slots_and_hardware_limit_are_separate_numbers() -> None:
    assert RCT_MODULE_SN_SLOTS == 7  # size of the battery_module_sn_0..6 array
    assert RCT_MAX_MODULES_PER_TOWER == 6  # documented modules per tower (RCT Power Battery / BMS V2)
    assert RCT_MODULE_SN_SLOTS > RCT_MAX_MODULES_PER_TOWER


def test_contiguous_run_within_the_hardware_limit_is_trusted() -> None:
    for count in range(1, RCT_MAX_MODULES_PER_TOWER + 1):
        report = _report(_runtime(_slots("battery", count)), "battery")
        assert report["module_count"] == count, count
        assert report["module_count_status"] == "ok"
        assert report["populated_module_slots"] == list(range(count))


def test_no_serial_read_yet_is_pending_not_zero_modules() -> None:
    report = _report(_runtime({}), "battery")
    assert report["module_count"] is None
    assert report["module_count_status"] == "pending"
    assert report["populated_module_slots"] == []


def test_blank_serials_do_not_count_as_populated() -> None:
    serials = {"battery_module_sn_0": "SN-0", "battery_module_sn_1": "   ", "battery_module_sn_2": ""}
    report = _report(_runtime(serials), "battery")
    assert report["module_count"] == 1
    assert report["module_count_status"] == "ok"


def test_null_and_control_byte_garbage_does_not_count_as_populated() -> None:
    # Real hardware can decode an unpopulated t_string register to garbage that is not an empty
    # string after .strip() - e.g. a run of non-NUL control characters - rather than "". Such a
    # slot must still be treated as unpopulated, not as a real module serial.
    serials = {
        "battery_module_sn_0": "SN-0",
        "battery_module_sn_1": "\x00\x00\x00\x00",
        "battery_module_sn_2": "\x01\x02\x03",
    }
    report = _report(_runtime(serials), "battery")
    assert report["module_count"] == 1
    assert report["module_count_status"] == "ok"
    assert report["populated_module_slots"] == [0]


def test_all_seven_slots_populated_is_an_anomaly_not_a_seven_module_tower(caplog) -> None:
    report = _report(_runtime(_slots("battery", RCT_MODULE_SN_SLOTS)), "battery")
    assert report["module_count"] is None, "seven populated slots must not be reported as seven modules"
    assert report["module_count_status"] == "anomaly"
    assert report["populated_module_slots"] == list(range(RCT_MODULE_SN_SLOTS))


def test_a_gap_in_the_middle_is_an_anomaly() -> None:
    serials = {"battery_module_sn_0": "a", "battery_module_sn_1": "b", "battery_module_sn_4": "e"}
    report = _report(_runtime(serials), "battery")
    assert report["module_count"] is None
    assert report["module_count_status"] == "anomaly"
    assert report["populated_module_slots"] == [0, 1, 4]


def test_a_populated_slot_six_alone_is_an_anomaly() -> None:
    report = _report(_runtime({"battery_module_sn_6": "x"}), "battery")
    assert report["module_count"] is None
    assert report["module_count_status"] == "anomaly"
    assert report["populated_module_slots"] == [6]


def test_anomalous_slot_pattern_is_logged_as_a_warning(caplog) -> None:
    with caplog.at_level("WARNING"):
        _report(_runtime(_slots("battery", RCT_MODULE_SN_SLOTS)), "battery")
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
    # cannot prove a second tower exists; a populated module serial can.
    readings = {"battery_soc": "0.5", "battery_placeholder_0_soc": "0.0"}
    runtime = _runtime(readings)
    primary_slots = _battery_populated_module_slots(runtime, "dev", "battery")
    placeholder_slots = _battery_populated_module_slots(runtime, "dev", "battery_placeholder_0")
    assert _battery_tower_present(runtime, "dev", "battery", primary_slots) is True
    assert _battery_tower_present(runtime, "dev", "battery_placeholder_0", placeholder_slots) is False
    with_serial = _runtime({**readings, **_slots("battery_placeholder_0", 4)})
    slots = _battery_populated_module_slots(with_serial, "dev", "battery_placeholder_0")
    assert _battery_tower_present(with_serial, "dev", "battery_placeholder_0", slots) is True


def test_metric_names_skip_roles_the_catalog_does_not_carry() -> None:
    runtime = _runtime({}, known={"battery_soc"})
    assert _battery_metric_names(runtime, "battery", include_device_wide=True) == {"soc": "battery_soc"}


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
    # One poll catches the loop mid-refresh: slot 3 has not been re-read yet and looks empty, so the
    # raw read for this single poll would be a gap-anomaly (0, 1, 2, 4) rather than 5 modules.
    del serials["battery_module_sn_3"]
    flaky = _report(runtime, "battery")
    assert flaky["module_count"] == 5, "a single changed read must not override the trusted count yet"
    assert flaky["module_count_status"] == "ok"
    assert flaky["populated_module_slots"] == [0, 1, 2, 3, 4]
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
        del serials["battery_module_sn_5"]
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
