#!/usr/bin/env python3
#
# tests/test_energy_store.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Dispatch store energy-mode and SoC-target-policy tables: fresh-schema shape and fail-closed reads.

The baseline schema is created directly (the application is unreleased, so there is no migration).
Both tables fail closed: a row that cannot be decrypted, or whose authenticated blob disagrees with
its cleartext column, makes the device read as mode *off* and its target derivation fall back to the
shipped default, never the other way around.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.dispatch.capabilities import CapabilityName, CapabilityRecord
from app.dispatch.models import DeviceLimits
from app.dispatch.soc_policy import SocTargetMode, SocTargetPolicy
from app.dispatch.store import DispatchStore
from app.energy.models import EnergyMode, ModeRecord

SECRET = "s" * 48
CHANGED_AT = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)


def test_the_baseline_energy_table_has_mode_and_no_armed_column(tmp_path: Path) -> None:
    """The current energy_manager_state has `mode` and NOT the legacy `armed` column; a reopen is a
    no-op and sets no migration version marker."""
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, SECRET)
    store.initialize()
    store.put_energy_state(ModeRecord("main", mode=EnergyMode.MANUAL, changed_at=CHANGED_AT, changed_by="admin"))
    reopened = DispatchStore(path, SECRET)
    reopened.initialize()  # idempotent: reopening the current schema neither fails nor rewrites
    with reopened.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(energy_manager_state)")}
        assert "mode" in columns
        assert "armed" not in columns
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 0
    assert reopened.get_energy_states()["main"].mode is EnergyMode.MANUAL


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
        assert db.execute("SELECT mode FROM energy_manager_state").fetchone()[0] == "manual"
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
            "INSERT INTO energy_manager_state(device_id,mode,encrypted) VALUES(?,?,?)",
            ("slave1", "manual", blob),
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


def test_energy_mode_cleartext_flipped_against_the_blob_reads_as_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """P2-01: the energy `mode` is authoritative from the authenticated blob. Editing only the
    cleartext column 'off' -> 'manual' (no Fernet key) must not switch the device on; the integrity
    cross-check skips the row, so it reads as mode off."""
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, SECRET)
    store.initialize()
    store.put_energy_state(ModeRecord("main", mode=EnergyMode.OFF, changed_at=CHANGED_AT, changed_by="admin"))
    with store.connect() as db:
        db.execute("UPDATE energy_manager_state SET mode='manual' WHERE device_id='main'")
    with caplog.at_level("ERROR"):
        assert store.get_energy_states() == {}
    assert "reads as mode off" in caplog.text


def test_soc_target_mode_cleartext_flipped_against_the_blob_reads_as_business_target(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """P2-01: the SoC-target policy `mode` is authoritative from the authenticated blob. Editing
    only the cleartext column must not change the derivation policy; the row is skipped and the
    device falls back to the shipped business-target default."""
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, SECRET)
    store.initialize()
    store.put_soc_target_policy(SocTargetPolicy("main", SocTargetMode.BUSINESS_TARGET))
    with store.connect() as db:
        db.execute("UPDATE dispatch_soc_target_policy SET mode='below_current_soc' WHERE device_id='main'")
    with caplog.at_level("ERROR"):
        assert store.get_soc_target_policies() == {}
    assert "business target default" in caplog.text


def test_an_unauthenticated_soc_target_policy_row_reads_as_default_then_reauthenticates(tmp_path: Path) -> None:
    """P2-01: a policy blob with no authenticated `mode` (the cleartext claims a policy the blob
    does not back) is not trusted, so the row reads fail closed (business-target default) until the
    next trusted put_soc_target_policy() re-writes the blob with the mirror."""
    path = tmp_path / "dispatch.db"
    store = DispatchStore(path, SECRET)
    store.initialize()
    legacy_blob = store._encrypt("main", {"below_margin_percent": 5.0, "note": None})
    with store.connect() as db:
        db.execute(
            "INSERT INTO dispatch_soc_target_policy(device_id,mode,encrypted) VALUES(?,?,?)",
            ("main", "below_current_soc", legacy_blob),  # cleartext claims a policy, blob has no mirror
        )
    assert store.get_soc_target_policies() == {}  # fail closed: the unauthenticated claim is not trusted
    store.put_soc_target_policy(SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC))
    assert store.get_soc_target_policies()["main"].mode is SocTargetMode.BELOW_CURRENT_SOC
