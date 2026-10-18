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

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.admin.api import (
    SECOND_TOWER_METRIC_NAMES,
    _battery_metric_names,
    second_tower_confirmed,
)
from app.gateway.battery_inventory import (
    _BATTERY_MODULE_STABILITY_READS,
    RCT_MAX_MODULES_PER_TOWER,
    RCT_MODULE_SN_SLOTS,
    MeasuredBatteryCapacity,
    _battery_module_report,
    _battery_module_slot_states,
    _battery_populated_module_slots,
    _battery_tower_present,
)
from app.gateway.cache import CacheFreshness
from app.gateway.rct import REFRESH_FAILURES_CONFIRMED


def _runtime(
    serials: dict[str, str],
    *,
    known: set[str] | None = None,
    unanswered: frozenset[str] = frozenset(),
    ah_samples: dict[str, tuple[float | str, CacheFreshness]] | None = None,
    inverter_serial: str | None = "SN-INV-1",
):
    """Runtime stub whose catalog knows `known` (default: every battery name used here).

    A slot name present in `serials` is a slot that WAS read (even if its value is blank/garbage -
    "empty", not "unknown"). A slot name absent from `serials` but present in `known`/unrestricted
    catalog existence is a slot that is defined but never read yet - UNKNOWN - matching
    cached_reading()'s real None-means-unread contract. A name in `unanswered` is a slot the device
    repeatedly left silent (RctGateway.read_failed), which the inventory scan then never reads past.
    """
    names = known if known is not None else None

    def exists(name: str) -> bool:
        return name in names if names is not None else True

    def cached_reading(device_id: str, name: str):
        if name not in serials:
            return None
        return (serials[name], CacheFreshness.FRESH)

    def cached_sample(device_id: str, name: str):
        sample = (ah_samples or {}).get(name)
        return None if sample is None else (sample[0], 0.0, sample[1])

    return SimpleNamespace(
        catalog=SimpleNamespace(exists=exists),
        gateway=SimpleNamespace(
            cached_reading=cached_reading,
            cached_sample=cached_sample,
            reported_serial=lambda _device_id: inverter_serial,
            read_failed=lambda _device_id, name: name in unanswered,
        ),
    )


def _slots(prefix: str, count: int) -> dict[str, str]:
    """A COMPLETE read of a tower with `count` battery modules: count populated
    slots followed by explicitly-read-empty slots up to
    RCT_MODULE_SN_SLOTS - i.e. a finished scan, not a mid-scan snapshot. Use `_partial_slots` to
    build a snapshot that still has UNKNOWN slots."""
    populated_slots = count
    populated = {f"{prefix}_module_sn_{i}": f"SN-{i}" for i in range(populated_slots)}
    rest_empty = {
        f"{prefix}_module_sn_{i}": ""
        for i in range(populated_slots, RCT_MODULE_SN_SLOTS)
    }
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
    assert (
        RCT_MAX_MODULES_PER_TOWER == 6
    )  # documented modules per tower (RCT Power Battery / BMS V2)
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
    # All seven slots READ (completeness gate satisfied): one real serial, the rest read as blank.
    serials = {
        "battery_module_sn_0": "SN-0",
        "battery_module_sn_1": "   ",
        "battery_module_sn_2": "",
        "battery_module_sn_3": "",
        "battery_module_sn_4": "",
        "battery_module_sn_5": "",
        "battery_module_sn_6": "",
    }
    report = _report(_runtime(serials), "battery")
    assert report["module_count"] == 1, "only the one real serial counts as a module"
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
        "battery_module_sn_3": "",
        "battery_module_sn_4": "",
        "battery_module_sn_5": "",
        "battery_module_sn_6": "",
    }
    report = _report(_runtime(serials), "battery")
    assert report["module_count"] == 1, "only the one real serial counts as a module"
    assert report["module_count_status"] == "ok"
    assert report["populated_module_slots"] == [0]


def test_six_populated_slots_are_the_six_module_power_battery_11_5() -> None:
    report = _report(_runtime(_slots("battery", RCT_MAX_MODULES_PER_TOWER)), "battery")
    assert report["module_count"] == RCT_MAX_MODULES_PER_TOWER
    assert report["module_count_status"] == "ok"
    assert report["populated_module_slots"] == list(range(RCT_MAX_MODULES_PER_TOWER))


def test_all_seven_slots_populated_is_an_anomaly() -> None:
    serials = {f"battery_module_sn_{i}": f"SN-{i}" for i in range(RCT_MODULE_SN_SLOTS)}
    report = _report(_runtime(serials), "battery")
    assert report["module_count"] is None
    assert report["module_count_status"] == "anomaly"


_GAPPED_SERIALS = {
    "battery_module_sn_0": "a",
    "battery_module_sn_1": "b",
    "battery_module_sn_4": "e",
    "battery_module_sn_2": "",
    "battery_module_sn_3": "",
    "battery_module_sn_5": "",
    "battery_module_sn_6": "",
}


def test_a_gap_in_the_middle_is_an_anomaly() -> None:
    # Complete read (all 7 slots): 0, 1, 4 populated, 2, 3, 5, 6 explicitly read as empty.
    report = _report(_runtime(_GAPPED_SERIALS), "battery")
    assert report["module_count"] is None
    assert report["module_count_status"] == "anomaly"
    assert report["populated_module_slots"] == [0, 1, 4]


def test_a_populated_slot_six_alone_is_an_anomaly() -> None:
    serials = {
        "battery_module_sn_6": "x",
        "battery_module_sn_0": "",
        "battery_module_sn_1": "",
        "battery_module_sn_2": "",
        "battery_module_sn_3": "",
        "battery_module_sn_4": "",
        "battery_module_sn_5": "",
    }
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
    known = {
        "battery_soc",
        "battery_temperature",
        "battery_status2",
        "battery_cycles",
        "battery_soc_target",
        "power_mng_bat_next_calib_date",
        "battery_placeholder_0_soc",
        "battery_placeholder_0_temperature",
        "battery_placeholder_0_status2",
    }
    runtime = _runtime({}, known=known)
    first = _battery_metric_names(runtime, "battery", include_device_wide=True)
    second = _battery_metric_names(
        runtime, "battery_placeholder_0", include_device_wide=False
    )
    assert first == {
        "soc": "battery_soc",
        "temperature": "battery_temperature",
        "status": "battery_status2",
        "cycles": "battery_cycles",
        "soc_target": "battery_soc_target",
        "next_calibration": "power_mng_bat_next_calib_date",
    }
    assert second == {
        "soc": "battery_placeholder_0_soc",
        "temperature": "battery_placeholder_0_temperature",
        "status": "battery_placeholder_0_status2",
    }
    # No shared name leaks into the second tower: that was the two-identical-towers bug.
    assert not set(second.values()) & set(first.values())


