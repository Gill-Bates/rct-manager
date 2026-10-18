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
import math
import os
import secrets
import sqlite3
import stat
import string
import threading
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import Settings, TokenRole
from app.security.pat import (
    FileToken,
    generate_pat,
    pat_well_formed,
)

log = logging.getLogger(__name__)
_PASSWORDS = PasswordHasher()
_DUMMY_PASSWORD_HASH = _PASSWORDS.hash("unused-admin-password")
_PASSWORD_ERRORS = (VerificationError, InvalidHashError, ValueError)
_DEFAULT_SESSION_DAYS = 1
_TOUCH_SECONDS = 60
_UPSERT_SETTING = (
    "INSERT INTO settings(key, encrypted) VALUES (?, ?) "
    "ON CONFLICT(key) DO UPDATE SET encrypted=excluded.encrypted"
)

_BOOTSTRAP_SPECIALS = "!#%+-=?@_"
_BOOTSTRAP_ALPHABET = string.ascii_letters + string.digits

# No hardware-proven completion signal exists yet. Legacy bms_ah_ratio_v1 rows stay on disk,
# but cannot become an effective calibration history or seed a 100 % baseline.
_QUALIFIED_BATTERY_CALIBRATION_CONTRACTS: frozenset[str] = frozenset()

# The current baseline admin schema, created directly (the application is unreleased: no deployed
# database to migrate). Keyed by table name so initialize() can create a table only when absent and
# otherwise verify its on-disk definition against the current shape.
_BATTERY_CALIBRATION_SQL = (
    "CREATE TABLE battery_calibration_history ("
    "id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, inverter_serial TEXT, battery_bms_sn TEXT, "
    "measured_at TEXT NOT NULL, "
    "battery_cycles INTEGER CHECK (battery_cycles >= 0), "
    "reference_ah REAL NOT NULL CHECK (reference_ah > 0), "
    "measured_ah REAL NOT NULL CHECK (measured_ah > 0), "
    "rated_usable_capacity_wh REAL NOT NULL CHECK (rated_usable_capacity_wh > 0), "
    "current_usable_capacity_wh REAL NOT NULL CHECK (current_usable_capacity_wh > 0), "
    "module_counts TEXT NOT NULL, source_contract TEXT NOT NULL, "
    "UNIQUE (inverter_serial, measured_at)) STRICT;"
)
# Shape written by the first calibration-history build (keyed by device_id); migrated, never dropped.
_LEGACY_BATTERY_CALIBRATION_SQL = (
    "CREATE TABLE battery_calibration_history ("
    "id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, measured_at TEXT NOT NULL, "
    "battery_cycles INTEGER CHECK (battery_cycles >= 0), "
    "reference_ah REAL NOT NULL CHECK (reference_ah > 0), "
    "measured_ah REAL NOT NULL CHECK (measured_ah > 0), "
    "rated_usable_capacity_wh REAL NOT NULL CHECK (rated_usable_capacity_wh > 0), "
    "current_usable_capacity_wh REAL NOT NULL CHECK (current_usable_capacity_wh > 0), "
    "module_counts TEXT NOT NULL, source_contract TEXT NOT NULL, "
    "UNIQUE (device_id, measured_at)) STRICT;"
)

