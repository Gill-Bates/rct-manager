#!/usr/bin/env python3
#
# tests/test_energy_store.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Dispatch store schema versions 3 and 4: the Energy Manager's mode and the SoC-target policy.

The upgrade is strictly additive — no existing table and no existing row may be touched — and both
new tables fail closed: a row that cannot be decrypted makes the device read as mode *off* and its
target derivation fall back to the shipped default, never the other way around.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.dispatch.capabilities import CapabilityName, CapabilityRecord
from app.dispatch.models import DeviceLimits
from app.dispatch.soc_policy import SocTargetMode, SocTargetPolicy
from app.dispatch.store import DispatchStore
from app.energy.models import EnergyMode, ModeRecord
from tests.test_dispatch_core import _V1_ROW, version_1_database

SECRET = "s" * 48
CHANGED_AT = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)


def version_2_database(path: Path) -> DispatchStore:
    """A dispatch database exactly as the user_version 2 code left it, with one row per table."""
    store = version_1_database(path, SECRET, _V1_ROW)
    store.initialize()  # the v1 -> current upgrade, then wind the version back to 2
    store.put_device_config("main", DeviceLimits(3000, 5000, engineering_mode=True))
    with store.connect() as db:
        db.executescript(
            "DROP TABLE energy_manager_state; DROP TABLE dispatch_soc_target_policy;"
            " PRAGMA user_version = 2;"
        )
    return store


def test_a_version_2_database_upgrades_to_3_and_keeps_its_rows_verbatim(tmp_path: Path) -> None:
    """AC-17: the two new tables are added; dispatch_operations and dispatch_device_config stay."""
    store = version_2_database(tmp_path / "dispatch.db")
    with store.connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 2
    store.initialize()
    with store.connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 4
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"energy_manager_state", "dispatch_soc_target_policy"} <= tables
        assert db.execute("SELECT record_version FROM dispatch_operations").fetchone()[0] == 7
    loaded = store.get("main")
    assert loaded is not None
    assert loaded.intent is not None and loaded.intent.operation_id == "1f2e3d4c"
    assert loaded.record_version == 7
    assert store.get_device_configs() == {"main": DeviceLimits(3000, 5000, engineering_mode=True)}
    # No mode is set and no policy exists just because the tables now do.
    assert store.get_energy_states() == {}
    assert store.get_soc_target_policies() == {}


def test_a_version_4_database_opens_again_without_upgrading_and_without_raising(tmp_path: Path) -> None:
    """AC-17b: the accepted-version guard knows 4, so the first restart after the upgrade comes up
    with dispatch available instead of refusing the database."""
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, SECRET)
    store.initialize()
    store.put_energy_state(ModeRecord("main", mode=EnergyMode.MANUAL, changed_at=CHANGED_AT, changed_by="admin"))
    reopened = DispatchStore(path, SECRET)
    reopened.initialize()
    with reopened.connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 4
    assert reopened.get_energy_states()["main"].mode is EnergyMode.MANUAL


def test_a_future_schema_version_still_raises(tmp_path: Path) -> None:
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, SECRET)
    store.initialize()
    with store.connect() as db:
        db.execute("PRAGMA user_version = 5")
    with pytest.raises(ValueError, match="unsupported dispatch database version"):
        DispatchStore(path, SECRET).initialize()


def test_energy_state_round_trips_including_added_write_names_and_changed_at(tmp_path: Path) -> None:
    store = DispatchStore(tmp_path / "dispatch.db", SECRET)
    store.initialize()
    record = ModeRecord(
        "main",
        mode=EnergyMode.EXTERNAL,
        added_write_names=("power_mng_soc_strategy", "power_mng_battery_power_extern"),
        changed_at=CHANGED_AT,
        changed_by="admin",
    )
    store.put_energy_state(record)
    store.put_energy_state(ModeRecord("slave1"))
    assert store.get_energy_states() == {"main": record, "slave1": ModeRecord("slave1")}

    # Switching off is an upsert, not a second row, and it keeps what the mode switch contributed on record.
    switched_off = ModeRecord("main", added_write_names=record.added_write_names, changed_at=CHANGED_AT)
    store.put_energy_state(switched_off)
    assert store.get_energy_states()["main"] == switched_off
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM energy_manager_state WHERE device_id='main'").fetchone()[0] == 1


def test_soc_target_policy_round_trips_and_upserts(tmp_path: Path) -> None:
    store = DispatchStore(tmp_path / "dispatch.db", SECRET)
    store.initialize()
    policy = SocTargetPolicy(
        "main",
        SocTargetMode.BELOW_CURRENT_SOC,
        below_margin_percent=7.5,
        note="candidate only; not hardware-verified",
    )
    store.put_soc_target_policy(policy)
    store.put_soc_target_policy(SocTargetPolicy("slave1"))
    assert store.get_soc_target_policies() == {"main": policy, "slave1": SocTargetPolicy("slave1")}
    store.put_soc_target_policy(SocTargetPolicy("main"))
    assert store.get_soc_target_policies()["main"] == SocTargetPolicy("main")


