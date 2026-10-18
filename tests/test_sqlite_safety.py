#!/usr/bin/env python3
#
# tests/test_sqlite_safety.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Fail-closed SQLite path validation (P2-2): the shared helper both encrypted stores open through.

The admin and dispatch stores resolve the database by path in ``sqlite3.connect``, so a writable
parent directory is a TOCTOU swap vector. The hardening is opt-in via ``RCT_SQLITE_STRICT_PATH``
(so the app still starts from a developer tree or an owner-foreign parent such as ``/opt``); when it
is on, ``validated_sqlite_path`` must refuse an untrusted parent or a non-regular database file
before the open, and accept a private directory with a not-yet-created file.
"""

import os
import stat
from pathlib import Path

import pytest

from app.sqlite_safety import (
    UnsafeDatabasePath,
    open_hardened_sqlite,
    strict_path_enabled,
    validated_sqlite_path,
)


@pytest.fixture
def strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCT_SQLITE_STRICT_PATH", "1")
    assert strict_path_enabled()


def test_strict_mode_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default is pass-through: the hardening only runs when explicitly enabled."""
    monkeypatch.delenv("RCT_SQLITE_STRICT_PATH", raising=False)
    assert strict_path_enabled() is False


def test_without_strict_mode_an_unsafe_parent_is_returned_unchecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off by default: even a world-writable parent is returned unchecked so startup is not blocked."""
    monkeypatch.delenv("RCT_SQLITE_STRICT_PATH", raising=False)
    db_dir = tmp_path / "shared"
    db_dir.mkdir()
    os.chmod(db_dir, 0o777)  # noqa: S103 - deliberately unsafe; must still pass when strict mode is off
    db_path = db_dir / "dispatch.db"
    assert validated_sqlite_path(db_path) == str(db_path)


def test_a_private_directory_with_a_missing_file_is_accepted(tmp_path: Path, strict: None) -> None:
    """A tight, owner-owned directory and a database file that does not exist yet is the normal
    first-start case: connect() creates the file under the validated directory."""
    db_dir = tmp_path / "state"
    db_dir.mkdir(mode=0o700)
    db_path = db_dir / "dispatch.db"
    assert validated_sqlite_path(db_path) == str(db_path)


def test_an_existing_regular_private_file_is_accepted(tmp_path: Path, strict: None) -> None:
    db_dir = tmp_path / "state"
    db_dir.mkdir(mode=0o700)
    db_path = db_dir / "dispatch.db"
    db_path.write_bytes(b"")
    os.chmod(db_path, 0o600)
    assert validated_sqlite_path(db_path) == str(db_path)


def test_a_world_writable_parent_without_the_sticky_bit_is_refused(tmp_path: Path, strict: None) -> None:
    """The core TOCTOU guard: a parent any principal can write to (and that is not sticky) lets the
    final component be swapped between the check and sqlite3.connect, so the open must fail closed."""
    db_dir = tmp_path / "shared"
    db_dir.mkdir(mode=0o777)
    os.chmod(db_dir, 0o777)  # noqa: S103 - a world-writable parent is exactly what the guard rejects
    assert not db_dir.stat().st_mode & stat.S_ISVTX
    with pytest.raises(UnsafeDatabasePath, match="sticky bit"):
        validated_sqlite_path(db_dir / "dispatch.db")


def test_a_world_writable_parent_with_the_sticky_bit_is_accepted(tmp_path: Path, strict: None) -> None:
    """A sticky shared-writable directory (as /tmp) is safe: only a component's owner may rename or
    remove it, so the swap the guard defends against cannot happen."""
    db_dir = tmp_path / "sticky"
    db_dir.mkdir()
    os.chmod(db_dir, 0o1777)  # noqa: S103 - sticky shared-writable dir (as /tmp) must be accepted
    db_path = db_dir / "dispatch.db"
    assert validated_sqlite_path(db_path) == str(db_path)


def test_a_symlinked_database_file_is_refused(tmp_path: Path, strict: None) -> None:
    """The database path must be a regular file; a symlink (the classic redirect) fails closed even
    when its target is otherwise fine."""
    db_dir = tmp_path / "state"
    db_dir.mkdir(mode=0o700)
    target = db_dir / "real.db"
    target.write_bytes(b"")
    link = db_dir / "dispatch.db"
    link.symlink_to(target)
    with pytest.raises(UnsafeDatabasePath, match="not a regular file"):
        validated_sqlite_path(link)


def test_a_group_or_world_writable_database_file_is_refused(tmp_path: Path, strict: None) -> None:
    db_dir = tmp_path / "state"
    db_dir.mkdir(mode=0o700)
    db_path = db_dir / "dispatch.db"
    db_path.write_bytes(b"")
    os.chmod(db_path, 0o666)  # noqa: S103 - a world-writable DB file is exactly what the guard rejects
    with pytest.raises(UnsafeDatabasePath, match="group- or world-writable"):
        validated_sqlite_path(db_path)


def test_open_hardened_sqlite_creates_a_private_db_with_base_pragmas(tmp_path: Path) -> None:
    """The shared open skeleton both stores use: parent 0o700, file 0o600, foreign_keys/busy_timeout
    on. The per-store synchronous policy is set by the caller, not here."""
    db_path = tmp_path / "state" / "store.db"
    db = open_hardened_sqlite(db_path)
    try:
        assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        db.close()


def test_open_hardened_sqlite_tightens_a_loosely_created_file(tmp_path: Path) -> None:
    """A database left over with a loose mode is tightened to 0o600 on open."""
    db_dir = tmp_path / "state"
    db_dir.mkdir(mode=0o700)
    db_path = db_dir / "store.db"
    db_path.write_bytes(b"")
    os.chmod(db_path, 0o644)  # intentionally loose; open_hardened_sqlite must tighten it
    db = open_hardened_sqlite(db_path)
    try:
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    finally:
        db.close()


def test_open_hardened_sqlite_rejects_an_untrusted_parent_in_strict_mode(tmp_path: Path, strict: None) -> None:
    """Strict mode routes the open through validated_sqlite_path, so an untrusted parent fails
    closed before sqlite3.connect for both stores alike."""
    db_dir = tmp_path / "shared"
    db_dir.mkdir(mode=0o777)
    os.chmod(db_dir, 0o777)  # noqa: S103 - a world-writable parent is exactly what the guard rejects
    with pytest.raises(UnsafeDatabasePath):
        open_hardened_sqlite(db_dir / "store.db")
