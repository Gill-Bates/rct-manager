#!/usr/bin/env python3
#
# tests/test_battery_calibration_history.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Permanent local history of BMS usable-capacity calibrations, keyed by inverter serial."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import app.admin.store as store_module
from app.admin.store import _LEGACY_BATTERY_CALIBRATION_SQL, AdminStore
from app.gateway.battery_inventory import CapacityCalibrationSample

AT = datetime(2026, 10, 16, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _allow_synthetic_qualified_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        store_module, "_QUALIFIED_BATTERY_CALIBRATION_CONTRACTS",
        frozenset({"test_qualified_calibration"}),
    )


def _sample(
    at: datetime, measured_ah: float = 14.0, serial: str = "SN-A", bms: str | None = None, device_id: str = "main"
) -> CapacityCalibrationSample:
    return CapacityCalibrationSample(
        device_id=device_id, inverter_serial=serial, battery_bms_sn=bms, measured_at=at, battery_cycles=128,
        reference_ah=17.0, measured_ah=measured_ah, rated_usable_capacity_wh=10368.0,
        current_usable_capacity_wh=10368.0 * measured_ah / 17.0,
        remaining_capacity_percent=100.0 * measured_ah / 17.0,
        module_counts=(6,), source_contract="test_qualified_calibration",
    )


def _store(tmp_path: Path) -> AdminStore:
    store = AdminStore(tmp_path / "admin.db", "x" * 32)
    store.initialize()
    return store