def test_placeholder_tower_needs_a_module_serial_but_the_primary_one_does_not() -> None:
    # A device answers a periodic read for an unwired register with a value, so soc/temperature alone
    # cannot prove a second tower exists; a populated module serial can. Presence is decided from the
    # TRUSTED (stabilized) report's populated_module_slots, same as devices() does.
    readings = {"battery_soc": "0.5", "battery_placeholder_0_soc": "0.0"}
    runtime = _runtime(readings)
    primary_slots = _battery_populated_module_slots(
        _battery_module_slot_states(runtime, "dev", "battery")
    )
    placeholder_slots = _battery_populated_module_slots(
        _battery_module_slot_states(runtime, "dev", "battery_placeholder_0")
    )
    assert _battery_tower_present(runtime, "dev", "battery", primary_slots) is True
    assert (
        _battery_tower_present(
            runtime, "dev", "battery_placeholder_0", placeholder_slots
        )
        is False
    )
    with_serial = _runtime({**readings, **_slots("battery_placeholder_0", 4)})
    trusted = _report(with_serial, "battery_placeholder_0")
    assert (
        _battery_tower_present(
            with_serial,
            "dev",
            "battery_placeholder_0",
            trusted["populated_module_slots"],
        )
        is True
    )


def test_second_tower_does_not_vanish_on_a_single_transient_missing_serial() -> None:
    # The scenario the user-reported bug names explicitly: an established second tower must not
    # disappear just because one poll's serial read for it came back empty/incomplete.
    serials = {**_slots("battery", 5), **_slots("battery_placeholder_0", 4)}
    runtime = _runtime(serials)
    trusted = _report(runtime, "battery_placeholder_0")
    assert (
        _battery_tower_present(
            runtime, "dev", "battery_placeholder_0", trusted["populated_module_slots"]
        )
        is True
    )
    # One poll loses every placeholder serial (transient read failure/mid-refresh).
    for i in range(RCT_MODULE_SN_SLOTS):
        serials.pop(f"battery_placeholder_0_module_sn_{i}", None)
    flaky = _report(runtime, "battery_placeholder_0")
    assert (
        flaky["module_count"] == 4
    ), "last-known-good must be held, not dropped to pending"
    assert (
        _battery_tower_present(
            runtime, "dev", "battery_placeholder_0", flaky["populated_module_slots"]
        )
        is True
    ), "the second tower card must not vanish on a transient missing serial"


def test_metric_names_skip_roles_the_catalog_does_not_carry() -> None:
    runtime = _runtime({}, known={"battery_soc"})
    assert _battery_metric_names(runtime, "battery", include_device_wide=True) == {
        "soc": "battery_soc"
    }


#
# This is the actual root-cause fix: a mid-scan snapshot (some slots UNKNOWN, i.e. never read -
# not "read as empty") must never be interpreted as a smaller real tower, no matter how clean the
# slots that HAVE been read look.


def test_an_unread_slot_is_unknown_not_empty() -> None:
    states = _battery_module_slot_states(
        _runtime({"battery_module_sn_0": "SN-0"}), "dev", "battery"
    )
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
        assert (
            held["module_count"] == 6
        ), "an incomplete re-scan must hold the established topology"
        assert held["module_count_status"] == "ok"


def test_a_single_complete_but_different_read_is_not_enough_either() -> None:
    # Completeness alone does not bypass the stability debounce: a complete snapshot that differs
    # from the trusted one must still repeat _BATTERY_MODULE_STABILITY_READS times.
    runtime = _runtime(_slots("battery", 6))
    assert _report(runtime, "battery")["module_count"] == 6
    runtime.gateway.cached_reading = _runtime(
        _slots("battery", 3)
    ).gateway.cached_reading
    once = _report(runtime, "battery")
    assert (
        once["module_count"] == 6
    ), "one complete-but-different read must not override immediately"


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
    assert (
        flaky["module_count"] == 5
    ), "an incomplete re-read must hold the trusted count"
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
        del serials[
            "battery_module_sn_5"
        ]  # makes slot 5 UNKNOWN again -> incomplete, held
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
            assert (
                report["module_count"] == 4
            ), "must not update before the new reading is confirmed"
    assert (
        report["module_count"] == 5
    ), "a sustained new reading must eventually be trusted"
    # And it stays trusted afterwards, without needing to keep re-confirming it.
    assert _report(runtime, "battery")["module_count"] == 5


def test_the_very_first_read_is_trusted_immediately_with_no_history_to_compare_against() -> (
    None
):
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


def test_anomaly_is_only_logged_once_the_transition_is_actually_confirmed(
    caplog,
) -> None:
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
    assert (
        unconfirmed["module_count"] == 4
    ), "still the trusted count while the anomaly is unconfirmed"
    assert not any(
        record.levelname == "WARNING" for record in caplog.records
    ), "must not warn about an anomaly that has not been confirmed yet"
    caplog.clear()
    with caplog.at_level("WARNING"):
        confirmed = _report(runtime, "battery")
    assert confirmed["module_count"] is None
    assert confirmed["module_count_status"] == "anomaly"
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_slots_the_device_repeatedly_leaves_unanswered_do_not_hold_pending_forever() -> (
    None
):
    serials = _partial_slots("battery", 3)
    runtime = _runtime(serials)
    unanswered = {f"battery_module_sn_{i}" for i in range(3, RCT_MODULE_SN_SLOTS)}
    runtime.gateway.read_failed = lambda device_id, name: name in unanswered
    states = _battery_module_slot_states(runtime, "dev", "battery")
    assert states[3:] == ["empty"] * (RCT_MODULE_SN_SLOTS - 3)
    for _ in range(_BATTERY_MODULE_STABILITY_READS):
        report = _report(runtime)
    assert (report["module_count"], report["module_count_status"]) == (3, "ok")


