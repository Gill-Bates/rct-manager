#!/usr/bin/env python3
#
# app/dispatch/store.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Crash-durable encrypted SQLite store for dispatch intent (REQ-111..REQ-115)."""

import base64
import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.dispatch.capabilities import CapabilityRecord
from app.dispatch.models import DeviceLimits, DispatchRecord, DispatchRecordCorrupt
from app.dispatch.soc_policy import SocTargetMode, SocTargetPolicy
from app.energy.models import ArmedRecord

log = logging.getLogger(__name__)

# user_version 2 adds the per-device capability and device-configuration tables. `status` and
# `engineering_mode` stay in cleartext for the same reason dispatch_operations keeps `state` and
# `restore_required` there: the two safety-deciding facts — is this device released, and does it
# run in engineering mode — must remain readable when the service is down or HMAC_SECRET was
# rotated. They are state names, not secrets.
_SCHEMA_V2 = """
    CREATE TABLE dispatch_capabilities (
        device_id TEXT NOT NULL,
        name      TEXT NOT NULL,
        status    TEXT NOT NULL,
        encrypted BLOB NOT NULL,
        PRIMARY KEY (device_id, name)
    ) STRICT;

    CREATE TABLE dispatch_device_config (
        device_id        TEXT PRIMARY KEY,
        engineering_mode INTEGER NOT NULL CHECK(engineering_mode IN (0,1)),
        encrypted        BLOB NOT NULL
    ) STRICT;
    PRAGMA user_version = 2;
"""

# user_version 3 is strictly additive as well: it adds the Energy Manager's armed state and the
# per-device SoC-target derivation policy. `armed` and `mode` stay in cleartext for the same
# documented reason `state`, `status` and `engineering_mode` do — an operator must be able to see
# whether a device is armed, and how its target is derived, while the service is down or after an
# HMAC_SECRET rotation. They are state names, not secrets.
_SCHEMA_V3 = """
    CREATE TABLE energy_manager_state (
        device_id TEXT PRIMARY KEY,
        armed     INTEGER NOT NULL CHECK(armed IN (0,1)),
        encrypted BLOB NOT NULL
    ) STRICT;

    CREATE TABLE dispatch_soc_target_policy (
        device_id TEXT PRIMARY KEY,
        mode      TEXT NOT NULL,
        encrypted BLOB NOT NULL
    ) STRICT;
    PRAGMA user_version = 3;
"""


