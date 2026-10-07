#!/usr/bin/env python3
#
# app/admin/store.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Encrypted settings and admin credentials in a local SQLite database."""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import string
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import Settings
from app.security.pat import (
    FileToken,
    generate_pat,
    pat_well_formed,
)

log = logging.getLogger(__name__)
_PASSWORDS = PasswordHasher()
_DUMMY_PASSWORD_HASH = _PASSWORDS.hash("unused-admin-password")
_PASSWORD_ERRORS = (VerificationError, InvalidHashError, ValueError)
SESSION_SECONDS = 12 * 3600
_TOUCH_SECONDS = 60
_UPSERT_SETTING = (
    "INSERT INTO settings(key, encrypted) VALUES (?, ?) "
    "ON CONFLICT(key) DO UPDATE SET encrypted=excluded.encrypted"
)

_BOOTSTRAP_SPECIALS = "!#%+-=?@_"
_BOOTSTRAP_ALPHABET = string.ascii_letters + string.digits


def _bootstrap_password(length: int = 8) -> str:
    """Short one-time password with one special character; it must be replaced at first login."""
    chars = [secrets.choice(_BOOTSTRAP_ALPHABET) for _ in range(length - 1)]
    chars.insert(secrets.randbelow(length), secrets.choice(_BOOTSTRAP_SPECIALS))
    return "".join(chars)