def test_a_slot_not_yet_confirmed_unanswered_still_counts_as_unknown() -> None:
    runtime = _runtime(_partial_slots("battery", 3))
    runtime.gateway.read_failed = lambda device_id, name: False
    assert _battery_module_slot_states(runtime, "dev", "battery")[3] == "unknown"


# Static module serials are read by the bounded, cached inventory pass.
# (RctGateway.read_inventory), NOT by the connection-destabilising periodic set.
# These tests use the PRODUCTION registry, which carries the module_sn slots the
# tests/fixtures/objects.json fixture does not.

_PROD_REGISTRY = (
    Path(__file__).resolve().parent.parent / "app" / "catalog" / "objects.json"
)


def test_module_serials_are_not_in_the_periodic_read_set() -> None:
    """The whole point of FIX 1: the battery_*module_sn_* slots must no longer be registered for
    periodic reads, where an unpopulated slot's missing response drops the connection. The periodic
    default set drops from ~60 to ~46 per device."""
    from app.api.app_factory import _MODULE_INVENTORY_NAMES, _effective_periodic_names
    from app.catalog.registry import RegistryCatalog
    from app.config import Settings

    settings = Settings(object_registry_path=_PROD_REGISTRY)
    catalog = RegistryCatalog.from_file(_PROD_REGISTRY)
    periodic = _effective_periodic_names(settings, catalog, None)

    assert not [
        n for n in periodic if "module_sn" in n
    ], "module serials must be out of the periodic set"
    # Optional second-tower live metrics join only after the topology confirmed the tower.
    assert not set(SECOND_TOWER_METRIC_NAMES) & set(periodic)
    assert (
        len(periodic) == 43
    )  # 39 preselected numeric + energy/card pins, minus serials and tower 2
    # The inventory names cover both towers' seven slots, exactly what devices() needs.
    assert len(_MODULE_INVENTORY_NAMES) == 2 * RCT_MODULE_SN_SLOTS
    assert set(_MODULE_INVENTORY_NAMES) <= set(catalog.names())


async def test_inventory_read_feeds_devices_module_count_without_the_periodic_set() -> (
    None
):
    """End to end for FIX 1: read_module_inventory populates the cache for the slots the device
    answers and settles the ones it never answers via read_failed(), so admin's devices()
    derivation (_battery_module_report) reports the correct module count for a populated tower - the
    feature that would otherwise regress when the serials leave the periodic set. Serials are
    static, so no clock-advancing re-gate is needed between passes: slots settle once and stay
    settled."""
    from datetime import timedelta

    from app.api.app_factory import _MODULE_INVENTORY_NAMES
    from app.catalog.registry import RegistryCatalog
    from app.config import DeviceEntry
    from app.gateway.cache import MemoryCache
    from app.gateway.rct import DeviceBinding, RctGateway
    from app.scheduling.retry import RetryConfig
    from app.scheduling.serializer import AccessSerializer
    from app.security.allowlist import Allowlist
    from app.transport.endpoint import EndpointConfig, TransportEndpoint
    from tests.conftest import AutoClock
    from tests.fakes import FakeNetwork

    clock = AutoClock()
    catalog = RegistryCatalog.from_file(_PROD_REGISTRY)
    # A 3-module primary tower: slots 0..2 populated (3 modules), 3..6 never
    # answered. The second tower answers nothing at all (no placeholder tower installed).
    answered = {f"battery_module_sn_{i}": f"SN-{i}" for i in range(3)}
    answered_ids = {catalog.object_entry(n).object_id for n in answered}

    def behavior(frame):
        return "respond" if frame.object_id in answered_ids else "ignore"

    net = FakeNetwork(clock, behavior=behavior)
    for name, serial in answered.items():
        net.payloads[catalog.object_entry(name).object_id] = serial.encode("latin-1")

    # A generous TTL+grace: AutoClock advances monotonic time on every internal sleep, so a single
    # pass alone moves the clock far more than any real pass would. The settle-once lifecycle keeps
    # answered serials warm by re-putting them each pass, so the cache lifetime is not what this
    # test asserts; it just has to outlast the AutoClock jumps so the freshly read serials stay
    # readable.
    cache = MemoryCache(100_000, 100_000)
    gateway = RctGateway(
        catalog,
        cache,
        clock,
        Allowlist({}, catalog),
        retry=RetryConfig(response_timeout_seconds=0.01),
    )
    device = DeviceEntry(device_id="main", host="10.0.0.5")
    cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
    endpoint = TransportEndpoint(
        "endpoint-1", device.key.endpoint, cfg, clock, connector=net.connect
    )
    serializer = AccessSerializer(
        endpoint,
        gateway.handler(endpoint),
        queue_max_length=10,
        queue_max_wait_seconds=10,
    )
    serializer.start()
    gateway.add_device(DeviceBinding(device, endpoint, serializer))
    runtime = SimpleNamespace(catalog=catalog, gateway=gateway)
    try:
        # Several passes with NO clock advance: the answered slots are cached on the first pass; the
        # unanswered ones only settle "empty" once read_failed confirms them
        # (REFRESH_FAILURES_CONFIRMED consecutive failures). Once settled, a slot is never read
        # again on a timer - the whole point of the lifecycle correction.
        total_sent = 0
        for _ in range(4):
            total_sent += await gateway.read_module_inventory(
                "main", _MODULE_INVENTORY_NAMES
            )
            for _ in range(_BATTERY_MODULE_STABILITY_READS):
                report = _battery_module_report(runtime, "main", "battery")

        # The answered slots reached the cache (not the periodic path) and are kept warm by the
        # settle-once re-put, so they are still readable after several passes.
        assert gateway.cached_reading("main", "battery_module_sn_0") is not None
        # The never-answered slots are confirmed unanswered, so the snapshot is complete.
        assert gateway.read_failed("main", "battery_module_sn_6")
        # devices()'s derivation now reports the real module count for the populated tower.
        assert report["module_count"] == 3
        assert report["module_count_status"] == "ok"
        # Bounded: a pass never issues more than the two towers' slots.
        assert total_sent <= 4 * 2 * RCT_MODULE_SN_SLOTS
    finally:
        await serializer.stop()