def test_mode_and_policy_stay_readable_without_the_secret(tmp_path: Path) -> None:
    """Which mode a device is in, and how its target is derived, must be visible while the service
    is down or after an HMAC_SECRET rotation. They are state names, not secrets."""
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, SECRET)
    store.initialize()
    store.put_energy_state(ModeRecord("main", mode=EnergyMode.MANUAL, changed_at=CHANGED_AT, changed_by="admin"))
    store.put_soc_target_policy(SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC))
    with DispatchStore(path, "r" * 48).connect() as db:
        assert db.execute("SELECT mode, armed FROM energy_manager_state").fetchone()[:] == ("manual", 1)
        assert db.execute("SELECT mode FROM dispatch_soc_target_policy").fetchone()[0] == "below_current_soc"


def test_a_row_written_under_another_secret_fails_closed_instead_of_defaulting(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The device must read as mode off and fall back to the shipped business-target derivation —
    an unreadable row is never silently taken as an active mode or as an accommodating policy."""
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, SECRET)
    store.initialize()
    store.put_energy_state(ModeRecord("main", mode=EnergyMode.MANUAL, changed_at=CHANGED_AT, changed_by="admin"))
    store.put_soc_target_policy(SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC, note="x"))

    rotated = DispatchStore(path, "r" * 48)
    with caplog.at_level("ERROR"):
        assert rotated.get_energy_states() == {}
        assert rotated.get_soc_target_policies() == {}
    assert "reads as mode off" in caplog.text
    assert "business target default" in caplog.text


def test_an_energy_state_blob_moved_to_another_row_is_rejected(tmp_path: Path) -> None:
    """The payload is bound to the bare device_id, so a copied blob cannot switch a second device on."""
    store = DispatchStore(tmp_path / "dispatch.db", SECRET)
    store.initialize()
    store.put_energy_state(ModeRecord("main", mode=EnergyMode.MANUAL, changed_at=CHANGED_AT, changed_by="admin"))
    with store.connect() as db:
        blob = db.execute("SELECT encrypted FROM energy_manager_state").fetchone()[0]
        db.execute(
            "INSERT INTO energy_manager_state(device_id,armed,mode,encrypted) VALUES(?,?,?,?)",
            ("slave1", 1, "manual", blob),
        )
    states = store.get_energy_states()
    assert set(states) == {"main"}  # the forged row is skipped, so slave1 reads as off


def test_one_unreadable_capability_or_device_config_row_blocks_no_other_row(tmp_path: Path) -> None:
    """A corrupt row must not stop startup for every other battery; it reads as unverified/absent."""
    store = DispatchStore(tmp_path / "dispatch.db", SECRET)
    store.initialize()
    good = CapabilityRecord(device_id="slave1", name=CapabilityName.BATTERY_POWER_SIGN)
    store.put_capabilities([CapabilityRecord(device_id="main", name=CapabilityName.BATTERY_POWER_SIGN), good])
    store.put_device_config("main", DeviceLimits(1000, 2000))
    store.put_device_config("slave1", DeviceLimits(3000, 4000))
    with store.connect() as db:
        db.execute("UPDATE dispatch_capabilities SET encrypted=? WHERE device_id='main'", (b"garbage",))
        db.execute("UPDATE dispatch_device_config SET encrypted=? WHERE device_id='main'", (b"garbage",))
    assert store.get_capabilities() == [good]
    assert list(store.get_device_configs()) == ["slave1"]


def version_3_database(path: Path) -> DispatchStore:
    """A dispatch database as the user_version 3 code left it: `armed` flag, `armed_at/by` keys."""
    store = DispatchStore(path, SECRET)
    store.initialize()
    old_payload = {"added_write_names": ["w1"], "armed_at": CHANGED_AT.isoformat(), "armed_by": "admin"}
    with store.connect() as db:
        db.executescript(
            "DROP TABLE energy_manager_state;"
            " CREATE TABLE energy_manager_state (device_id TEXT PRIMARY KEY,"
            " armed INTEGER NOT NULL CHECK(armed IN (0,1)), encrypted BLOB NOT NULL) STRICT;"
            " PRAGMA user_version = 3;"
        )
        for device_id, armed in (("main", 1), ("slave1", 0)):
            db.execute(
                "INSERT INTO energy_manager_state(device_id,armed,encrypted) VALUES(?,?,?)",
                (device_id, armed, store._encrypt(device_id, old_payload)),
            )
    return store


def test_the_version_3_to_4_upgrade_maps_armed_to_manual_and_keeps_the_old_fields(tmp_path: Path) -> None:
    store = version_3_database(tmp_path / "dispatch.db")
    store.initialize()
    with store.connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 4
        rows = db.execute("SELECT device_id, armed, mode FROM energy_manager_state ORDER BY device_id")
        assert [tuple(row) for row in rows] == [("main", 1, "manual"), ("slave1", 0, "off")]
    states = store.get_energy_states()
    assert states["main"] == ModeRecord(
        "main", EnergyMode.MANUAL, ("w1",), changed_at=CHANGED_AT, changed_by="admin"
    )
    assert states["slave1"].mode is EnergyMode.OFF
    store.initialize()  # idempotent: a second start neither fails nor remaps
    assert store.get_energy_states()["main"].mode is EnergyMode.MANUAL