class DispatchStore:
    def __init__(self, path: Path, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("HMAC_SECRET must contain at least 32 characters")
        self.path = path
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"rct-dispatch-db-v1").derive(
            secret.encode()
        )
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        db.execute("PRAGMA synchronous = FULL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, 1, 2, 3):
                raise ValueError("unsupported dispatch database version")
            if version == 0:
                db.executescript("""
                    CREATE TABLE dispatch_operations (
                        device_id TEXT PRIMARY KEY,
                        state TEXT NOT NULL,
                        restore_required INTEGER NOT NULL CHECK(restore_required IN (0,1)),
                        updated_at TEXT NOT NULL,
                        record_version INTEGER NOT NULL,
                        encrypted BLOB NOT NULL
                    ) STRICT;
                """)
            if version in (0, 1):
                # Upgrade 1 -> 2 adds the two capability tables and nothing else: the schema of
                # dispatch_operations and every row in it stay exactly as they are, so an update
                # cannot lose the restore duty a persisted operation carries.
                db.executescript(_SCHEMA_V2)
            if version in (0, 1, 2):
                # Upgrade 2 -> 3: two new tables and the version bump, nothing else. A code
                # downgrade afterwards is not supported — the older initialize() rejects version 3,
                # so a rollback means restoring the dispatch database from a backup.
                db.executescript(_SCHEMA_V3)

    def _encode(self, device_id: str, record: DispatchRecord, *, version: int | None = None) -> bytes:
        # `version` lets `put()` encode the pending next version without first writing it onto the
        # caller's `record` (see `put()`'s comment): the stored payload must carry the version this
        # write is attempting, even though `record.record_version` is not updated until that write
        # has committed.
        data = record.to_dict()
        if version is not None:
            data["record_version"] = version
        return self._encrypt(device_id, data)

    def _decode(self, device_id: str, encrypted: bytes) -> DispatchRecord:
        return DispatchRecord.from_dict(self._decrypt(device_id, encrypted))

    def _encrypt(self, key: str, data: dict) -> bytes:
        """Bind the payload to the row it belongs to, so a moved blob fails the integrity check."""
        payload = {"key": key, "value": data}
        return self._fernet.encrypt(json.dumps(payload, separators=(",", ":")).encode())

    def _decrypt(self, key: str, encrypted: bytes) -> dict:
        try:
            payload = json.loads(self._fernet.decrypt(encrypted))
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise DispatchRecordCorrupt("dispatch database cannot be decrypted with HMAC_SECRET") from exc
        if not isinstance(payload, dict) or payload.get("key") != key:
            raise DispatchRecordCorrupt("dispatch database record integrity check failed")
        return payload["value"]

    def get(self, device_id: str) -> DispatchRecord | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT state,restore_required,record_version,encrypted FROM dispatch_operations WHERE device_id=?",
                (device_id,),
            ).fetchone()
        if row is None:
            return None
        record = self._decode(device_id, row["encrypted"])
        record.record_version = int(row["record_version"])
        return record

    def all(self) -> list[DispatchRecord]:
        return self.all_with_skipped()[0]

    def all_with_skipped(self) -> tuple[list[DispatchRecord], list[str]]:
        """Every readable row. A row that fails ``DispatchRecord.from_dict()`` is skipped, not
        raised: a single corrupt record must not block ``recover()``/``shutdown_restore()`` from
        reaching every other device's restore duty. The caller is responsible for surfacing the
        skipped device_id to an operator.
        """
        with self.connect() as db:
            rows = db.execute("SELECT device_id,record_version,encrypted FROM dispatch_operations").fetchall()
        records = []
        skipped = []
        for row in rows:
            try:
                record = self._decode(row["device_id"], row["encrypted"])
            except ValueError:
                log.error("Dispatch record for device %s is unreadable and was skipped", row["device_id"])
                skipped.append(row["device_id"])
                continue
            record.record_version = int(row["record_version"])
            records.append(record)
        return records, skipped

    def put(self, record: DispatchRecord) -> None:
        # The caller-supplied `record` must only be mutated after the write durably commits: a
        # rejected CAS (stale version) must leave it exactly as passed in, so a caller that blindly
        # retries the same object does not fall through a check it would otherwise fail against the
        # version it actually started from. The encoded payload therefore carries `next_version`
        # without touching `record.record_version` yet; the field is updated on `record` itself only
        # after the INSERT/UPDATE below has run without raising, i.e. once SQLite has accepted it
        # (still inside `connect()`'s transaction, which commits on clean exit).
        next_version = record.record_version + 1
        encrypted = self._encode(record.device_id, record, version=next_version)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT record_version FROM dispatch_operations WHERE device_id=?", (record.device_id,)
            ).fetchone()
            if current is not None and int(current[0]) != next_version - 1:
                raise RuntimeError("stale dispatch record")
            db.execute(
                """INSERT INTO dispatch_operations(device_id,state,restore_required,updated_at,record_version,encrypted)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(device_id) DO UPDATE SET state=excluded.state,
                       restore_required=excluded.restore_required,updated_at=excluded.updated_at,
                       record_version=excluded.record_version,encrypted=excluded.encrypted""",
                (
                    record.device_id,
                    record.state.value,
                    int(record.restore_required),
                    datetime.now(UTC).isoformat(),
                    next_version,
                    encrypted,
                ),
            )
        record.record_version = next_version

    def get_capabilities(self) -> list[CapabilityRecord]:
        with self.connect() as db:
            rows = db.execute("SELECT device_id,name,encrypted FROM dispatch_capabilities").fetchall()
        return [
            CapabilityRecord.from_dict(self._decrypt(f"{row['device_id']}/{row['name']}", row["encrypted"]))
            for row in rows
        ]

    def put_capability(self, record: CapabilityRecord) -> None:
        self.put_capabilities([record])

    def put_capabilities(self, records: list[CapabilityRecord]) -> None:
        """Persist several capability rows in one transaction: all of them or none."""
        rows = [
            (r.device_id, r.name.value, r.status.value, self._encrypt(f"{r.device_id}/{r.name.value}", r.to_dict()))
            for r in records
        ]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                """INSERT INTO dispatch_capabilities(device_id,name,status,encrypted)
                   VALUES(?,?,?,?)
                   ON CONFLICT(device_id,name) DO UPDATE SET status=excluded.status,
                       encrypted=excluded.encrypted""",
                rows,
            )

    def get_device_configs(self) -> dict[str, DeviceLimits]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT device_id,engineering_mode,encrypted FROM dispatch_device_config"
            ).fetchall()
        configs = {}
        for row in rows:
            data = self._decrypt(row["device_id"], row["encrypted"])
            configs[row["device_id"]] = DeviceLimits(
                max_charge_power_w=float(data["max_charge_power_w"]),
                max_discharge_power_w=float(data["max_discharge_power_w"]),
                engineering_mode=bool(row["engineering_mode"]),
            )
        return configs

    def put_device_config(self, device_id: str, limits: DeviceLimits) -> None:
        encrypted = self._encrypt(
            device_id,
            {
                "max_charge_power_w": limits.max_charge_power_w,
                "max_discharge_power_w": limits.max_discharge_power_w,
            },
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO dispatch_device_config(device_id,engineering_mode,encrypted)
                   VALUES(?,?,?)
                   ON CONFLICT(device_id) DO UPDATE SET engineering_mode=excluded.engineering_mode,
                       encrypted=excluded.encrypted""",
                (device_id, int(limits.engineering_mode), encrypted),
            )

    def get_energy_states(self) -> dict[str, ArmedRecord]:
        """Every readable armed record. A row that cannot be decrypted is skipped and reported, so
        the device reads as *not armed* — fail closed, never silently defaulted to armed.
        """
        with self.connect() as db:
            rows = db.execute("SELECT device_id,armed,encrypted FROM energy_manager_state").fetchall()
        states: dict[str, ArmedRecord] = {}
        for row in rows:
            device_id = row["device_id"]
            try:
                data = self._decrypt(device_id, row["encrypted"])
                armed_at = data.get("armed_at")
                states[device_id] = ArmedRecord(
                    device_id=device_id,
                    armed=bool(row["armed"]),
                    added_write_names=tuple(data.get("added_write_names") or ()),
                    armed_at=datetime.fromisoformat(armed_at) if armed_at else None,
                    armed_by=data.get("armed_by"),
                )
            except (ValueError, TypeError):
                log.error(
                    "Energy manager state for device %s is unreadable and was skipped; the device "
                    "reads as not armed",
                    device_id,
                )
        return states

    def put_energy_state(self, record: ArmedRecord) -> None:
        encrypted = self._encrypt(
            record.device_id,
            {
                "added_write_names": list(record.added_write_names),
                "armed_at": record.armed_at.isoformat() if record.armed_at is not None else None,
                "armed_by": record.armed_by,
            },
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO energy_manager_state(device_id,armed,encrypted)
                   VALUES(?,?,?)
                   ON CONFLICT(device_id) DO UPDATE SET armed=excluded.armed,
                       encrypted=excluded.encrypted""",
                (record.device_id, int(record.armed), encrypted),
            )

    def get_soc_target_policies(self) -> dict[str, SocTargetPolicy]:
        """Every readable derivation policy. An unreadable row is skipped and reported, so the
        device falls back to the shipped ``BUSINESS_TARGET`` default (today's behaviour).
        """
        with self.connect() as db:
            rows = db.execute("SELECT device_id,mode,encrypted FROM dispatch_soc_target_policy").fetchall()
        policies: dict[str, SocTargetPolicy] = {}
        for row in rows:
            device_id = row["device_id"]
            try:
                data = self._decrypt(device_id, row["encrypted"])
                policies[device_id] = SocTargetPolicy(
                    device_id=device_id,
                    mode=SocTargetMode(row["mode"]),
                    below_margin_percent=float(data["below_margin_percent"]),
                    note=data.get("note"),
                )
            except (ValueError, TypeError, KeyError):
                log.error(
                    "SoC target policy for device %s is unreadable and was skipped; the device "
                    "falls back to the business target default",
                    device_id,
                )
        return policies

    def put_soc_target_policy(self, policy: SocTargetPolicy) -> None:
        encrypted = self._encrypt(
            policy.device_id,
            {"below_margin_percent": policy.below_margin_percent, "note": policy.note},
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO dispatch_soc_target_policy(device_id,mode,encrypted)
                   VALUES(?,?,?)
                   ON CONFLICT(device_id) DO UPDATE SET mode=excluded.mode,
                       encrypted=excluded.encrypted""",
                (policy.device_id, policy.mode.value, encrypted),
            )

    def delete(self, device_id: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM dispatch_operations WHERE device_id=?", (device_id,))

    def close(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