async def test_settled_inventory_slots_are_not_re_read_across_later_loop_ticks() -> (
    None
):
    """Regression for the lifecycle correction: a never-answering slot (a dead tower slot) must NOT
    be re-read once settled, no matter how many inventory-loop ticks run afterwards. This is what
    removes the recurring timeout -> _drop_connection exposure in steady state. Positives stay warm
    and read_failed() stays True for the settled-empty slots; the absent second tower costs at most
    one probe (its first serial slot only)."""
    from datetime import timedelta

    from app.api.app_factory import _MODULE_INVENTORY_NAMES
    from app.catalog.registry import RegistryCatalog
    from app.config import DeviceEntry
    from app.gateway.cache import MemoryCache
    from app.gateway.rct import DeviceBinding, RctGateway
    from app.scheduling.retry import RetryConfig
    from app.scheduling.serializer import AccessSerializer
    from app.security.allowlist import Allowlist
    from app.transport.endpoint import EndpointConfig, TransportEndpoint
    from tests.conftest import AutoClock
    from tests.fakes import FakeNetwork

    clock = AutoClock()
    catalog = RegistryCatalog.from_file(_PROD_REGISTRY)
    # Primary tower answers slots 0..3 (3 modules + base part); 4..6 never answer. The whole second
    # (placeholder) tower never answers.
    answered = {f"battery_module_sn_{i}": f"SN-{i}" for i in range(3)}
    answered_ids = {catalog.object_entry(n).object_id for n in answered}

    def behavior(frame):
        return "respond" if frame.object_id in answered_ids else "ignore"

    net = FakeNetwork(clock, behavior=behavior)
    for name, serial in answered.items():
        net.payloads[catalog.object_entry(name).object_id] = serial.encode("latin-1")

    cache = MemoryCache(100_000, 100_000)
    gateway = RctGateway(
        catalog,
        cache,
        clock,
        Allowlist({}, catalog),
        retry=RetryConfig(response_timeout_seconds=0.01),
    )
    device = DeviceEntry(device_id="main", host="10.0.0.5")
    cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
    endpoint = TransportEndpoint(
        "endpoint-1", device.key.endpoint, cfg, clock, connector=net.connect
    )
    serializer = AccessSerializer(
        endpoint,
        gateway.handler(endpoint),
        queue_max_length=10,
        queue_max_wait_seconds=10,
    )
    serializer.start()
    gateway.add_device(DeviceBinding(device, endpoint, serializer))
    placeholder_sn_0 = catalog.object_entry(
        "battery_placeholder_0_module_sn_0"
    ).object_id
    higher_placeholder_ids = {
        catalog.object_entry(f"battery_placeholder_0_module_sn_{i}").object_id
        for i in range(1, RCT_MODULE_SN_SLOTS)
    }
    try:
        # Drive discovery to the settled state (no clock advance: the lifecycle has no re-gate).
        for _ in range(6):
            await gateway.read_module_inventory("main", _MODULE_INVENTORY_NAMES)
        assert gateway.read_failed(
            "main", "battery_module_sn_6"
        )  # dead slot settled empty
        assert (
            gateway.cached_reading("main", "battery_module_sn_0") is not None
        )  # positive kept warm

        # Baseline the device frames, then run several MORE inventory-loop-equivalent passes.
        frames_before = len(net.frames)
        extra_sent = 0
        for _ in range(5):
            extra_sent += await gateway.read_module_inventory(
                "main", _MODULE_INVENTORY_NAMES
            )
        # No settled slot is probed again: no new device frame, nothing sent.
        assert extra_sent == 0
        assert len(net.frames) == frames_before
        # The settled-empty slot stays settled and the positive stays warm (re-put, no device read).
        assert gateway.read_failed("main", "battery_module_sn_6")
        assert gateway.cached_reading("main", "battery_module_sn_0") is not None
        # The absent second tower cost at most one probed slot: only its first serial slot was ever
        # read (bounded by the confirm-before-empty threshold); the higher placeholder slots never
        # were, so a missing second tower never triggers a full 7-slot scan.
        probed_ids = [frame.object_id for _ts, frame in net.frames]
        assert placeholder_sn_0 in probed_ids
        assert all(oid not in probed_ids for oid in higher_placeholder_ids)
    finally:
        await serializer.stop()


async def test_single_tower_with_a_silent_placeholder_yields_capacity_after_the_real_inventory_scan() -> (
    None
):
    """Regression: the scan reads only placeholder slot 0 and never its higher slots; the capacity
    must not stay unavailable because those slots remain unread."""
    from datetime import timedelta

    from app.api.app_factory import _MODULE_INVENTORY_NAMES
    from app.catalog.registry import RegistryCatalog
    from app.config import DeviceEntry
    from app.gateway.cache import MemoryCache
    from app.gateway.rct import DeviceBinding, RctGateway
    from app.scheduling.retry import RetryConfig
    from app.scheduling.serializer import AccessSerializer
    from app.security.allowlist import Allowlist
    from app.transport.endpoint import EndpointConfig, TransportEndpoint
    from tests.conftest import AutoClock
    from tests.fakes import FakeNetwork

    clock = AutoClock()
    catalog = RegistryCatalog.from_file(_PROD_REGISTRY)
    answered = {f"battery_module_sn_{i}": f"SN-{i}" for i in range(6)}
    answered_ids = {catalog.object_entry(n).object_id for n in answered}
    net = FakeNetwork(
        clock,
        behavior=lambda frame: (
            "respond" if frame.object_id in answered_ids else "ignore"
        ),
    )
    for name, serial in answered.items():
        net.payloads[catalog.object_entry(name).object_id] = serial.encode("latin-1")
    gateway = RctGateway(
        catalog,
        MemoryCache(100_000, 100_000),
        clock,
        Allowlist({}, catalog),
        retry=RetryConfig(response_timeout_seconds=0.01),
    )
    device = DeviceEntry(device_id="main", host="10.0.0.5")
    cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
    endpoint = TransportEndpoint(
        "endpoint-1", device.key.endpoint, cfg, clock, connector=net.connect
    )
    serializer = AccessSerializer(
        endpoint,
        gateway.handler(endpoint),
        queue_max_length=10,
        queue_max_wait_seconds=10,
    )
    serializer.start()
    gateway.add_device(DeviceBinding(device, endpoint, serializer))
    runtime = SimpleNamespace(catalog=catalog, gateway=gateway)
    higher_placeholder_ids = {
        catalog.object_entry(f"battery_placeholder_0_module_sn_{i}").object_id
        for i in range(1, RCT_MODULE_SN_SLOTS)
    }
    try:
        for _ in range(8):
            await gateway.read_module_inventory("main", _MODULE_INVENTORY_NAMES)
        assert gateway.read_failed("main", "battery_placeholder_0_module_sn_0")
        assert not higher_placeholder_ids & {
            frame.object_id for _ts, frame in net.frames
        }
        primary = _battery_module_report(runtime, "main", "battery")
        assert primary["module_count_status"] == "ok" and primary["module_count"] == 6
        second = _battery_module_report(runtime, "main", "battery_placeholder_0")
        assert (
            second["module_count_status"] == "pending"
            and second["populated_module_slots"] == []
        )
        assert not _battery_tower_present(
            runtime, "main", "battery_placeholder_0", second["populated_module_slots"]
        )
    finally:
        await serializer.stop()