_SCHEMA: dict[str, str] = {
    "settings": "CREATE TABLE settings (key TEXT PRIMARY KEY, encrypted BLOB NOT NULL) STRICT;",
    "admin_user": "CREATE TABLE admin_user (username TEXT PRIMARY KEY, encrypted BLOB NOT NULL) STRICT;",
    "sessions": "CREATE TABLE sessions (digest TEXT PRIMARY KEY, encrypted BLOB NOT NULL) STRICT;",
    "pats": (
        "CREATE TABLE pats (id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, "
        "encrypted BLOB NOT NULL) STRICT;"
    ),
    "spot_market_intervals": (
        "CREATE TABLE spot_market_intervals ("
        "config_key TEXT NOT NULL, local_date TEXT NOT NULL, interval_start TEXT PRIMARY KEY, "
        "interval_end TEXT NOT NULL, price_eur_per_kwh REAL NOT NULL, timezone TEXT NOT NULL, "
        "fetched_at TEXT NOT NULL) STRICT;"
    ),
    "solar_forecast_intervals": (
        "CREATE TABLE solar_forecast_intervals ("
        "config_key TEXT NOT NULL, local_date TEXT NOT NULL, interval_start TEXT PRIMARY KEY, "
        "interval_end TEXT NOT NULL, energy_wh REAL NOT NULL CHECK (energy_wh >= 0), "
        "timezone TEXT NOT NULL, fetched_at TEXT NOT NULL) STRICT;"
    ),
    # Local household-consumption history, aggregated into fixed 15-minute UTC buckets per device.
    # It is the optimizer's own data source and is deliberately independent of the TSDB exporter:
    # the integrated energy, the number of samples and the covered seconds are stored so a later
    # forecast can treat a poorly-covered bucket as uncertain. UTC ISO-8601 instants, one row per
    # (device, bucket start), atomically upserted.
    "consumption_history_intervals": (
        "CREATE TABLE consumption_history_intervals ("
        "device_id TEXT NOT NULL, interval_start TEXT NOT NULL, interval_end TEXT NOT NULL, "
        "energy_wh REAL NOT NULL CHECK (energy_wh >= 0), "
        "sample_count INTEGER NOT NULL CHECK (sample_count >= 0), "
        "coverage_seconds REAL NOT NULL CHECK (coverage_seconds >= 0), "
        "PRIMARY KEY (device_id, interval_start)) STRICT;"
    ),
    # Counter-based (meter) energy per bucket: MEASURED parts come from counter pairs close together,
    # ESTIMATED parts are a counter difference spread across an app stop. Kept apart from the power-
    # integrated table so quality is never blended, and so an older database needs no migration.
    "consumption_meter_intervals": (
        "CREATE TABLE consumption_meter_intervals ("
        "device_id TEXT NOT NULL, interval_start TEXT NOT NULL, interval_end TEXT NOT NULL, "
        "measured_wh REAL NOT NULL CHECK (measured_wh >= 0), "
        "measured_seconds REAL NOT NULL CHECK (measured_seconds >= 0), "
        "estimated_wh REAL NOT NULL CHECK (estimated_wh >= 0), "
        "estimated_seconds REAL NOT NULL CHECK (estimated_seconds >= 0), "
        "PRIMARY KEY (device_id, interval_start)) STRICT;"
    ),
    # Thinned cumulative-counter readings (about one per 15 minutes) kept for cross-checks and for
    # reconstructing a day boundary, plus the single newest reading per counter that bridges an app stop.
    "consumption_counter_readings": (
        "CREATE TABLE consumption_counter_readings ("
        "device_id TEXT NOT NULL, metric TEXT NOT NULL, measured_at TEXT NOT NULL, value_wh REAL NOT NULL, "
        "PRIMARY KEY (device_id, metric, measured_at)) STRICT;"
    ),
    "consumption_counter_state": (
        "CREATE TABLE consumption_counter_state ("
        "device_id TEXT NOT NULL, metric TEXT NOT NULL, measured_at TEXT NOT NULL, value_wh REAL NOT NULL, "
        "PRIMARY KEY (device_id, metric)) STRICT;"
    ),
    # Ledger of applied write batches: a retried batch is recognised and not added a second time.
    "consumption_applied_batches": (
        "CREATE TABLE consumption_applied_batches ("
        "device_id TEXT NOT NULL, batch_id TEXT NOT NULL, batch_time TEXT NOT NULL, "
        "PRIMARY KEY (device_id, batch_id)) STRICT;"
    ),
    # Completed local calendar days taken from the inverter's own daily logger. A day total is an
    # alternative day source, never a 15-minute observation: it is kept apart from the interval tables
    # so it can neither enter the forecast nor be added to a locally measured sum.
    "consumption_daily_history": (
        "CREATE TABLE consumption_daily_history ("
        "device_id TEXT NOT NULL, local_date TEXT NOT NULL, "
        "energy_wh REAL NOT NULL CHECK (energy_wh >= 0), "
        "source TEXT NOT NULL, timezone TEXT NOT NULL, fetched_at TEXT NOT NULL, "
        "PRIMARY KEY (device_id, local_date)) STRICT;"
    ),
    # Timestamped next-day consumption forecasts, to be compared with later measured actuals.
    "consumption_forecast_runs": (
        "CREATE TABLE consumption_forecast_runs ("
        "device_id TEXT NOT NULL, created_at TEXT NOT NULL, target_date TEXT NOT NULL, "
        "method TEXT NOT NULL, explanation TEXT NOT NULL, PRIMARY KEY (device_id, created_at, method)) STRICT;"
    ),
    "consumption_forecast_values": (
        "CREATE TABLE consumption_forecast_values ("
        "device_id TEXT NOT NULL, created_at TEXT NOT NULL, method TEXT NOT NULL, interval_start TEXT NOT NULL, "
        "energy_wh REAL NOT NULL CHECK (energy_wh >= 0), "
        "PRIMARY KEY (device_id, created_at, method, interval_start)) STRICT;"
    ),
    # Keyed by the inverter serial (never by device_id, which is only a local alias). The serial is
    # nullable solely for rows that predate it; the writer always sets it. battery_bms_sn tells a
    # swapped battery apart: a new BMS serial starts a new series.
    "battery_calibration_history": _BATTERY_CALIBRATION_SQL,
    # Commissioning date per inverter serial. 'manual' (operator) wins over 'logger_derived'.
    "inverter_commissioning": (
        "CREATE TABLE inverter_commissioning ("
        "inverter_serial TEXT NOT NULL, commissioned_at TEXT NOT NULL, "
        "source TEXT NOT NULL CHECK (source IN ('logger_derived', 'manual')), "
        "determined_at TEXT NOT NULL, PRIMARY KEY (inverter_serial, source)) STRICT;"
    ),
    # Admin-UI dashboard layout as structured rows, one per placed widget, not an opaque JSON blob.
    # position preserves the widgets-list order the API round-trips; the whole layout is read and
    # replaced atomically. This holds non-secret UI config, so it is stored in plain columns rather
    # than the encrypted settings table.
    "dashboard_layout": (
        "CREATE TABLE dashboard_layout ("
        "widget_id TEXT PRIMARY KEY, position INTEGER NOT NULL, "
        "x INTEGER NOT NULL, y INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL, "
        "visible INTEGER NOT NULL CHECK (visible IN (0, 1))) STRICT;"
    ),
    # The layout's version scalar lives in its own one-row companion: it is a layout-level field,
    # not a per-widget attribute, and its presence is the "a layout is stored" marker, so an empty
    # widget list can still be a stored (versioned) layout and absence means "no layout".
    "dashboard_layout_meta": (
        "CREATE TABLE dashboard_layout_meta ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL) STRICT;"
    ),
}

# Retention prunes several consumption tables by a non-PK-leading timestamp column. Only the two
# where EXPLAIN QUERY PLAN showed a table scan over a continuously growing, append-only ledger get a
# supporting index: the 15-minute history buckets (interval_start) and the applied-batch ledger
# (batch_time, a new marker per non-empty 30 s drain that is never consolidated). The other pruned
# tables — meter intervals, counter readings, forecast values/runs — are bounded by upsert, thinning
# or a daily cadence to at most low tens of thousands of rows per device, so they are left unindexed.
_CONSUMPTION_PRUNE_INDEXES = {
    "consumption_history_intervals_interval_start_idx": (
        "CREATE INDEX consumption_history_intervals_interval_start_idx "
        "ON consumption_history_intervals(interval_start)"
    ),
    "consumption_applied_batches_batch_time_idx": (
        "CREATE INDEX consumption_applied_batches_batch_time_idx "
        "ON consumption_applied_batches(batch_time)"
    ),
}


def _schema_sql(sql: str) -> str:
    return sql.strip().rstrip(";")


