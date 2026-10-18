#!/usr/bin/env python3
#
# app/sqlite_safety.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Fail-closed path validation for the local SQLite database files.

The encrypted admin and dispatch stores open a SQLite file by path with ``sqlite3.connect(path)``.
A path-based open is a time-of-check/time-of-use hazard: validating a file descriptor obtained
earlier (``os.open(..., O_NOFOLLOW)``) says nothing about the path SQLite itself resolves a moment
later, because a principal with write access to a parent directory can swap the final component
(symlink, rename) in between. Protecting the descriptor cannot close that window; protecting the
directory the path resolves through can.

``validated_sqlite_path`` validates the containing directory chain and the file fail closed and
returns the path only when the resolution is trustworthy:

* every directory from the database's parent up to the filesystem root must be owned by this
  process' user (or root) and must not be group- or world-writable, so no other principal can
  rename or replace a component of the path, and
* if the database file already exists it must be a regular file (not a symlink, FIFO, device, or
  directory) owned by this process' user (or root) and not group- or world-writable.

Any violation raises ``UnsafeDatabasePath`` and the caller must refuse to open the database rather
than fall back to a less safe location. See the OWASP File Upload and Injection Prevention cheat
sheets for the underlying directory-trust reasoning.
"""

import os
import stat
from pathlib import Path

# Group- and world-writable bits: a path component carrying either lets a principal other than the
# owner replace it, which is what makes the symlink/rename swap possible. On a directory the
# sticky bit (S_ISVTX) negates this: with it set, only a component's owner may rename or remove
# it, so a shared-writable sticky directory such as /tmp is not swap-exploitable.
_UNSAFE_WRITE_BITS = stat.S_IWGRP | stat.S_IWOTH


class UnsafeDatabasePath(Exception):
    """The directory or file a SQLite database would be opened from is not trustworthy.

    Raised fail closed before ``sqlite3.connect``: an owner-foreign or group/world-writable parent,
    or a database path that is not a regular file, must stop the open rather than be worked around.
    """


def _is_owner_trusted(file_stat: os.stat_result) -> bool:
    """Whether ``file_stat`` is owned by this process' effective user or by root.

    root (uid 0) is accepted because a root-owned parent of a service directory is the normal case
    for a system-managed install; any other owner could tamper with the path.
    """
    return file_stat.st_uid in (os.geteuid(), 0)


def _ensure_trusted_directory(directory: Path) -> None:
    """Fail closed unless every directory from ``directory`` up to the root is a trusted directory.

    A single group- or world-writable or owner-foreign component anywhere on the path is enough for
    another principal to swap the final file in, so the whole chain is checked, not only the
    immediate parent.
    """
    current = directory.resolve()
    while True:
        try:
            dir_stat = os.stat(current)
        except OSError as exc:
            raise UnsafeDatabasePath(f"database directory {current} cannot be inspected: {exc}") from exc
        if not stat.S_ISDIR(dir_stat.st_mode):
            raise UnsafeDatabasePath(f"database path component {current} is not a directory")
        if not _is_owner_trusted(dir_stat):
            raise UnsafeDatabasePath(f"database directory {current} is owned by an untrusted user")
        if dir_stat.st_mode & _UNSAFE_WRITE_BITS and not dir_stat.st_mode & stat.S_ISVTX:
            raise UnsafeDatabasePath(f"database directory {current} is group- or world-writable without the sticky bit")
        parent = current.parent
        if parent == current:  # reached the filesystem root
            return
        current = parent


def _ensure_trusted_file(path: Path) -> None:
    """Fail closed unless an existing ``path`` is a regular, owner-trusted, non-shared-writable file.

    A missing file is fine — ``connect()`` creates it under the already-validated directory with a
    tight mode. A present symlink, FIFO, device node, or directory, a foreign owner, or a group/
    world-writable mode means the target is not the private database it must be.
    """
    try:
        file_stat = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UnsafeDatabasePath(f"database file {path} cannot be inspected: {exc}") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise UnsafeDatabasePath(f"database file {path} is not a regular file")
    if not _is_owner_trusted(file_stat):
        raise UnsafeDatabasePath(f"database file {path} is owned by an untrusted user")
    if file_stat.st_mode & _UNSAFE_WRITE_BITS:
        raise UnsafeDatabasePath(f"database file {path} is group- or world-writable")


def validated_sqlite_path(path: Path) -> str:
    """Validate ``path`` fail closed and return it as a string for ``sqlite3.connect``.

    Call this immediately before opening so the directory-trust check and the SQLite open are as
    close together as possible. It creates nothing and performs no write; the caller stays
    responsible for creating the parent directory with a tight mode first. Raises
    ``UnsafeDatabasePath`` on any untrusted parent directory or non-regular existing file.
    """
    _ensure_trusted_directory(path.parent)
    _ensure_trusted_file(path)
    return str(path)