def test_second_tower_is_confirmed_only_by_module_serials_not_by_numeric_readings() -> (
    None
):
    """A placeholder soc/temperature value proves nothing (an unwired register reads back as 0)."""
    numeric_only = _runtime(
        {"battery_placeholder_0_soc": 0.0, "battery_placeholder_0_temperature": 0.0}
    )
    assert second_tower_confirmed(numeric_only, "dev-a") is False
    with_serials = _runtime(_slots("battery_placeholder_0", 3))
    assert second_tower_confirmed(with_serials, "dev-b") is True


def test_second_tower_is_not_confirmed_while_the_inventory_scan_is_incomplete() -> None:
    # No serial read yet (mid-scan / not started): nothing is confirmed.
    assert second_tower_confirmed(_runtime({}), "dev-c") is False


async def test_second_tower_metrics_join_the_periodic_set_only_after_confirmation() -> (
    None
):
    from datetime import timedelta

    from app.api.app_factory import _activate_second_tower
    from app.catalog.registry import RegistryCatalog
    from app.config import DeviceKey, EndpointKey
    from app.scheduling.periodic import PeriodicManager
    from app.scheduling.serializer import AccessSerializer
    from app.transport.endpoint import EndpointConfig, TransportEndpoint
    from tests.conftest import AutoClock
    from tests.fakes import FakeNetwork

    clock = AutoClock()
    catalog = RegistryCatalog.from_file(_PROD_REGISTRY)
    tower2_ids = {catalog.object_entry(n).object_id for n in SECOND_TOWER_METRIC_NAMES}
    base_id = catalog.object_entry("battery_soc").object_id
    key = EndpointKey("10.0.0.5", 8899)
    # A single-tower inverter: the second tower's registers are never answered.
    net = FakeNetwork(
        clock, behavior=lambda f: "ignore" if f.object_id in tower2_ids else "respond"
    )
    cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
    endpoint = TransportEndpoint("endpoint-1", key, cfg, clock, connector=net.connect)
    serializer = AccessSerializer(
        endpoint,
        lambda r: endpoint.execute(r),
        queue_max_length=10,
        queue_max_wait_seconds=10,
    )
    serializer.start()
    manager = PeriodicManager(
        endpoint, serializer, DeviceKey(key), [base_id], 30, clock
    )
    try:
        assert await manager.ensure() is True
        epoch = endpoint.connection_epoch
        # No tower 2 in the inventory: nothing is added, nothing is read, the connection stays.
        single = SimpleNamespace(
            catalog=catalog,
            gateway=SimpleNamespace(
                cached_reading=lambda d, n: None, read_failed=lambda d, n: True
            ),
        )
        assert _activate_second_tower(single, manager, "dev-d") is False
        assert await manager.ensure() is True
        assert set(manager.object_ids) == {base_id}
        assert endpoint.connection_epoch == epoch
        assert not tower2_ids & {
            frame.object_id for _t, frame in net.frames
        }  # never sent to the device
        assert net.connects == 1  # no reconnect

        # Tower 2 confirmed via its serials: the metrics are registered by the next ensure().
        net.behavior = lambda f: "respond"
        pair = SimpleNamespace(
            catalog=catalog,
            gateway=SimpleNamespace(
                cached_reading=lambda d, n: (
                    (f"SN-{n}", CacheFreshness.FRESH)
                    if "placeholder_0_module_sn" in n
                    else None
                ),
                read_failed=lambda d, n: False,
            ),
        )
        assert _activate_second_tower(pair, manager, "dev-d") is True
        assert await manager.ensure() is True
        assert tower2_ids <= set(manager.object_ids)
        assert all(manager.is_registered(i) for i in tower2_ids)
    finally:
        await serializer.stop()


def test_a_placeholder_inventory_with_no_populated_serial_does_not_confirm_a_second_tower() -> (
    None
):
    # The gate confirms the second tower from a POPULATED module serial, not merely from the
    # placeholder registers existing and answering. A single-tower device whose placeholder prefix
    # the catalog carries but whose slots all read back blank/garbage (physically no second tower)
    # must stay unconfirmed - the "a transient/partial inventory does not activate" half of FEAT-002.
    blanks = {
        f"battery_placeholder_0_module_sn_{i}": "" for i in range(RCT_MODULE_SN_SLOTS)
    }
    answered_blank = _runtime(blanks)
    report = _report(answered_blank, "battery_placeholder_0")
    assert report["populated_module_slots"] == []
    assert second_tower_confirmed(answered_blank, "dev-blank") is False
    # A scan that has not surfaced any slot yet (nothing read) is likewise unconfirmed.
    assert second_tower_confirmed(_runtime({}), "dev-unread") is False