class AdminStore:
    def __init__(self, path: Path, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("HMAC_SECRET must contain at least 32 characters")
        self.path = path
        self._closed = False
        self._last_touch: dict[str, float] = {}
        self._touch_lock = threading.Lock()  # authenticate_token runs in worker threads
        self._touch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pat-last-used")
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"rct-admin-db-v1").derive(secret.encode())
        self._fernet = Fernet(base64.urlsafe_b64encode(key))
        self._hmac_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"rct-admin-auth-v1").derive(secret.encode())

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                os.fchmod(fd, 0o600)  # tighten a database created with a looser umask
        finally:
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        db.execute("PRAGMA synchronous = NORMAL")  # safe with WAL; a power cut loses at most the last commits
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def close(self) -> None:
        """Finish pending last-used writes and fold the WAL into the database; safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self._touch_pool.shutdown(wait=True)
        try:
            with self.connect() as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            log.debug("WAL checkpoint at shutdown failed", exc_info=True)

    def initialize(self) -> str | None:
        """Create schema and return a one-time bootstrap password, if needed."""
        with self.connect() as db:
            # Persistent; readers no longer block the writer, so autosaves and last-used updates can overlap.
            if db.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() != "wal":
                log.warning("SQLite WAL mode is unavailable (network file system?); using the default journal")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("unsupported admin database version")
            if version == 0:
                db.executescript("""
                    CREATE TABLE settings (key TEXT PRIMARY KEY, encrypted BLOB NOT NULL) STRICT;
                    CREATE TABLE admin_user (username TEXT PRIMARY KEY, encrypted BLOB NOT NULL) STRICT;
                    CREATE TABLE sessions (
                        digest TEXT PRIMARY KEY,
                        encrypted BLOB NOT NULL
                    ) STRICT;
                    CREATE TABLE pats (
                        id TEXT PRIMARY KEY,
                        digest TEXT NOT NULL UNIQUE,
                        encrypted BLOB NOT NULL
                    ) STRICT;
                    PRAGMA user_version = 2;
                """)
            if version == 1:
                # Version 1 persisted the whole shipped write allowlist as its default; drop that
                # selection once so the deny-by-default now applied to new databases also holds here.
                db.execute("DELETE FROM settings WHERE key='write_names'")
                db.execute("PRAGMA user_version = 2")
                db.commit()  # the implicit DELETE transaction must end before BEGIN IMMEDIATE below
                log.warning(
                    "Admin database upgraded: the write allowlist was reset to deny-by-default; "
                    "re-enable the required writes in the admin UI."
                )
            db.execute("BEGIN IMMEDIATE")
            check = db.execute("SELECT encrypted FROM settings WHERE key='__key_check__'").fetchone()
            if check:
                self._decode("__key_check__", check["encrypted"])
            else:
                if db.execute("SELECT 1 FROM admin_user").fetchone():
                    raise ValueError("admin database has no key verifier")
                db.execute("INSERT INTO settings(key,encrypted) VALUES (?,?)",
                           ("__key_check__", self._encode("__key_check__", "rct-admin-v1")))
            if db.execute("SELECT 1 FROM admin_user WHERE username='admin'").fetchone():
                return None
            password = _bootstrap_password()
            db.execute(
                "INSERT INTO admin_user(username, encrypted) VALUES ('admin', ?)",
                (self._encode("admin", {"password_hash": _PASSWORDS.hash(password), "must_change": True}),),
            )
            return password

    def _insert_token(self, db: sqlite3.Connection, name: str, role: str,
                      expires_at: datetime | None) -> tuple[dict, str]:
        if not 1 <= len(name) <= 64 or any(ord(char) < 32 for char in name):
            raise ValueError("invalid token name")
        if role not in ("read", "read/write"):
            raise ValueError("invalid token role")
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise ValueError("token expiry must be in the future")
        token = generate_pat()
        record = {
            "id": secrets.token_hex(16), "name": name, "role": role,
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
        }
        if db.execute("SELECT count(*) FROM pats").fetchone()[0] >= 32:
            raise ValueError("at most 32 API tokens are allowed")
        db.execute(
            "INSERT INTO pats(id,digest,encrypted) VALUES (?,?,?)",
            (record["id"], self._pat_digest(hashlib.sha256(token.encode()).hexdigest()),
             self._encode(record["id"], record)),
        )
        return record, token

    def _digest(self, value: str) -> str:
        return hmac.new(self._hmac_key, value.encode(), hashlib.sha256).hexdigest()

    def _pat_digest(self, sha256_hex: str) -> str:
        return self._digest("pat:" + sha256_hex)

    def _encode(self, key: str, value) -> bytes:
        return self._fernet.encrypt(json.dumps({"key": key, "value": value}, separators=(",", ":")).encode())

    def _decode(self, key: str, encrypted: bytes):
        try:
            data = json.loads(self._fernet.decrypt(encrypted))
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise ValueError("admin database cannot be decrypted with HMAC_SECRET") from exc
        if not isinstance(data, dict) or data.get("key") != key:
            raise ValueError("admin database record integrity check failed")
        return data["value"]

    def get(self, key: str):
        with self.connect() as db:
            row = db.execute("SELECT encrypted FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        return self._decode(key, row["encrypted"])

    def put(self, key: str, value) -> None:
        encrypted = self._encode(key, value)
        with self.connect() as db:
            db.execute(_UPSERT_SETTING, (key, encrypted))

    def put_many(self, values: dict[str, object]) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, value in values.items():
                db.execute(_UPSERT_SETTING, (key, self._encode(key, value)))

    def merge_operator_settings(self, current, changes: dict):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT encrypted FROM settings WHERE key='operator_settings'").fetchone()
            saved = self._decode("operator_settings", row["encrypted"]) if row else {}
            merged = {**saved, **changes}
            updated = Settings.model_validate({**current.model_dump(), **merged})
            db.execute(_UPSERT_SETTING, ("operator_settings", self._encode("operator_settings", merged)))
            return updated

    def verify_password(self, username: str, password: str) -> bool:
        if len(password.encode()) > 4096:
            return False
        with self.connect() as db:
            row = db.execute("SELECT encrypted FROM admin_user WHERE username=?", (username,)).fetchone()
        try:
            password_hash = (
                self._decode(username, row["encrypted"])["password_hash"] if row else _DUMMY_PASSWORD_HASH
            )
            verified = _PASSWORDS.verify(password_hash, password)
            return bool(row is not None and verified)
        except (*_PASSWORD_ERRORS, KeyError, TypeError):
            # An unreadable record is a failed login (counted by the caller), never a 500.
            return False

    def change_password(self, current: str, new: str) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT encrypted FROM admin_user WHERE username='admin'").fetchone()
            if row is None:
                return False
            try:
                if not _PASSWORDS.verify(self._decode("admin", row["encrypted"])["password_hash"], current):
                    return False
            except _PASSWORD_ERRORS:
                return False
            hashed = _PASSWORDS.hash(new)
            db.execute("UPDATE admin_user SET encrypted=? WHERE username='admin'",
                       (self._encode("admin", {"password_hash": hashed, "must_change": False}),))
            db.execute("DELETE FROM sessions")
        return True

    def password_change_pending(self) -> bool:
        """True while the admin still uses the generated bootstrap password."""
        with self.connect() as db:
            row = db.execute("SELECT encrypted FROM admin_user WHERE username='admin'").fetchone()
        return row is None or bool(self._decode("admin", row["encrypted"])["must_change"])

    def new_session(self) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
        digest = self._digest(token)
        with self.connect() as db:
            self._purge_sessions(db)
            db.execute(
                "INSERT INTO sessions(digest,encrypted) VALUES (?,?)",
                (digest, self._encode(digest, {"csrf_digest": self._digest(csrf),
                                              "expires_at": int(time.time()) + SESSION_SECONDS})),
            )
        return token, csrf

    def _purge_sessions(self, db: sqlite3.Connection) -> None:
        now = int(time.time())
        for row in db.execute("SELECT digest, encrypted FROM sessions").fetchall():
            try:
                expired = self._decode(row["digest"], row["encrypted"])["expires_at"] <= now
            except ValueError:
                expired = True  # unreadable session rows are useless and are dropped
            if expired:
                db.execute("DELETE FROM sessions WHERE digest=?", (row["digest"],))

    def session(self, token: str | None) -> dict | None:
        if not token or len(token) > 256:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT encrypted FROM sessions WHERE digest=?", (self._digest(token),)
            ).fetchone()
            user = db.execute("SELECT encrypted FROM admin_user WHERE username='admin'").fetchone()
        if row is None or user is None:
            return None
        try:
            payload = self._decode(self._digest(token), row["encrypted"])
            must_change = bool(self._decode("admin", user["encrypted"])["must_change"])
        except ValueError:
            return None  # tampered or swapped records never authenticate
        if payload["expires_at"] <= int(time.time()):
            return None
        return {"must_change_password": must_change, "csrf_digest": payload["csrf_digest"]}

    def verify_csrf(self, session: dict, submitted: str | None) -> bool:
        return bool(submitted and hmac.compare_digest(session["csrf_digest"], self._digest(submitted)))

    def delete_session(self, token: str | None) -> None:
        if token:
            with self.connect() as db:
                db.execute("DELETE FROM sessions WHERE digest=?", (self._digest(token),))

    def list_tokens(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,encrypted FROM pats").fetchall()
        records = [{"last_used_at": None, **self._decode(row["id"], row["encrypted"])} for row in rows]
        return sorted(records, key=lambda row: row["created_at"], reverse=True)

    def create_token(self, name: str, role: str, expires_at: datetime | None) -> tuple[dict, str]:
        with self.connect() as db:
            return self._insert_token(db, name, role, expires_at)

    def delete_token(self, token_id: str) -> bool:
        with self.connect() as db:
            return bool(db.execute("DELETE FROM pats WHERE id=?", (token_id,)).rowcount)

    def authenticate_token(self, token: str) -> FileToken | None:
        if len(token) > 512 or not pat_well_formed(token):
            return None
        sha256 = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("SELECT * FROM pats WHERE digest=?", (self._pat_digest(sha256),)).fetchone()
        if row is None:
            return None
        record = self._decode(row["id"], row["encrypted"])
        if record["expires_at"] and datetime.fromisoformat(record["expires_at"]) <= datetime.now(UTC):
            return None
        self._note_use(record)
        return FileToken(record["id"], record["role"])

    def _note_use(self, record: dict) -> None:
        """Record the last use at most once per interval, off the caller's thread; never raises."""
        try:
            now = time.monotonic()
            with self._touch_lock:
                if now - self._last_touch.get(record["id"], -_TOUCH_SECONDS) < _TOUCH_SECONDS:
                    return
                stored = record.get("last_used_at")
                recent = bool(stored) and (datetime.now(UTC) - datetime.fromisoformat(stored)).total_seconds() < _TOUCH_SECONDS
                self._last_touch[record["id"]] = now
            if not recent:
                self._touch_pool.submit(self._write_last_used, record["id"])
        except Exception:
            log.debug("Could not schedule the last-used update", exc_info=True)

    def _write_last_used(self, token_id: str) -> None:
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT encrypted FROM pats WHERE id=?", (token_id,)).fetchone()
                if row is None:  # revoked meanwhile
                    return
                record = self._decode(token_id, row["encrypted"])
                record["last_used_at"] = datetime.now(UTC).isoformat()
                db.execute("UPDATE pats SET encrypted=? WHERE id=?", (self._encode(token_id, record), token_id))
        except Exception:
            log.debug("Could not store the last-used time of a token", exc_info=True)
