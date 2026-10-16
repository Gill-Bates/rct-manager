"""Permanent local history of BMS usable-capacity calibrations."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.admin.store import AdminStore
from app.gateway.battery_inventory import CapacityCalibrationSample


def _sample(at: datetime, measured_ah: float = 14.0) -> CapacityCalibrationSample:
    return CapacityCalibrationSample(
        device_id="main", measured_at=at, battery_cycles=128, reference_ah=17.0,
        measured_ah=measured_ah, rated_usable_capacity_wh=10368.0,
        current_usable_capacity_wh=10368.0 * measured_ah / 17.0,
        remaining_capacity_percent=100.0 * measured_ah / 17.0,
        module_counts=(6,), source_contract="bms_ah_ratio_v1",
    )


def test_calibration_history_survives_restart_deduplicates_and_ignores_consumption_retention(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    at = datetime(2026, 10, 16, 12, tzinfo=UTC)
    store = AdminStore(path, "x" * 32)
    store.initialize()
    assert store.record_battery_calibration(_sample(at))
    assert not store.record_battery_calibration(_sample(at + timedelta(days=1), 14.0001))
    assert store.record_battery_calibration(_sample(at + timedelta(days=30), 13.5))
    store.prune_consumption_intervals((at + timedelta(days=90)).isoformat())
    store.prune_consumption_daily((at + timedelta(days=90)).date().isoformat())
    store.close()

    reopened = AdminStore(path, "x" * 32)
    reopened.initialize()
    rows = reopened.get_battery_calibration_history("main")
    assert [row["measured_ah"] for row in rows] == [14.0, 13.5]
    assert rows[0]["measured_at"] == at.isoformat()
    assert rows[0]["module_counts"] == [6]
    assert rows[0]["remaining_capacity_percent"] == pytest.approx(82.35)
    assert reopened.get_battery_calibration_history("main", limit=1) == rows[-1:]
    assert reopened.get_battery_calibration_history("other") == []
    reopened.close()


def test_missing_calibration_table_is_added_without_replacing_existing_data(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"
    store = AdminStore(path, "x" * 32)
    store.initialize()
    store.put("example", {"retained": True})
    with store.connect() as db:
        db.execute("DROP TABLE battery_calibration_history")
    store.close()

    reopened = AdminStore(path, "x" * 32)
    reopened.initialize()
    assert reopened.get("example") == {"retained": True}
    assert reopened.get_battery_calibration_history("main") == []
    reopened.close()


def test_calibration_history_rejects_naive_time_and_bounds_reads(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "admin.db", "x" * 32)
    store.initialize()
    with pytest.raises(ValueError, match="timezone-aware"):
        store.record_battery_calibration(replace(_sample(datetime.now(UTC)), measured_at=datetime.now(UTC).replace(tzinfo=None)))
    with pytest.raises(ValueError, match="limit"):
        store.get_battery_calibration_history("main", limit=5001)
    store.close()