async def test_a_placeholder_tower_with_no_serial_never_adds_or_reads_the_second_tower_metrics() -> (
    None
):
    # End to end against a real PeriodicManager: while the placeholder inventory shows no populated
    # module serial, _activate_second_tower must refuse - no add_object_ids, no device read of the
    # placeholder registers, and therefore no reconnect that an unanswered optional read would cause.
    from datetime import timedelta

    from app.api.app_factory import _activate_second_tower
    from app.catalog.registry import RegistryCatalog
    from app.config import DeviceKey, EndpointKey
    from app.scheduling.periodic import PeriodicManager
    from app.scheduling.serializer import AccessSerializer
    from app.transport.endpoint import EndpointConfig, TransportEndpoint
    from tests.conftest import AutoClock
    from tests.fakes import FakeNetwork

    clock = AutoClock()
    catalog = RegistryCatalog.from_file(_PROD_REGISTRY)
    tower2_ids = {catalog.object_entry(n).object_id for n in SECOND_TOWER_METRIC_NAMES}
    base_id = catalog.object_entry("battery_soc").object_id
    key = EndpointKey("10.0.0.5", 8899)
    net = FakeNetwork(
        clock, behavior=lambda f: "ignore" if f.object_id in tower2_ids else "respond"
    )
    cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
    endpoint = TransportEndpoint("endpoint-1", key, cfg, clock, connector=net.connect)
    serializer = AccessSerializer(
        endpoint,
        lambda r: endpoint.execute(r),
        queue_max_length=10,
        queue_max_wait_seconds=10,
    )
    serializer.start()
    manager = PeriodicManager(
        endpoint, serializer, DeviceKey(key), [base_id], 30, clock
    )
    try:
        assert await manager.ensure() is True
        epoch = endpoint.connection_epoch
        # The placeholder slots all read back blank (device answers, but no second tower is wired):
        # the stabilized report has no populated slot -> second_tower_confirmed False.
        blank_serials = {
            f"battery_placeholder_0_module_sn_{i}": ""
            for i in range(RCT_MODULE_SN_SLOTS)
        }

        def cached_reading(device_id, name):
            if name in blank_serials:
                return (blank_serials[name], CacheFreshness.FRESH)
            return None

        transient = SimpleNamespace(
            catalog=catalog, gateway=SimpleNamespace(cached_reading=cached_reading)
        )
        assert _activate_second_tower(transient, manager, "dev-blank") is False
        assert await manager.ensure() is True
        assert set(manager.object_ids) == {
            base_id
        }  # the placeholder metrics were NOT added
        assert endpoint.connection_epoch == epoch  # no reconnect
        assert not tower2_ids & {
            frame.object_id for _t, frame in net.frames
        }  # never read from the device
        assert net.connects == 1
    finally:
        await serializer.stop()


# ---- Usable capacity from the trusted module count (RCT Power Battery datasheet) ----------------


# What the inventory scan leaves behind on a single-tower system: the placeholder tower's slot 0
# never answers, so its higher slots are never read at all (not cached, not marked failed).
_PLACEHOLDER_SILENT = frozenset({"battery_placeholder_0_module_sn_0"})


def _single_tower(count: int) -> dict[str, str]:
    """Primary tower with `count` modules; the placeholder tower contributes no reading at all."""
    return _slots("battery", count)


def _measured_capacity(runtime) -> float | None:
    return MeasuredBatteryCapacity(runtime).verified_usable_capacity_wh("dev")


def _ah_samples(
    reference: float = 17.82485008239746,
    measured: float = 14.450748443603516,
    freshness: CacheFreshness = CacheFreshness.FRESH,
):
    return {
        "battery_ah_capacity": (reference, freshness),
        "power_mng_amp_hours_measured": (measured, freshness),
    }


def test_complete_module_count_provides_rated_planning_capacity() -> None:
    runtime = _runtime(_single_tower(6), unanswered=_PLACEHOLDER_SILENT)
    assert _battery_module_report(runtime, "dev", "battery")["module_count"] == 6
    assert _measured_capacity(runtime) == pytest.approx(10_368)


def test_incomplete_or_invalid_module_inventory_has_no_planning_capacity() -> None:
    partial = _runtime(_partial_slots("battery", 6), unanswered=_PLACEHOLDER_SILENT)
    assert _measured_capacity(partial) is None
    gapped = _single_tower(6)
    gapped["battery_module_sn_2"] = ""
    assert _measured_capacity(_runtime(gapped, unanswered=_PLACEHOLDER_SILENT)) is None
    assert (
        _measured_capacity(_runtime(_single_tower(7), unanswered=_PLACEHOLDER_SILENT))
        is None
    )
    assert (
        _measured_capacity(_runtime(_single_tower(1), unanswered=_PLACEHOLDER_SILENT))
        is None
    )


@pytest.mark.parametrize(
    ("reference", "measured"),
    [(25.0, 23.8531), (16.9251, 8.65005)],
)
def test_state_dependent_ah_pairs_do_not_change_rated_capacity(
    reference: float, measured: float
) -> None:
    runtime = _runtime(
        _single_tower(6),
        unanswered=_PLACEHOLDER_SILENT,
        ah_samples=_ah_samples(reference=reference, measured=measured),
    )
    assert _measured_capacity(runtime) == pytest.approx(10_368)
    assert (
        MeasuredBatteryCapacity(runtime).calibration_sample(
            "dev", datetime(2026, 10, 17, tzinfo=UTC)
        )
        is None
    )


def test_planning_capacity_tracks_completed_module_topology() -> None:
    serials = _single_tower(5)
    runtime = _runtime(
        serials, unanswered=_PLACEHOLDER_SILENT, ah_samples=_ah_samples()
    )
    assert _battery_module_report(runtime, "dev", "battery")["module_count"] == 5
    assert _measured_capacity(runtime) == pytest.approx(5 * 1920 * 0.9)
    serials.update(_single_tower(6))  # one complete 6-module read: not yet trusted
    assert _measured_capacity(runtime) == pytest.approx(5 * 1920 * 0.9)
    assert _measured_capacity(runtime) == pytest.approx(6 * 1920 * 0.9)


