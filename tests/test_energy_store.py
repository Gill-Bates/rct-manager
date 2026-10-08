#!/usr/bin/env python3
#
# tests/test_energy_store.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Dispatch store schema version 3: the Energy Manager's armed state and the SoC-target policy.

The upgrade is strictly additive — no existing table and no existing row may be touched — and both
new tables fail closed: a row that cannot be decrypted makes the device read as *not armed* and its
target derivation fall back to the shipped default, never the other way around.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.dispatch.capabilities import CapabilityName, CapabilityRecord
from app.dispatch.models import DeviceLimits
from app.dispatch.soc_policy import SocTargetMode, SocTargetPolicy
from app.dispatch.store import DispatchStore
from app.energy.models import ArmedRecord
from tests.test_dispatch_core import _V1_ROW, version_1_database

SECRET = "s" * 48
ARMED_AT = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)


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
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 3
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"energy_manager_state", "dispatch_soc_target_policy"} <= tables
        assert db.execute("SELECT record_version FROM dispatch_operations").fetchone()[0] == 7
    loaded = store.get("main")
    assert loaded is not None
    assert loaded.intent is not None and loaded.intent.operation_id == "1f2e3d4c"
    assert loaded.record_version == 7
    assert store.get_device_configs() == {"main": DeviceLimits(3000, 5000, engineering_mode=True)}
    # Nothing is armed and no policy exists just because the tables now do.
    assert store.get_energy_states() == {}
    assert store.get_soc_target_policies() == {}


def test_a_version_3_database_opens_again_without_upgrading_and_without_raising(tmp_path: Path) -> None:
    """AC-17b: the accepted-version guard knows 3, so the first restart after the upgrade comes up
    with dispatch available instead of refusing the database."""
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, SECRET)
    store.initialize()
    store.put_energy_state(ArmedRecord("main", armed=True, armed_at=ARMED_AT, armed_by="admin"))
    reopened = DispatchStore(path, SECRET)
    reopened.initialize()
    with reopened.connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 3
    assert reopened.get_energy_states()["main"].armed is True


def test_a_future_schema_version_still_raises(tmp_path: Path) -> None:
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, SECRET)
    store.initialize()
    with store.connect() as db:
        db.execute("PRAGMA user_version = 4")
    with pytest.raises(ValueError, match="unsupported dispatch database version"):
        DispatchStore(path, SECRET).initialize()


def test_energy_state_round_trips_including_added_write_names_and_armed_at(tmp_path: Path) -> None:
    store = DispatchStore(tmp_path / "dispatch.db", SECRET)
    store.initialize()
    record = ArmedRecord(
        "main",
        armed=True,
        added_write_names=("power_mng_soc_strategy", "power_mng_battery_power_extern"),
        armed_at=ARMED_AT,
        armed_by="admin",
    )
    store.put_energy_state(record)
    store.put_energy_state(ArmedRecord("slave1"))
    assert store.get_energy_states() == {"main": record, "slave1": ArmedRecord("slave1")}

    # Disarming is an upsert, not a second row, and it keeps what arming contributed on record.
    disarmed = ArmedRecord("main", armed=False, added_write_names=record.added_write_names, armed_at=ARMED_AT)
    store.put_energy_state(disarmed)
    assert store.get_energy_states()["main"] == disarmed
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


def test_armed_and_mode_stay_readable_without_the_secret(tmp_path: Path) -> None:
    """Whether a device is armed, and how its target is derived, must be visible while the service
    is down or after an HMAC_SECRET rotation. They are state names, not secrets."""
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, SECRET)
    store.initialize()
    store.put_energy_state(ArmedRecord("main", armed=True, armed_at=ARMED_AT, armed_by="admin"))
    store.put_soc_target_policy(SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC))
    with DispatchStore(path, "r" * 48).connect() as db:
        assert db.execute("SELECT armed FROM energy_manager_state").fetchone()[0] == 1
        assert db.execute("SELECT mode FROM dispatch_soc_target_policy").fetchone()[0] == "below_current_soc"


def test_a_row_written_under_another_secret_fails_closed_instead_of_defaulting(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The device must read as NOT armed and fall back to the shipped business-target derivation —
    an unreadable row is never silently taken as "armed" or as an accommodating policy."""
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, SECRET)
    store.initialize()
    store.put_energy_state(ArmedRecord("main", armed=True, armed_at=ARMED_AT, armed_by="admin"))
    store.put_soc_target_policy(SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC, note="x"))

    rotated = DispatchStore(path, "r" * 48)
    with caplog.at_level("ERROR"):
        assert rotated.get_energy_states() == {}
        assert rotated.get_soc_target_policies() == {}
    assert "not armed" in caplog.text
    assert "business target default" in caplog.text


def test_an_energy_state_blob_moved_to_another_row_is_rejected(tmp_path: Path) -> None:
    """The payload is bound to the bare device_id, so a copied blob cannot arm a second device."""
    store = DispatchStore(tmp_path / "dispatch.db", SECRET)
    store.initialize()
    store.put_energy_state(ArmedRecord("main", armed=True, armed_at=ARMED_AT, armed_by="admin"))
    with store.connect() as db:
        blob = db.execute("SELECT encrypted FROM energy_manager_state").fetchone()[0]
        db.execute(
            "INSERT INTO energy_manager_state(device_id,armed,encrypted) VALUES(?,?,?)",
            ("slave1", 1, blob),
        )
    states = store.get_energy_states()
    assert set(states) == {"main"}  # the forged row is skipped, so slave1 reads as not armed


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