def test_calibration_history_survives_restart_deduplicates_and_ignores_consumption_retention(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.record_battery_calibration(_sample(AT))
    assert not store.record_battery_calibration(_sample(AT + timedelta(days=1), 14.0001))
    assert store.record_battery_calibration(_sample(AT + timedelta(days=30), 13.5))
    store.prune_consumption_intervals((AT + timedelta(days=90)).isoformat())
    store.prune_consumption_daily((AT + timedelta(days=90)).date().isoformat())
    store.close()

    reopened = _store(tmp_path)
    history = reopened.get_battery_calibration_history("SN-A")
    rows = history["points"]
    assert [row["measured_ah"] for row in rows] == [14.0, 13.5]
    assert rows[0]["measured_at"] == AT.isoformat()
    assert rows[0]["module_counts"] == [6]
    assert rows[0]["remaining_capacity_percent"] == pytest.approx(82.35)
    assert reopened.get_battery_calibration_history("SN-A", limit=1)["points"] == rows[-1:]
    assert reopened.get_battery_calibration_history("SN-OTHER") == {
        "battery_bms_sn": None, "battery_replaced": False, "points": [],
    }
    reopened.close()


def test_inverters_with_different_serials_keep_separate_series_and_dedupe(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.record_battery_calibration(_sample(AT, 14.0, serial="SN-A", device_id="main"))
    # The same Ah value at the other inverter is not a duplicate, whatever the device_id says.
    assert store.record_battery_calibration(_sample(AT, 14.0, serial="SN-B", device_id="main"))
    assert store.record_battery_calibration(_sample(AT + timedelta(days=1), 12.0, serial="SN-B", device_id="second"))
    assert not store.record_battery_calibration(_sample(AT + timedelta(days=2), 14.0, serial="SN-A", device_id="second"))
    assert [p["measured_ah"] for p in store.get_battery_calibration_history("SN-A")["points"]] == [14.0]
    assert [p["measured_ah"] for p in store.get_battery_calibration_history("SN-B")["points"]] == [14.0, 12.0]
    store.close()


@pytest.mark.parametrize("serial", ["", "  "])
def test_unknown_inverter_serial_is_never_recorded(tmp_path: Path, serial: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="inverter serial"):
        store.record_battery_calibration(_sample(AT, serial=serial))
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM battery_calibration_history").fetchone()[0] == 0
    store.close()


def test_changed_bms_serial_starts_a_new_series_even_with_unchanged_ah(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.record_battery_calibration(_sample(AT, 14.0, bms="BMS-1"))
    assert not store.record_battery_calibration(_sample(AT + timedelta(days=1), 14.0, bms="BMS-1"))
    assert not store.record_battery_calibration(_sample(AT + timedelta(days=2), 14.0, bms=None))  # unknown != swap
    history = store.get_battery_calibration_history("SN-A")
    assert (history["battery_bms_sn"], history["battery_replaced"]) == ("BMS-1", False)

    assert store.record_battery_calibration(_sample(AT + timedelta(days=3), 14.0, bms="BMS-2"))
    assert store.record_battery_calibration(_sample(AT + timedelta(days=4), 16.5, bms="BMS-2"))
    history = store.get_battery_calibration_history("SN-A")
    assert (history["battery_bms_sn"], history["battery_replaced"]) == ("BMS-2", True)
    # Only the present battery's series is returned; the old battery's rows stay stored.
    assert [p["measured_ah"] for p in history["points"]] == [14.0, 16.5]
    assert {p["battery_bms_sn"] for p in history["points"]} == {"BMS-2"}
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM battery_calibration_history").fetchone()[0] == 3
    store.close()


def _create_first_build_database(path: Path, device_id: str = "main") -> None:
    db = sqlite3.connect(path)
    db.executescript(_LEGACY_BATTERY_CALIBRATION_SQL)
    for index, ah in enumerate((14.0, 13.5)):
        db.execute(
            "INSERT INTO battery_calibration_history (device_id, measured_at, battery_cycles, reference_ah, "
            "measured_ah, rated_usable_capacity_wh, current_usable_capacity_wh, module_counts, source_contract) "
            "VALUES (?, ?, 100, 17.0, ?, 10368.0, 8000.0, '[6]', 'bms_ah_ratio_v1')",
            (device_id, (AT + timedelta(days=index)).isoformat(), ah),
        )
    db.commit()
    db.close()


def test_first_build_schema_is_migrated_keeping_rows_and_attaching_the_serial_later(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    store = AdminStore(path, "x" * 32)
    store.initialize()
    store.put("example", {"retained": True})
    with store.connect() as db:
        db.execute("DROP TABLE battery_calibration_history")
    store.close()
    _create_first_build_database(path)

    migrated = AdminStore(path, "x" * 32)
    migrated.initialize()
    migrated.initialize()  # idempotent: a second start finds the current shape
    assert migrated.get("example") == {"retained": True}
    with migrated.connect() as db:
        rows = db.execute("SELECT id, inverter_serial, battery_bms_sn FROM battery_calibration_history ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [(1, None, None), (2, None, None)]
        assert db.execute("SELECT name FROM sqlite_schema WHERE name LIKE '%legacy%'").fetchone() is None
    # Rows without a serial are invisible to the serial-keyed read until the serial is attached.
    assert migrated.get_battery_calibration_history("SN-A")["points"] == []
    assert migrated.attach_inverter_serial("second", "SN-X") == 0  # another device_id's rows stay untouched
    assert migrated.attach_inverter_serial("main", "SN-A") == 2
    assert migrated.get_battery_calibration_history("SN-A")["points"] == []
    # Legacy rows stay on disk but cannot qualify a calibration point.
    assert migrated.record_battery_calibration(_sample(AT + timedelta(days=2), 13.5))
    assert migrated.record_battery_calibration(_sample(AT + timedelta(days=3), 12.0))
    migrated.close()


def test_recording_attaches_the_serial_to_rows_of_the_first_build(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    store = AdminStore(path, "x" * 32)
    store.initialize()
    with store.connect() as db:
        db.execute("DROP TABLE battery_calibration_history")
    store.close()
    _create_first_build_database(path)

    migrated = AdminStore(path, "x" * 32)
    migrated.initialize()
    assert migrated.record_battery_calibration(_sample(AT + timedelta(days=5), 13.5))
    assert [p["measured_ah"] for p in migrated.get_battery_calibration_history("SN-A")["points"]] == [13.5]
    migrated.close()


def test_commissioning_date_is_kept_per_serial_and_manual_wins(tmp_path: Path) -> None:
    store = _store(tmp_path)
    derived, manual = datetime(2021, 5, 1, tzinfo=UTC), datetime(2021, 6, 15, tzinfo=UTC)
    assert store.get_inverter_commissioning("SN-A") is None
    store.set_inverter_commissioning("SN-A", derived, "logger_derived")
    assert store.get_inverter_commissioning("SN-A")["source"] == "logger_derived"
    store.set_inverter_commissioning("SN-A", manual, "manual")
    effective = store.get_inverter_commissioning("SN-A")
    assert (effective["source"], effective["commissioned_at"]) == ("manual", manual.isoformat())
    assert store.get_inverter_commissioning("SN-B") is None
    assert store.delete_inverter_commissioning("SN-A", "manual")
    assert not store.delete_inverter_commissioning("SN-A", "manual")
    effective = store.get_inverter_commissioning("SN-A")
    assert (effective["source"], effective["commissioned_at"]) == ("logger_derived", derived.isoformat())
    with pytest.raises(ValueError, match="source"):
        store.set_inverter_commissioning("SN-A", derived, "guess")
    with pytest.raises(ValueError, match="timezone-aware"):
        store.set_inverter_commissioning("SN-A", derived.replace(tzinfo=None), "manual")
    store.close()


def test_missing_calibration_tables_are_added_without_replacing_existing_data(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("example", {"retained": True})
    with store.connect() as db:
        db.execute("DROP TABLE battery_calibration_history")
        db.execute("DROP TABLE inverter_commissioning")
    store.close()

    reopened = _store(tmp_path)
    assert reopened.get("example") == {"retained": True}
    assert reopened.get_battery_calibration_history("SN-A")["points"] == []
    assert reopened.get_inverter_commissioning("SN-A") is None
    reopened.close()


def test_calibration_history_rejects_naive_time_and_bounds_reads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.record_battery_calibration(replace(_sample(AT), measured_at=AT.replace(tzinfo=None)))
    with pytest.raises(ValueError, match="limit"):
        store.get_battery_calibration_history("SN-A", limit=5001)
    store.close()