def test_incomplete_followup_scan_keeps_trusted_planning_capacity() -> None:
    serials = _single_tower(6)
    runtime = _runtime(serials, unanswered=_PLACEHOLDER_SILENT)
    assert _measured_capacity(runtime) == pytest.approx(10_368)
    del serials["battery_module_sn_5"]
    assert _measured_capacity(runtime) == pytest.approx(10_368)


def test_startup_scan_settles_only_once_the_completed_topology_is_trusted() -> None:
    # Startup race: the first capacity call sees a mid-scan snapshot, which becomes the initial
    # trusted state, so the completed scan still needs its confirming read.
    serials = _partial_slots("battery", 3)
    runtime = _runtime(serials, unanswered=_PLACEHOLDER_SILENT)
    capacity = MeasuredBatteryCapacity(runtime)
    assert capacity.verified_usable_capacity_wh("dev") is None
    assert capacity.is_settling("dev") is True
    serials.update(_single_tower(6))
    assert capacity.is_settling("dev") is True
    assert capacity.verified_usable_capacity_wh("dev") is None  # candidate 1 of 2
    # Asking again must not advance the debounce on its own.
    assert capacity.is_settling("dev") is True
    assert capacity.is_settling("dev") is True
    assert capacity.verified_usable_capacity_wh("dev") == pytest.approx(10_368)
    assert capacity.is_settling("dev") is False


def test_stable_unsupported_topology_is_settled_without_capacity() -> None:
    three_and_three = _runtime(
        {**_slots("battery", 3), **_slots("battery_placeholder_0", 3)}
    )
    capacity = MeasuredBatteryCapacity(three_and_three)
    assert capacity.verified_usable_capacity_wh("dev") is None
    assert capacity.is_settling("dev") is False

    gapped = _single_tower(6)
    gapped["battery_module_sn_2"] = ""
    anomaly = MeasuredBatteryCapacity(_runtime(gapped, unanswered=_PLACEHOLDER_SILENT))
    assert anomaly.verified_usable_capacity_wh("dev") is None
    assert anomaly.is_settling("dev") is False


def test_fresh_cycles_and_ah_do_not_prove_a_completed_calibration() -> None:
    readings = {**_ah_samples(), "battery_cycles": (42, CacheFreshness.FRESH)}
    runtime = _runtime(
        _single_tower(6), unanswered=_PLACEHOLDER_SILENT, ah_samples=readings
    )
    sample = MeasuredBatteryCapacity(runtime).calibration_sample(
        "dev", datetime(2026, 10, 16, tzinfo=UTC)
    )
    assert sample is None


def test_fresh_bms_serial_does_not_qualify_ah_pair() -> None:
    at = datetime(2026, 10, 16, tzinfo=UTC)
    readings = {
        **_ah_samples(),
        "battery_bms_sn": ("BMS\x0012345 ", CacheFreshness.FRESH),
    }
    sample = MeasuredBatteryCapacity(
        _runtime(_single_tower(6), unanswered=_PLACEHOLDER_SILENT, ah_samples=readings)
    ).calibration_sample("dev", at)
    assert sample is None
    stale = {**_ah_samples(), "battery_bms_sn": ("BMS12345", CacheFreshness.GRACE)}
    sample = MeasuredBatteryCapacity(
        _runtime(_single_tower(6), unanswered=_PLACEHOLDER_SILENT, ah_samples=stale)
    ).calibration_sample("dev", at)
    assert sample is None


@pytest.mark.parametrize("serial", [None, ""])
def test_calibration_sample_needs_the_inverter_serial_and_never_falls_back_to_device_id(
    serial,
) -> None:
    runtime = _runtime(
        _single_tower(6),
        unanswered=_PLACEHOLDER_SILENT,
        ah_samples=_ah_samples(),
        inverter_serial=serial,
    )
    assert (
        MeasuredBatteryCapacity(runtime).calibration_sample(
            "dev", datetime(2026, 10, 16, tzinfo=UTC)
        )
        is None
    )


def test_bms_ah_cache_remains_fresh_between_daily_background_reads() -> None:
    from app.catalog.registry import RegistryCatalog
    from app.gateway.cache import MemoryCache
    from app.gateway.rct import RctGateway
    from app.security.allowlist import Allowlist
    from tests.conftest import AutoClock

    clock = AutoClock()
    catalog = RegistryCatalog.from_file(_PROD_REGISTRY)
    cache = MemoryCache(10, 10)
    RctGateway(catalog, cache, clock, Allowlist({}, catalog))
    key = ("dev", "power_mng_amp_hours_measured")
    cache.put(
        key,
        14.45,
        measured_at=clock.now(),
        received_monotonic=clock.monotonic(),
        origin="transaction",
    )
    entry = cache.get(key)
    assert entry is not None
    assert (
        cache.classify(key, entry, now_monotonic=clock.monotonic() + 24 * 60 * 60)
        is CacheFreshness.FRESH
    )
    assert (
        cache.classify(key, entry, now_monotonic=clock.monotonic() + 49 * 60 * 60)
        is CacheFreshness.EXPIRED
    )


def test_measured_ah_above_reference_does_not_raise_rated_capacity() -> None:
    runtime = _runtime(
        _single_tower(6),
        unanswered=_PLACEHOLDER_SILENT,
        ah_samples=_ah_samples(reference=15.0, measured=18.0),
    )
    assert _measured_capacity(runtime) == pytest.approx(10_368)


@pytest.mark.parametrize(
    "bad_samples",
    [
        {"battery_ah_capacity": (17.0, CacheFreshness.FRESH)},
        _ah_samples(freshness=CacheFreshness.GRACE),
        _ah_samples(reference=0),
        _ah_samples(measured=float("nan")),
    ],
)
def test_missing_stale_or_invalid_ah_does_not_change_rated_capacity(
    bad_samples,
) -> None:
    runtime = _runtime(
        _single_tower(6), unanswered=_PLACEHOLDER_SILENT, ah_samples=bad_samples
    )
    assert _measured_capacity(runtime) == pytest.approx(10_368)