def _utc_timestamp() -> str:
    """Server-generated UTC acceptance instant, second precision (e.g. ``2026-10-16T14:42:31Z``)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        # Serialises forecast-table writes of the background refresh against a config save, so a
        # refresh that started under the old config cannot write rows back after the save cleared them.
        self.forecast_write_lock = threading.Lock()
        self._touch_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pat-last-used"
        )
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"rct-admin-db-v1"
        ).derive(secret.encode())
        self._fernet = Fernet(base64.urlsafe_b64encode(key))
        self._hmac_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"rct-admin-auth-v1"
        ).derive(secret.encode())

    def _ensure_secure_parent(self) -> None:
        """Reject fail-closed unless the database parent is a private, owner-writable directory.

        The parent is created with 0o700 when absent. A parent that already exists and is group- or
        world-writable, or is not a directory, lets another local principal replace the database
        path after any check, so the open is refused instead. An existing database that is not a
        regular file (a symlink, FIFO or directory) is refused too.
        """
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_st = os.stat(parent, follow_symlinks=False)
        if not stat.S_ISDIR(parent_st.st_mode) or parent_st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(
                f"admin database parent {parent} must be a directory only its owner can write"
            )
        try:
            file_st = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return  # created below under the validated parent
        if not stat.S_ISREG(file_st.st_mode):
            raise ValueError(f"admin database path {self.path} is not a regular file")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # Validate the (operator-configurable) parent before opening, fail-closed. O_NOFOLLOW only
        # guards the descriptor below; sqlite3.connect reopens the path by name, so a path swap
        # between the two is a TOCTOU window. A parent another local principal can write (group- or
        # world-writable) is what makes that swap possible, so such a parent is rejected rather than
        # trusted; an existing database that is not a regular file is rejected too.
        self._ensure_secure_parent()
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                os.fchmod(fd, 0o600)  # tighten a database created with a looser umask
        finally:
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 5000")
            journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0].lower()
            db.execute(
                "PRAGMA synchronous = NORMAL"
                if journal_mode == "wal"
                else "PRAGMA synchronous = FULL"
            )
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

    @staticmethod
    def _migrate_battery_calibration_history(db: sqlite3.Connection) -> None:
        """Rebuild the device_id-keyed history of the first build, keeping every row (idempotent).

        The inverter serial of those rows stays NULL until record_battery_calibration or
        attach_inverter_serial learns it for the device_id.
        """
        row = db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='battery_calibration_history'"
        ).fetchone()
        if row is None or _schema_sql(row["sql"]) != _schema_sql(
            _LEGACY_BATTERY_CALIBRATION_SQL
        ):
            return
        columns = (
            "id, device_id, measured_at, battery_cycles, reference_ah, measured_ah, "
            "rated_usable_capacity_wh, current_usable_capacity_wh, module_counts, source_contract"
        )
        db.executescript(
            "BEGIN IMMEDIATE;"  # noqa: S608 - identifiers are module constants
            "ALTER TABLE battery_calibration_history RENAME TO battery_calibration_history_legacy;"
            f"{_BATTERY_CALIBRATION_SQL}"
            f"INSERT INTO battery_calibration_history ({columns}) SELECT {columns} "
            "FROM battery_calibration_history_legacy;"
            "DROP TABLE battery_calibration_history_legacy;COMMIT;"
        )

    @staticmethod
    def _create_baseline_schema(db: sqlite3.Connection) -> None:
        """Create any missing baseline table; reject one whose on-disk shape is not the current one.

        A fresh file gets every table created; a normal restart finds them already present with the
        expected definition and does nothing. No general migration path exists; the first
        battery-calibration schema is the sole supported compatibility migration (applied below),
        and any other incompatible old table fails clearly rather than being migrated.
        """
        AdminStore._migrate_battery_calibration_history(db)
        for table, create in _SCHEMA.items():
            row = db.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row is None:
                db.executescript(f"BEGIN IMMEDIATE;{create}COMMIT;")
            elif _schema_sql(row["sql"]) != _schema_sql(create):
                raise ValueError(
                    f"admin database table {table!r} does not match the current schema; this build "
                    "does not migrate an older database — delete it and restart"
                )
        created_any = False
        for name, create in _CONSUMPTION_PRUNE_INDEXES.items():
            row = db.execute(
                "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?", (name,)
            ).fetchone()
            if row is None:
                db.execute(create)
                created_any = True
            elif row["sql"] is None or _schema_sql(row["sql"]) != _schema_sql(create):
                raise ValueError(
                    f"admin database index {name!r} does not match the current schema"
                )
        if created_any:
            db.execute("PRAGMA optimize")

    def initialize(self) -> str | None:
        """Create schema and return a one-time bootstrap password, if needed."""
        with self.connect() as db:
            # Persistent; readers no longer block the writer, so autosaves and last-used updates can overlap.
            if db.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() != "wal":
                log.warning(
                    "SQLite WAL mode is unavailable (network file system?); using the default journal"
                )
            else:
                db.execute("PRAGMA synchronous = NORMAL")
            # The application is unreleased. Create the current baseline schema directly. A normal
            # restart finds the tables already present with the expected definitions and does
            # nothing. No general migration path exists; the first battery-calibration schema is the
            # sole supported compatibility migration, and any other incompatible old dev table (a
            # different shape) fails clearly instead of being silently upgraded, so the developer
            # must delete it before start.
            self._create_baseline_schema(db)
            db.execute("BEGIN IMMEDIATE")
            check = db.execute(
                "SELECT encrypted FROM settings WHERE key='__key_check__'"
            ).fetchone()
            if check:
                self._decode("__key_check__", check["encrypted"])
            else:
                if db.execute("SELECT 1 FROM admin_user").fetchone():
                    raise ValueError("admin database has no key verifier")
                db.execute(
                    "INSERT INTO settings(key,encrypted) VALUES (?,?)",
                    ("__key_check__", self._encode("__key_check__", "rct-admin-v1")),
                )
            if db.execute("SELECT 1 FROM admin_user WHERE username='admin'").fetchone():
                return None
            password = _bootstrap_password()
            db.execute(
                "INSERT INTO admin_user(username, encrypted) VALUES ('admin', ?)",
                (
                    self._encode(
                        "admin",
                        {
                            "password_hash": _PASSWORDS.hash(password),
                            "must_change": True,
                        },
                    ),
                ),
            )
            return password

    def _insert_token(
        self, db: sqlite3.Connection, name: str, role: str, expires_at: datetime | None
    ) -> tuple[dict, str]:
        if not 1 <= len(name) <= 64 or any(ord(char) < 32 for char in name):
            raise ValueError("invalid token name")
        if role not in (TokenRole.READ, TokenRole.READ_WRITE):
            raise ValueError("invalid token role")
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise ValueError("token expiry must be in the future")
        token = generate_pat()
        record = {
            "id": secrets.token_hex(16),
            "name": name,
            "role": role,
            "suffix": token[-4:],
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
        }
        if db.execute("SELECT count(*) FROM pats").fetchone()[0] >= 32:
            raise ValueError("at most 32 API tokens are allowed")
        db.execute(
            "INSERT INTO pats(id,digest,encrypted) VALUES (?,?,?)",
            (
                record["id"],
                self._pat_digest(hashlib.sha256(token.encode()).hexdigest()),
                self._encode(record["id"], record),
            ),
        )
        return record, token

    def _digest(self, value: str) -> str:
        return hmac.new(self._hmac_key, value.encode(), hashlib.sha256).hexdigest()

    def _pat_digest(self, sha256_hex: str) -> str:
        return self._digest("pat:" + sha256_hex)

    def _encode(self, key: str, value) -> bytes:
        return self._fernet.encrypt(
            json.dumps({"key": key, "value": value}, separators=(",", ":")).encode()
        )

    def _decode(self, key: str, encrypted: bytes):
        try:
            data = json.loads(self._fernet.decrypt(encrypted))
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise ValueError(
                "admin database cannot be decrypted with HMAC_SECRET"
            ) from exc
        if not isinstance(data, dict) or data.get("key") != key:
            raise ValueError("admin database record integrity check failed")
        return data["value"]

    def get(self, key: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT encrypted FROM settings WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        return self._decode(key, row["encrypted"])

    def get_refresh_status(self, key: str):
        """Read a background-refresh status record; a dedicated accessor so a dashboard projection
        can annotate data with a refresh error without being counted as a data-cache read.
        """
        return self.get(key)

    def put(self, key: str, value) -> None:
        encrypted = self._encode(key, value)
        with self.connect() as db:
            db.execute(_UPSERT_SETTING, (key, encrypted))

    def put_many(self, values: dict[str, object]) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key, value in values.items():
                db.execute(_UPSERT_SETTING, (key, self._encode(key, value)))

    def delete(self, key: str) -> None:
        """Remove a stored value; a missing key is not an error (idempotent reset-to-default)."""
        with self.connect() as db:
            db.execute("DELETE FROM settings WHERE key=?", (key,))

    def replace_spot_market_intervals(self, rows: list[dict]) -> None:
        """Atomically replace the bounded spot-price window; this table never contains history."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM spot_market_intervals")
            db.executemany(
                "INSERT INTO spot_market_intervals "
                "(config_key, local_date, interval_start, interval_end, price_eur_per_kwh, timezone, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        row["config_key"],
                        row["local_date"],
                        row["interval_start"],
                        row["interval_end"],
                        row["price_eur_per_kwh"],
                        row["timezone"],
                        row["fetched_at"],
                    )
                    for row in rows
                ],
            )

    def get_spot_market_intervals(
        self,
        config_key: str,
        first_date: str | None = None,
        last_date: str | None = None,
    ) -> list[dict]:
        with self.connect() as db:
            if first_date is not None and last_date is not None:
                rows = db.execute(
                    "SELECT config_key, local_date, interval_start, interval_end, price_eur_per_kwh, timezone, fetched_at "
                    "FROM spot_market_intervals WHERE config_key=? AND local_date BETWEEN ? AND ? "
                    "ORDER BY interval_start",
                    (config_key, first_date, last_date),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT config_key, local_date, interval_start, interval_end, price_eur_per_kwh, timezone, fetched_at "
                    "FROM spot_market_intervals WHERE config_key=? ORDER BY interval_start",
                    (config_key,),
                ).fetchall()
        return [dict(row) for row in rows]

    def clear_spot_market_intervals(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM spot_market_intervals")

    def prune_spot_market_intervals(
        self, config_key: str, first_date: str, last_date: str
    ) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM spot_market_intervals WHERE config_key=? AND (local_date < ? OR local_date > ?)",
                (config_key, first_date, last_date),
            )

    def replace_solar_forecast_intervals(self, rows: list[dict]) -> None:
        """Atomically replace the bounded solar forecast window; no prior forecast is retained."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM solar_forecast_intervals")
            db.executemany(
                "INSERT INTO solar_forecast_intervals "
                "(config_key, local_date, interval_start, interval_end, energy_wh, timezone, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        row["config_key"],
                        row["local_date"],
                        row["interval_start"],
                        row["interval_end"],
                        row["energy_wh"],
                        row["timezone"],
                        row["fetched_at"],
                    )
                    for row in rows
                ],
            )

    def get_solar_forecast_intervals(
        self, config_key: str, first_date: str, last_date: str
    ) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT local_date, interval_start, interval_end, energy_wh, timezone, fetched_at "
                "FROM solar_forecast_intervals WHERE config_key=? AND local_date BETWEEN ? AND ? "
                "ORDER BY interval_start",
                (config_key, first_date, last_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_solar_forecast_intervals(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM solar_forecast_intervals")

    def prune_solar_forecast_intervals(
        self, config_key: str, first_date: str, last_date: str
    ) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM solar_forecast_intervals WHERE config_key=? AND (local_date < ? OR local_date > ?)",
                (config_key, first_date, last_date),
            )

    def upsert_consumption_intervals(
        self,
        device_id: str,
        buckets: list[dict],
        batch_id: str | None = None,
        meter_buckets: Sequence[dict] = (),
        counter_readings: Sequence[tuple[str, str, float]] = (),
        counter_state: Sequence[tuple[str, str, float]] = (),
    ) -> bool:
        """Accumulate one device's aggregated consumption data in ONE transaction; ``False`` if skipped.

        Power buckets carry the energy, sample count and covered seconds the drain computed since
        the last write; a bucket may be written more than once (still being filled), so the write
        ADDS to any existing row for the same (device, bucket start); coverage is clamped to the 900 s
        a bucket holds. ``meter_buckets`` are the counter-based equivalents (measured and estimated
        parts), ``counter_readings`` thinned ``(metric, measured_at, value_wh)`` rows and
        ``counter_state`` the newest reading per counter. With a ``batch_id`` the write is idempotent:
        a batch already applied (a retry after an ambiguous failure) changes nothing and returns False.
        """
        batch_time = max(
            [b["interval_start"] for b in buckets]
            + [b["interval_start"] for b in meter_buckets]
            + [at for _, at, _ in counter_state]
            or [""]
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if batch_id is not None:
                marked = db.execute(
                    "INSERT OR IGNORE INTO consumption_applied_batches (device_id, batch_id, batch_time) "
                    "VALUES (?, ?, ?)",
                    (device_id, batch_id, batch_time),
                )
                if marked.rowcount == 0:
                    return False
            db.executemany(
                "INSERT INTO consumption_history_intervals "
                "(device_id, interval_start, interval_end, energy_wh, sample_count, coverage_seconds) "
                "VALUES (:device_id, :interval_start, :interval_end, :energy_wh, :sample_count, :coverage_seconds) "
                "ON CONFLICT(device_id, interval_start) DO UPDATE SET "
                "energy_wh = energy_wh + excluded.energy_wh, "
                "sample_count = sample_count + excluded.sample_count, "
                "coverage_seconds = MIN(900.0, coverage_seconds + excluded.coverage_seconds)",
                [
                    {
                        "device_id": device_id,
                        "interval_start": bucket["interval_start"],
                        "interval_end": bucket["interval_end"],
                        "energy_wh": bucket["energy_wh"],
                        "sample_count": bucket["sample_count"],
                        "coverage_seconds": bucket["coverage_seconds"],
                    }
                    for bucket in buckets
                ],
            )
            db.executemany(
                "INSERT INTO consumption_meter_intervals "
                "(device_id, interval_start, interval_end, measured_wh, measured_seconds, "
                "estimated_wh, estimated_seconds) "
                "VALUES (:device_id, :interval_start, :interval_end, :measured_wh, :measured_seconds, "
                ":estimated_wh, :estimated_seconds) "
                "ON CONFLICT(device_id, interval_start) DO UPDATE SET "
                "measured_wh = measured_wh + excluded.measured_wh, "
                "measured_seconds = MIN(900.0, measured_seconds + excluded.measured_seconds), "
                "estimated_wh = estimated_wh + excluded.estimated_wh, "
                "estimated_seconds = MIN(900.0, estimated_seconds + excluded.estimated_seconds)",
                [
                    {
                        "device_id": device_id,
                        "interval_start": bucket["interval_start"],
                        "interval_end": bucket["interval_end"],
                        "measured_wh": bucket["measured_wh"],
                        "measured_seconds": bucket["measured_seconds"],
                        "estimated_wh": bucket["estimated_wh"],
                        "estimated_seconds": bucket["estimated_seconds"],
                    }
                    for bucket in meter_buckets
                ],
            )
            db.executemany(
                "INSERT OR IGNORE INTO consumption_counter_readings (device_id, metric, measured_at, value_wh) "
                "VALUES (?, ?, ?, ?)",
                [
                    (device_id, metric, at, value)
                    for metric, at, value in counter_readings
                ],
            )
            db.executemany(
                "INSERT INTO consumption_counter_state (device_id, metric, measured_at, value_wh) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(device_id, metric) DO UPDATE SET "
                "measured_at = excluded.measured_at, value_wh = excluded.value_wh "
                "WHERE excluded.measured_at > measured_at",
                [(device_id, metric, at, value) for metric, at, value in counter_state],
            )
        return True

    def get_consumption_meter_intervals(
        self, device_id: str, first_start: str, last_start: str
    ) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT interval_start, interval_end, measured_wh, measured_seconds, estimated_wh, "
                "estimated_seconds FROM consumption_meter_intervals "
                "WHERE device_id=? AND interval_start BETWEEN ? AND ? ORDER BY interval_start",
                (device_id, first_start, last_start),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_consumption_counter_state(
        self, device_id: str
    ) -> dict[str, tuple[str, float]]:
        """The newest stored reading per counter metric: ``{metric: (measured_at, value_wh)}``."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT metric, measured_at, value_wh FROM consumption_counter_state WHERE device_id=?",
                (device_id,),
            ).fetchall()
        return {row["metric"]: (row["measured_at"], row["value_wh"]) for row in rows}

    def get_consumption_counter_readings(
        self, device_id: str, metric: str, first: str, last: str
    ) -> list[tuple[str, float]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT measured_at, value_wh FROM consumption_counter_readings "
                "WHERE device_id=? AND metric=? AND measured_at BETWEEN ? AND ? ORDER BY measured_at",
                (device_id, metric, first, last),
            ).fetchall()
        return [(row["measured_at"], row["value_wh"]) for row in rows]

    def record_consumption_forecast(
        self,
        device_id: str,
        created_at: str,
        target_date: str,
        method: str,
        explanation: str,
        values: Sequence[tuple[str, float]],
    ) -> bool:
        """Store one timestamped forecast run (idempotent per device, creation instant and method)."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            added = db.execute(
                "INSERT OR IGNORE INTO consumption_forecast_runs "
                "(device_id, created_at, target_date, method, explanation) VALUES (?, ?, ?, ?, ?)",
                (device_id, created_at, target_date, method, explanation),
            )
            if added.rowcount == 0:
                return False
            db.executemany(
                "INSERT OR IGNORE INTO consumption_forecast_values "
                "(device_id, created_at, method, interval_start, energy_wh) VALUES (?, ?, ?, ?, ?)",
                [
                    (device_id, created_at, method, start, energy)
                    for start, energy in values
                ],
            )
        return True

    def get_consumption_forecast_runs(
        self, device_id: str, first_created: str, last_created: str
    ) -> list[dict]:
        """Forecast runs created in the window, each with its quarter-hour ``values`` list."""
        with self.connect() as db:
            runs = db.execute(
                "SELECT created_at, target_date, method, explanation FROM consumption_forecast_runs "
                "WHERE device_id=? AND created_at BETWEEN ? AND ? ORDER BY created_at, method",
                (device_id, first_created, last_created),
            ).fetchall()
            result = []
            for run in runs:
                values = db.execute(
                    "SELECT interval_start, energy_wh FROM consumption_forecast_values "
                    "WHERE device_id=? AND created_at=? AND method=? ORDER BY interval_start",
                    (device_id, run["created_at"], run["method"]),
                ).fetchall()
                result.append(
                    {
                        **dict(run),
                        "values": [
                            (v["interval_start"], v["energy_wh"]) for v in values
                        ],
                    }
                )
        return result

    def has_consumption_forecast_run(
        self, device_id: str, target_date: str, method: str
    ) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM consumption_forecast_runs WHERE device_id=? AND target_date=? AND method=? LIMIT 1",
                (device_id, target_date, method),
            ).fetchone()
        return row is not None

    def get_consumption_intervals(
        self,
        device_id: str,
        first_start: str | None = None,
        last_start: str | None = None,
    ) -> list[dict]:
        """One device's consumption buckets, ordered by start; optionally bounded to a start window.

        ``first_start``/``last_start`` are inclusive UTC ISO-8601 bucket-start bounds; both or
        neither are given.
        """
        with self.connect() as db:
            if first_start is not None and last_start is not None:
                rows = db.execute(
                    "SELECT device_id, interval_start, interval_end, energy_wh, sample_count, coverage_seconds "
                    "FROM consumption_history_intervals WHERE device_id=? AND interval_start BETWEEN ? AND ? "
                    "ORDER BY interval_start",
                    (device_id, first_start, last_start),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT device_id, interval_start, interval_end, energy_wh, sample_count, coverage_seconds "
                    "FROM consumption_history_intervals WHERE device_id=? ORDER BY interval_start",
                    (device_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def first_consumption_measurement(self, device_id: str) -> str | None:
        """Start of the device's earliest local consumption bucket (UTC ISO 8601), or None."""
        with self.connect() as db:
            row = db.execute(
                "SELECT MIN(start) FROM ("
                "SELECT MIN(interval_start) AS start FROM consumption_history_intervals WHERE device_id=? "
                "UNION ALL SELECT MIN(interval_start) FROM consumption_meter_intervals WHERE device_id=?)",
                (device_id, device_id),
            ).fetchone()
        return row[0] if row else None

    def upsert_consumption_daily(self, device_id: str, rows: list[dict]) -> None:
        """Store completed-day totals (``local_date``, ``energy_wh``, ``source``, ``timezone``, ``fetched_at``)."""
        if not rows:
            return
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "INSERT INTO consumption_daily_history (device_id, local_date, energy_wh, source, timezone, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(device_id, local_date) DO UPDATE SET "
                "energy_wh=excluded.energy_wh, source=excluded.source, timezone=excluded.timezone, "
                "fetched_at=excluded.fetched_at",
                [
                    (
                        device_id,
                        r["local_date"],
                        r["energy_wh"],
                        r["source"],
                        r["timezone"],
                        r["fetched_at"],
                    )
                    for r in rows
                ],
            )

    def get_consumption_daily(
        self, device_id: str, first_date: str, last_date: str
    ) -> list[dict]:
        """Day totals with ``first_date <= local_date <= last_date`` (ISO dates), oldest first."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT local_date, energy_wh, source, timezone FROM consumption_daily_history "
                "WHERE device_id=? AND local_date BETWEEN ? AND ? ORDER BY local_date",
                (device_id, first_date, last_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def attach_inverter_serial(self, device_id: str, inverter_serial: str) -> int:
        """Give history rows that predate the serial their inverter serial; returns the rows updated."""
        if not inverter_serial:
            return 0
        with self.connect() as db:
            return db.execute(
                "UPDATE battery_calibration_history SET inverter_serial=? "
                "WHERE device_id=? AND inverter_serial IS NULL",
                (inverter_serial, device_id),
            ).rowcount

    def record_battery_calibration(self, sample) -> bool:
        """Keep each changed BMS capacity measurement permanently; return whether a row was added.

        Rows belong to the inverter serial. A measurement is skipped when the Ah value is unchanged
        for the same battery; a new BMS serial always starts a new series.
        """
        if sample.source_contract not in _QUALIFIED_BATTERY_CALIBRATION_CONTRACTS:
            raise ValueError("unsupported battery calibration source contract")
        if sample.measured_at.tzinfo is None or sample.measured_at.utcoffset() is None:
            raise ValueError("battery calibration timestamp must be timezone-aware")
        serial = sample.inverter_serial
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("battery calibration needs the inverter serial")
        bms_sn = sample.battery_bms_sn or None
        measured_at = sample.measured_at.astimezone(UTC).isoformat()
        values = (
            sample.reference_ah,
            sample.measured_ah,
            sample.rated_usable_capacity_wh,
            sample.current_usable_capacity_wh,
        )
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError(
                "battery calibration capacities must be finite and positive"
            )
        if sample.battery_cycles is not None and (
            isinstance(sample.battery_cycles, bool) or sample.battery_cycles < 0
        ):
            raise ValueError("battery calibration cycle count must be nonnegative")
        counts = tuple(sample.module_counts)
        if not counts or any(
            isinstance(count, bool) or not isinstance(count, int) or count <= 0
            for count in counts
        ):
            raise ValueError(
                "battery calibration topology must contain positive module counts"
            )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE battery_calibration_history SET inverter_serial=? WHERE device_id=? AND inverter_serial IS NULL",
                (serial, sample.device_id),
            )
            latest = db.execute(
                "SELECT measured_ah FROM battery_calibration_history "
                "WHERE inverter_serial=? AND source_contract=? ORDER BY id DESC LIMIT 1",
                (serial, sample.source_contract),
            ).fetchone()
            known_bms = db.execute(
                "SELECT battery_bms_sn FROM battery_calibration_history "
                "WHERE inverter_serial=? AND source_contract=? "
                "AND battery_bms_sn IS NOT NULL ORDER BY id DESC LIMIT 1",
                (serial, sample.source_contract),
            ).fetchone()
            new_series = bms_sn is not None and (
                known_bms is None or known_bms["battery_bms_sn"] != bms_sn
            )
            if (
                not new_series
                and latest is not None
                and abs(latest["measured_ah"] - sample.measured_ah) < 0.01
            ):
                return False
            inserted = db.execute(
                "INSERT OR IGNORE INTO battery_calibration_history ("
                "device_id, inverter_serial, battery_bms_sn, measured_at, battery_cycles, reference_ah, "
                "measured_ah, rated_usable_capacity_wh, current_usable_capacity_wh, module_counts, source_contract"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sample.device_id,
                    serial,
                    bms_sn,
                    measured_at,
                    sample.battery_cycles,
                    sample.reference_ah,
                    sample.measured_ah,
                    sample.rated_usable_capacity_wh,
                    sample.current_usable_capacity_wh,
                    json.dumps(counts),
                    sample.source_contract,
                ),
            )
            return inserted.rowcount == 1

    def get_battery_calibration_history(
        self, inverter_serial: str, limit: int = 1200
    ) -> dict:
        """Newest bounded part of the inverter's current battery series, oldest first.

        ``battery_replaced`` is true when the inverter also recorded a different BMS serial earlier;
        those older rows belong to the previous battery and are not returned.
        """
        if not 1 <= limit <= 5000:
            raise ValueError(
                "battery calibration history limit must be between 1 and 5000"
            )
        if not _QUALIFIED_BATTERY_CALIBRATION_CONTRACTS:
            return {"battery_bms_sn": None, "battery_replaced": False, "points": []}
        if len(_QUALIFIED_BATTERY_CALIBRATION_CONTRACTS) != 1:
            raise ValueError("one qualified calibration contract must be selected")
        contract = next(iter(_QUALIFIED_BATTERY_CALIBRATION_CONTRACTS))
        with self.connect() as db:
            current = db.execute(
                "SELECT battery_bms_sn FROM battery_calibration_history "
                "WHERE inverter_serial=? AND source_contract=? "
                "AND battery_bms_sn IS NOT NULL ORDER BY id DESC LIMIT 1",
                (inverter_serial, contract),
            ).fetchone()
            bms_sn = current["battery_bms_sn"] if current is not None else None
            cut = 0
            if bms_sn is not None:
                cut = db.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM battery_calibration_history "
                    "WHERE inverter_serial=? AND source_contract=? "
                    "AND battery_bms_sn IS NOT NULL AND battery_bms_sn != ?",
                    (inverter_serial, contract, bms_sn),
                ).fetchone()[0]
            rows = db.execute(
                "SELECT measured_at, battery_bms_sn, battery_cycles, reference_ah, measured_ah, "
                "rated_usable_capacity_wh, current_usable_capacity_wh, module_counts, source_contract "
                "FROM battery_calibration_history WHERE inverter_serial=? AND id>? "
                "AND source_contract=? ORDER BY id DESC LIMIT ?",
                (inverter_serial, cut, contract, limit),
            ).fetchall()
        return {
            "battery_bms_sn": bms_sn,
            "battery_replaced": cut > 0,
            "points": [
                {
                    **dict(row),
                    "module_counts": json.loads(row["module_counts"]),
                    "remaining_capacity_percent": round(
                        min(
                            100.0,
                            row["current_usable_capacity_wh"]
                            / row["rated_usable_capacity_wh"]
                            * 100,
                        ),
                        2,
                    ),
                }
                for row in reversed(rows)
            ],
        }

    def set_inverter_commissioning(
        self, inverter_serial: str, commissioned_at: datetime, source: str
    ) -> None:
        """Persist the commissioning date of one inverter; one row per source, a repeat replaces it."""
        if not inverter_serial:
            raise ValueError("inverter serial is required")
        if source not in ("logger_derived", "manual"):
            raise ValueError("unknown commissioning source")
        if commissioned_at.tzinfo is None or commissioned_at.utcoffset() is None:
            raise ValueError("commissioning timestamp must be timezone-aware")
        with self.connect() as db:
            db.execute(
                "INSERT INTO inverter_commissioning (inverter_serial, commissioned_at, source, determined_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(inverter_serial, source) DO UPDATE SET "
                "commissioned_at=excluded.commissioned_at, determined_at=excluded.determined_at",
                (
                    inverter_serial,
                    commissioned_at.astimezone(UTC).isoformat(),
                    source,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def delete_inverter_commissioning(self, inverter_serial: str, source: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "DELETE FROM inverter_commissioning WHERE inverter_serial=? AND source=?",
                    (inverter_serial, source),
                ).rowcount
                > 0
            )

    def get_inverter_commissioning(self, inverter_serial: str) -> dict | None:
        """The effective commissioning date: the manual entry if present, else the derived one."""
        with self.connect() as db:
            row = db.execute(
                "SELECT commissioned_at, source, determined_at FROM inverter_commissioning "
                "WHERE inverter_serial=? ORDER BY (source='manual') DESC LIMIT 1",
                (inverter_serial,),
            ).fetchone()
        return None if row is None else dict(row)

    def get_dashboard_layout(self) -> dict | None:
        """The stored dashboard layout as ``{"version": int, "widgets": [...]}``, or None if absent.

        Widgets are returned in their stored ``position`` order, reconstructing the exact JSON shape
        the API persisted. ``None`` means no layout is stored (the meta row is the presence marker),
        mirroring the "no layout stored" result the GET endpoint returns in that case.
        """
        with self.connect() as db:
            meta = db.execute(
                "SELECT version FROM dashboard_layout_meta WHERE id=1"
            ).fetchone()
            if meta is None:
                return None
            rows = db.execute(
                "SELECT widget_id, x, y, w, h, visible FROM dashboard_layout ORDER BY position"
            ).fetchall()
        return {
            "version": meta["version"],
            "widgets": [
                {
                    "id": row["widget_id"],
                    "x": row["x"],
                    "y": row["y"],
                    "w": row["w"],
                    "h": row["h"],
                    "visible": bool(row["visible"]),
                }
                for row in rows
            ],
        }

    def replace_dashboard_layout(self, layout: dict) -> None:
        """Atomically replace the whole layout: clear every widget row and the meta, then rewrite.

        ``layout`` is the validated ``{"version": int, "widgets": [...]}`` shape; each widget carries
        ``id``, ``x``, ``y``, ``w``, ``h`` and ``visible``. The widgets-list order is persisted as
        ``position`` so a later read returns the widgets in the same order.
        """
        widgets = layout["widgets"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM dashboard_layout")
            db.execute("DELETE FROM dashboard_layout_meta")
            db.execute(
                "INSERT INTO dashboard_layout_meta (id, version) VALUES (1, ?)",
                (layout["version"],),
            )
            db.executemany(
                "INSERT INTO dashboard_layout "
                "(widget_id, position, x, y, w, h, visible) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        widget["id"],
                        position,
                        widget["x"],
                        widget["y"],
                        widget["w"],
                        widget["h"],
                        1 if widget["visible"] else 0,
                    )
                    for position, widget in enumerate(widgets)
                ],
            )

    def clear_dashboard_layout(self) -> None:
        """Remove the stored layout; a missing layout is not an error (idempotent reset-to-default)."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM dashboard_layout")
            db.execute("DELETE FROM dashboard_layout_meta")

    def prune_consumption_daily(self, cutoff_date: str) -> None:
        """Drop day totals older than the ISO local date ``cutoff_date``."""
        with self.connect() as db:
            db.execute(
                "DELETE FROM consumption_daily_history WHERE local_date < ?",
                (cutoff_date,),
            )

    def prune_consumption_intervals(self, cutoff_start: str) -> None:
        """Drop consumption data older than ``cutoff_start`` (inclusive-exclusive at the cutoff).

        Scoped to the consumption tables only, so retention never touches price or solar-forecast
        rows. ``cutoff_start`` is a UTC ISO-8601 instant.
        """
        with self.connect() as db:
            db.execute(
                "DELETE FROM consumption_history_intervals WHERE interval_start < ?",
                (cutoff_start,),
            )
            db.execute(
                "DELETE FROM consumption_meter_intervals WHERE interval_start < ?",
                (cutoff_start,),
            )
            db.execute(
                "DELETE FROM consumption_counter_readings WHERE measured_at < ?",
                (cutoff_start,),
            )
            db.execute(
                "DELETE FROM consumption_applied_batches WHERE batch_time < ?",
                (cutoff_start,),
            )
            db.execute(
                "DELETE FROM consumption_forecast_values WHERE created_at < ?",
                (cutoff_start,),
            )
            db.execute(
                "DELETE FROM consumption_forecast_runs WHERE created_at < ?",
                (cutoff_start,),
            )

    def merge_operator_settings(self, current, changes: dict):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT encrypted FROM settings WHERE key='operator_settings'"
            ).fetchone()
            saved = self._decode("operator_settings", row["encrypted"]) if row else {}
            merged = {**saved, **changes}
            updated = Settings.model_validate({**current.model_dump(), **merged})
            db.execute(
                _UPSERT_SETTING,
                ("operator_settings", self._encode("operator_settings", merged)),
            )
            return updated

    def verify_password(self, username: str, password: str) -> bool:
        if len(password.encode()) > 4096:
            return False
        with self.connect() as db:
            row = db.execute(
                "SELECT encrypted FROM admin_user WHERE username=?", (username,)
            ).fetchone()
        try:
            password_hash = (
                self._decode(username, row["encrypted"])["password_hash"]
                if row
                else _DUMMY_PASSWORD_HASH
            )
            verified = _PASSWORDS.verify(password_hash, password)
            return bool(row is not None and verified)
        except (*_PASSWORD_ERRORS, KeyError, TypeError):
            # An unreadable record is a failed login (counted by the caller), never a 500.
            return False

    def change_password(self, current: str, new: str) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT encrypted FROM admin_user WHERE username='admin'"
            ).fetchone()
            if row is None:
                return False
            try:
                record = self._decode("admin", row["encrypted"])
                if not _PASSWORDS.verify(record["password_hash"], current):
                    return False
            except _PASSWORD_ERRORS:
                return False
            hashed = _PASSWORDS.hash(new)
            updated = {"password_hash": hashed, "must_change": False}
            # A later regular password change must never drop an already-given disclaimer acceptance.
            accepted_at = record.get("disclaimer_accepted_at")
            if accepted_at is not None:
                updated["disclaimer_accepted_at"] = accepted_at
            db.execute(
                "UPDATE admin_user SET encrypted=? WHERE username='admin'",
                (self._encode("admin", updated),),
            )
            db.execute("DELETE FROM sessions")
        return True

    def password_change_pending(self) -> bool:
        """True while the admin still uses the generated bootstrap password."""
        with self.connect() as db:
            row = db.execute(
                "SELECT encrypted FROM admin_user WHERE username='admin'"
            ).fetchone()
        return row is None or bool(
            self._decode("admin", row["encrypted"])["must_change"]
        )

    def disclaimer_accepted_at(self) -> str | None:
        """The stored UTC acceptance timestamp, or ``None`` while the disclaimer is not accepted."""
        with self.connect() as db:
            row = db.execute(
                "SELECT encrypted FROM admin_user WHERE username='admin'"
            ).fetchone()
        if row is None:
            return None
        return self._decode("admin", row["encrypted"]).get("disclaimer_accepted_at")

    def disclaimer_required(self) -> bool:
        """True while no acceptance timestamp is stored (a missing field counts as not accepted)."""
        return self.disclaimer_accepted_at() is None

    def accept_disclaimer(self) -> str:
        """Persist the mandatory disclaimer acceptance; server-generated timestamp, idempotent.

        A second call keeps and returns the first stored timestamp instead of overwriting it, so a
        repeated accept request can never corrupt or reset the recorded consent.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT encrypted FROM admin_user WHERE username='admin'"
            ).fetchone()
            if row is None:
                raise ValueError("admin user is not initialized")
            record = self._decode("admin", row["encrypted"])
            existing = record.get("disclaimer_accepted_at")
            if existing is not None:
                return existing
            accepted_at = _utc_timestamp()
            record["disclaimer_accepted_at"] = accepted_at
            db.execute(
                "UPDATE admin_user SET encrypted=? WHERE username='admin'",
                (self._encode("admin", record),),
            )
        return accepted_at

    def new_session(self) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
        digest = self._digest(token)
        with self.connect() as db:
            lifetime = self._session_lifetime_seconds(db)
            self._purge_sessions(db, lifetime)
            issued_at = int(time.time())
            db.execute(
                "INSERT INTO sessions(digest,encrypted) VALUES (?,?)",
                (
                    digest,
                    self._encode(
                        digest,
                        {
                            "csrf_digest": self._digest(csrf),
                            "issued_at": issued_at,
                            "expires_at": issued_at + lifetime if lifetime else None,
                        },
                    ),
                ),
            )
        return token, csrf

    def _session_lifetime_seconds(self, db: sqlite3.Connection) -> int:
        row = db.execute(
            "SELECT encrypted FROM settings WHERE key='operator_settings'"
        ).fetchone()
        saved = self._decode("operator_settings", row["encrypted"]) if row else {}
        days = saved.get("session_max_days", _DEFAULT_SESSION_DAYS)
        return max(0, min(int(days), 7)) * 86400

    @staticmethod
    def _session_expired(payload: dict, now: int, lifetime: int) -> bool:
        expires_at = payload["expires_at"]
        if lifetime and "issued_at" in payload:
            current_limit = payload["issued_at"] + lifetime
            expires_at = (
                min(expires_at, current_limit)
                if expires_at is not None
                else current_limit
            )
        return expires_at is not None and expires_at <= now

    def _purge_sessions(self, db: sqlite3.Connection, lifetime: int) -> None:
        now = int(time.time())
        for row in db.execute("SELECT digest, encrypted FROM sessions").fetchall():
            try:
                expired = self._session_expired(
                    self._decode(row["digest"], row["encrypted"]), now, lifetime
                )
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
            user = db.execute(
                "SELECT encrypted FROM admin_user WHERE username='admin'"
            ).fetchone()
            lifetime = self._session_lifetime_seconds(db)
        if row is None or user is None:
            return None
        try:
            payload = self._decode(self._digest(token), row["encrypted"])
            admin_record = self._decode("admin", user["encrypted"])
            must_change = bool(admin_record["must_change"])
        except ValueError:
            return None  # tampered or swapped records never authenticate
        if self._session_expired(payload, int(time.time()), lifetime):
            return None
        accepted_at = admin_record.get("disclaimer_accepted_at")
        return {
            "must_change_password": must_change,
            "disclaimer_required": accepted_at is None,
            "disclaimer_accepted_at": accepted_at,
            "csrf_digest": payload["csrf_digest"],
        }

    def verify_csrf(self, session: dict, submitted: str | None) -> bool:
        return bool(
            submitted
            and hmac.compare_digest(session["csrf_digest"], self._digest(submitted))
        )

    def delete_session(self, token: str | None) -> None:
        if token:
            with self.connect() as db:
                db.execute(
                    "DELETE FROM sessions WHERE digest=?", (self._digest(token),)
                )

    def list_tokens(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,encrypted FROM pats").fetchall()
        records = [
            {
                "last_used_at": None,
                "suffix": None,
                **self._decode(row["id"], row["encrypted"]),
            }
            for row in rows
        ]
        return sorted(records, key=lambda row: row["created_at"], reverse=True)

    def create_token(
        self, name: str, role: str, expires_at: datetime | None
    ) -> tuple[dict, str]:
        with self.connect() as db:
            db.execute(
                "BEGIN IMMEDIATE"
            )  # the 32-token cap is count-then-insert: take the write lock first
            return self._insert_token(db, name, role, expires_at)

    def delete_token(self, token_id: str) -> bool:
        with self.connect() as db:
            deleted = bool(
                db.execute("DELETE FROM pats WHERE id=?", (token_id,)).rowcount
            )
        if deleted:
            with self._touch_lock:
                self._last_touch.pop(token_id, None)
        return deleted

    def authenticate_token(self, token: str) -> FileToken | None:
        if len(token) > 512 or not pat_well_formed(token):
            return None
        sha256 = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM pats WHERE digest=?", (self._pat_digest(sha256),)
            ).fetchone()
        if row is None:
            return None
        try:
            record = self._decode(row["id"], row["encrypted"])
        except ValueError:
            log.warning(
                "Rejected an unreadable token record"
            )  # tampered or encrypted under another HMAC_SECRET
            return None
        if record["expires_at"] and datetime.fromisoformat(
            record["expires_at"]
        ) <= datetime.now(UTC):
            return None
        self._note_use(record)
        return FileToken(record["id"], record["role"])

    def _note_use(self, record: dict) -> None:
        """Record the last use at most once per interval, off the caller's thread; never raises."""
        try:
            now = time.monotonic()
            with self._touch_lock:
                if (
                    now - self._last_touch.get(record["id"], -_TOUCH_SECONDS)
                    < _TOUCH_SECONDS
                ):
                    return
                stored = record.get("last_used_at")
                recent = (
                    bool(stored)
                    and (
                        datetime.now(UTC) - datetime.fromisoformat(stored)
                    ).total_seconds()
                    < _TOUCH_SECONDS
                )
                self._last_touch[record["id"]] = now
            if not recent:
                self._touch_pool.submit(self._write_last_used, record["id"])
        except Exception:
            log.debug("Could not schedule the last-used update", exc_info=True)

    def _write_last_used(self, token_id: str) -> None:
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT encrypted FROM pats WHERE id=?", (token_id,)
                ).fetchone()
                if row is None:  # revoked meanwhile
                    return
                record = self._decode(token_id, row["encrypted"])
                record["last_used_at"] = datetime.now(UTC).isoformat()
                db.execute(
                    "UPDATE pats SET encrypted=? WHERE id=?",
                    (self._encode(token_id, record), token_id),
                )
        except Exception:
            log.debug("Could not store the last-used time of a token", exc_info=True)