@pytest.mark.parametrize("failure", [None, "battery_ah_capacity", "battery_bms_sn"])
async def test_background_capacity_refresh_reads_both_metrics_and_isolates_failure(
    failure: str | None,
) -> None:
    from app.api.app_factory import _inventory_loop
    from app.errors import DeviceApiError
    from app.gateway.base import DeviceState

    class StopCycle(Exception):
        pass

    class Clock:
        elapsed = 0.0

        def monotonic(self):
            return self.elapsed

        def now(self):
            return datetime(2026, 10, 16, tzinfo=UTC)

        async def sleep(self, seconds):
            self.elapsed += seconds
            if self.elapsed > 15:
                raise StopCycle

    reads = []

    async def read_system(device_id, name):
        reads.append(name)
        if name == failure:
            raise DeviceApiError("unavailable")

    gateway = SimpleNamespace(
        read_module_inventory=lambda *_args: _done(),
        device_status=lambda _device_id: SimpleNamespace(
            state=DeviceState.OK, queue_length=0
        ),
        read_system=read_system,
    )

    async def _done():
        return 0

    with pytest.raises(StopCycle):
        await _inventory_loop(
            gateway,
            "dev",
            Clock(),
            capacity_ah_names=("battery_ah_capacity", "power_mng_amp_hours_measured"),
            bms_serial_name="battery_bms_sn",
        )
    # Reads remain diagnostic; no background reading writes a calibration point.
    ah_reads = ["battery_ah_capacity", "power_mng_amp_hours_measured"]
    assert reads == (
        ah_reads if failure == "battery_ah_capacity" else [*ah_reads, "battery_bms_sn"]
    )


def test_power_battery_11_5_six_modules_provide_rated_capacity() -> None:
    runtime = _runtime(_single_tower(6), unanswered=_PLACEHOLDER_SILENT)
    report = _battery_module_report(runtime, "dev", "battery")
    assert report["module_count_status"] == "ok" and report["module_count"] == 6
    assert _measured_capacity(runtime) == pytest.approx(10_368)


@pytest.mark.parametrize("modules", [4, 5, 6])
def test_symmetric_two_towers_provide_rated_capacity(
    modules: int,
) -> None:
    serials = {**_slots("battery", modules), **_slots("battery_placeholder_0", modules)}
    runtime = _runtime(serials)
    assert _battery_module_report(runtime, "dev", "battery")["module_count"] == modules
    assert (
        _battery_module_report(runtime, "dev", "battery_placeholder_0")["module_count"]
        == modules
    )
    measured = _runtime(serials, ah_samples=_ah_samples(reference=18.0, measured=18.0))
    assert _measured_capacity(measured) == pytest.approx(2 * modules * 1920 * 0.9)


@pytest.mark.parametrize(("primary", "second"), [(6, 5), (3, 3), (4, 5), (2, 2)])
def test_mixed_or_small_two_towers_remain_diagnostic_only(
    primary: int, second: int
) -> None:
    serials = {**_slots("battery", primary), **_slots("battery_placeholder_0", second)}
    runtime = _runtime(serials)
    assert _battery_module_report(runtime, "dev", "battery")["module_count"] == primary
    assert (
        _battery_module_report(runtime, "dev", "battery_placeholder_0")["module_count"]
        == second
    )
    measured = _runtime(serials, ah_samples=_ah_samples())
    assert _measured_capacity(measured) is None


@pytest.mark.parametrize("modules", range(2, 7))
def test_single_tower_module_range_provides_rated_capacity(modules: int) -> None:
    runtime = _runtime(_single_tower(modules), unanswered=_PLACEHOLDER_SILENT)
    assert _battery_module_report(runtime, "dev", "battery")["module_count"] == modules
    assert _measured_capacity(runtime) == pytest.approx(modules * 1920 * 0.9)


# ---- Live reconfiguration must not carry the old hardware's topology forward ---------------------


def test_replace_devices_drops_the_trusted_battery_topology() -> None:
    from app.catalog.registry import RegistryCatalog
    from app.gateway.cache import MemoryCache
    from app.gateway.rct import RctGateway
    from app.security.allowlist import Allowlist
    from tests.conftest import AutoClock

    clock = AutoClock()
    catalog = RegistryCatalog.from_file(
        Path(__file__).resolve().parent / "fixtures" / "objects.json"
    )
    cache = MemoryCache(30, 30)
    gateway = RctGateway(catalog, cache, clock, Allowlist({}, catalog))
    runtime = SimpleNamespace(
        catalog=SimpleNamespace(exists=lambda _name: True), gateway=gateway
    )

    def scan(serials: dict[str, str]) -> None:
        for name in [
            f"{p}_module_sn_{i}"
            for p in ("battery", "battery_placeholder_0")
            for i in range(RCT_MODULE_SN_SLOTS)
        ]:
            cache.invalidate(("dev", name))
        for name, value in serials.items():
            cache.put(
                ("dev", name),
                value,
                measured_at=clock.now(),
                received_monotonic=clock.monotonic(),
                origin="transaction",
            )

    scan(_single_tower(6))
    gateway._refresh_failures[("dev", "battery_placeholder_0_module_sn_0")] = (
        REFRESH_FAILURES_CONFIRMED
    )
    assert _battery_module_report(runtime, "dev", "battery")["module_count"] == 6

    gateway.replace_devices(
        []
    )  # same device id, re-addressed hardware: nothing may be inherited
    scan(_partial_slots("battery", 3))  # the new device's scan has only just begun

    report = _battery_module_report(runtime, "dev", "battery")
    assert report["module_count"] is None
    assert report["module_count_status"] == "pending"


def test_battery_status2_badge_texts_are_short_and_logical() -> None:
    from app.admin.api import _battery_status2_label as label

    assert label(0) == "Operational"
    assert label(0x400) == label(0x008) == "Calibrating"
    assert label(0x002) == "Syncing"
    assert label(0x100 | 0x800) == "Balancing"  # active implies due
    assert (
        label(0x400 | 0x008) == "Calibrating"
    )  # one text for both calibration directions
    assert label(0x200 | 0x100) == "Balancing · Low voltage"
    assert label(0x1000) == "Status 4096"
    assert label(0x100 | 0x1000) == "Balancing · code 4352"
    # Short enough for a phone card header: no entry exceeds a dozen characters.
    from app.admin.api import _BATTERY_STATUS2_BITS

    assert all(len(text) <= 14 for text in _BATTERY_STATUS2_BITS.values())
