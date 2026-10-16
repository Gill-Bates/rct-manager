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
from app.energy.models import EnergyMode, ModeRecord

log = logging.getLogger(__name__)

# The baseline dispatch schema. The application is unreleased: there is no deployed database to
# migrate, so each table is created directly in its current shape, with no version stepping. The
# safety-deciding facts (`status`, `engineering_mode`, energy `mode`, SoC-target `mode`) stay in
# cleartext columns so they remain readable when the service is down or HMAC_SECRET was rotated.
# Each is a diagnostic mirror only: the same fact is ALSO carried in the authenticated blob, which
# the read trusts and cross-checks, so a cleartext edit cannot forge it without the Fernet key.
#
# Each table is created in its own BEGIN IMMEDIATE ... COMMIT unit, so a crash between two of them
# leaves each table either fully created or absent, never half-built; the next start creates the
# ones still missing. ``initialize()`` creates a table only when it does not exist and otherwise
# checks the on-disk definition: a database whose shape does not match the
# current baseline fails clearly instead of being silently upgraded (there is no migration path).
_SCHEMA: tuple[tuple[str, str], ...] = (
    (
        "dispatch_operations",
        """
        CREATE TABLE dispatch_operations (
            device_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            restore_required INTEGER NOT NULL CHECK(restore_required IN (0,1)),
            updated_at TEXT NOT NULL,
            record_version INTEGER NOT NULL,
            encrypted BLOB NOT NULL
        ) STRICT;
        """,
    ),
    (
        "dispatch_capabilities",
        """
        CREATE TABLE dispatch_capabilities (
            device_id TEXT NOT NULL,
            name      TEXT NOT NULL,
            status    TEXT NOT NULL,
            encrypted BLOB NOT NULL,
            PRIMARY KEY (device_id, name)
        ) STRICT;
        """,
    ),
    (
        "dispatch_device_config",
        """
        CREATE TABLE dispatch_device_config (
            device_id        TEXT PRIMARY KEY,
            engineering_mode INTEGER NOT NULL CHECK(engineering_mode IN (0,1)),
            encrypted        BLOB NOT NULL
        ) STRICT;
        """,
    ),
    (
        "energy_manager_state",
        """
        CREATE TABLE energy_manager_state (
            device_id TEXT PRIMARY KEY,
            mode      TEXT NOT NULL CHECK(mode IN ('off','manual','external')),
            encrypted BLOB NOT NULL
        ) STRICT;
        """,
    ),
    (
        "dispatch_soc_target_policy",
        """
        CREATE TABLE dispatch_soc_target_policy (
            device_id TEXT PRIMARY KEY,
            mode      TEXT NOT NULL,
            encrypted BLOB NOT NULL
        ) STRICT;
        """,
    ),
)

def _schema_sql(sql: str) -> str:
    return sql.strip().rstrip(";")


def _authenticated_bool(data: dict, key: str) -> bool:
    """Read a safety-deciding bool from the authenticated blob, fail closed.

    A missing key raises KeyError and a present-but-non-bool value raises TypeError rather than
    being coerced, so a tampered or malformed blob is treated as unreadable (the caller skips the
    row to its safe default) instead of being silently trusted.
    """
    value = data[key]
    if not isinstance(value, bool):
        raise TypeError(f"authenticated {key} must be a bool, got {type(value).__name__}")
    return value


def _authenticated_str(data: dict, key: str) -> str:
    """Read a safety-deciding string (an enum value) from the authenticated blob, fail closed.

    Same contract as ``_authenticated_bool``: a missing key is a row to skip, a non-string is
    corruption, not a value to coerce.
    """
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"authenticated {key} must be a str, got {type(value).__name__}")
    return value


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
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 5000")
            db.execute("PRAGMA synchronous = FULL")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        with self.connect() as db:
            if db.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() != "wal":
                log.warning("SQLite WAL mode is unavailable; dispatch store is using the rollback journal")
            # Create the current baseline schema directly; there is no migration from an older
            # layout. A table that already exists with the expected definition is left as is, so a
            # normal restart is a no-op; one that exists with a different shape (an incompatible
            # old dev database) fails clearly here — the developer must delete it before start.
            for table, create in _SCHEMA:
                row = db.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone()
                if row is None:
                    # executescript() commits any pending transaction first, so the BEGIN/COMMIT
                    # has to live inside the script to make this one CREATE its own atomic unit.
                    db.executescript(f"BEGIN IMMEDIATE;{create}COMMIT;")
                elif _schema_sql(row["sql"]) != _schema_sql(create):
                    raise ValueError(
                        f"dispatch database table {table!r} does not match the current schema; "
                        "this build does not migrate an older database — delete it and restart"
                    )

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
        """Every readable capability row. An unreadable row is skipped and reported, so that
        capability reads as unverified — fail closed — and one bad row blocks no other device.
        """
        with self.connect() as db:
            rows = db.execute("SELECT device_id,name,encrypted FROM dispatch_capabilities").fetchall()
        records = []
        for row in rows:
            try:
                records.append(
                    CapabilityRecord.from_dict(self._decrypt(f"{row['device_id']}/{row['name']}", row["encrypted"]))
                )
            except (ValueError, TypeError, KeyError):
                log.error(
                    "Capability %s of device %s is unreadable and was skipped; it reads as unverified",
                    row["name"], row["device_id"],
                )
        return records

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
            try:
                data = self._decrypt(row["device_id"], row["encrypted"])
                # engineering_mode is authoritative from the authenticated blob, never from the
                # cleartext column. A missing key raises KeyError and the row is skipped —
                # engineering mode then reads as off, fail closed. A cleartext column tampered out
                # of step with the blob fails the equality check below, so a forged release of
                # dispatch onto unverified hardware reads as off instead.
                engineering_mode = _authenticated_bool(data, "engineering_mode")
                if engineering_mode != bool(row["engineering_mode"]):
                    raise ValueError("engineering_mode integrity mismatch")
                # usable_capacity_wh is optional: a blob written before this field carries no key,
                # which reads back as "no capacity set" (None) — the device then fails closed in the
                # Dry-Run plan, never defaulted to a guessed value. A present-but-null value is the
                # same "unset". DeviceLimits.__post_init__ rejects a non-finite/<=0 stored value.
                raw_capacity = data.get("usable_capacity_wh")
                usable_capacity_wh = None if raw_capacity is None else float(raw_capacity)
                configs[row["device_id"]] = DeviceLimits(
                    max_charge_power_w=float(data["max_charge_power_w"]),
                    max_discharge_power_w=float(data["max_discharge_power_w"]),
                    engineering_mode=engineering_mode,
                    usable_capacity_wh=usable_capacity_wh,
                )
            except (ValueError, TypeError, KeyError):
                log.error(
                    "Device configuration for %s is unreadable and was skipped; the environment limits "
                    "apply and engineering mode stays off",
                    row["device_id"],
                )
        return configs

    def put_device_config(self, device_id: str, limits: DeviceLimits) -> None:
        # engineering_mode is mirrored into the authenticated blob as well as its cleartext column:
        # the cleartext stays for offline diagnostics, but the blob copy is what the read trusts.
        # Flipping only the cleartext 0->1 (no Fernet key) then fails the cross-check in
        # get_device_configs(), so the switch that releases dispatch onto unverified hardware cannot
        # be forged by editing the SQLite row.
        encrypted = self._encrypt(
            device_id,
            {
                "max_charge_power_w": limits.max_charge_power_w,
                "max_discharge_power_w": limits.max_discharge_power_w,
                "engineering_mode": limits.engineering_mode,
                "usable_capacity_wh": limits.usable_capacity_wh,
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

    def get_energy_states(self) -> dict[str, ModeRecord]:
        """Every readable mode record. A row that cannot be decrypted or carries an unknown mode is
        skipped and reported, so the device reads as mode ``off`` — fail closed, never silently
        defaulted to a mode that accepts commands.
        """
        with self.connect() as db:
            rows = db.execute("SELECT device_id,mode,encrypted FROM energy_manager_state").fetchall()
        states: dict[str, ModeRecord] = {}
        for row in rows:
            device_id = row["device_id"]
            try:
                data = self._decrypt(device_id, row["encrypted"])
                changed_at = data.get("changed_at")
                # `mode` is authoritative from the authenticated blob, never from the cleartext
                # column. A missing `mode` key raises KeyError and the row is skipped, so it reads
                # as mode off (fail closed). A cleartext column edited out of step with the blob
                # fails the equality check, so a device cannot be switched on by editing the row.
                mode = EnergyMode(_authenticated_str(data, "mode"))
                if mode is not EnergyMode(row["mode"]):
                    raise ValueError("energy mode integrity mismatch")
                states[device_id] = ModeRecord(
                    device_id=device_id,
                    mode=mode,
                    added_write_names=tuple(data.get("added_write_names") or ()),
                    changed_at=datetime.fromisoformat(changed_at) if changed_at else None,
                    changed_by=data.get("changed_by"),
                )
            except (ValueError, TypeError, KeyError):
                log.error(
                    "Energy manager state for device %s is unreadable and was skipped; the device "
                    "reads as mode off",
                    device_id,
                )
        return states

    def put_energy_state(self, record: ModeRecord) -> None:
        # `mode` is mirrored into the authenticated blob as well as its cleartext column: the
        # cleartext stays readable while the service is down, but get_energy_states() trusts the
        # blob copy and cross-checks it. Editing only the cleartext column (e.g. 'off' -> 'manual'
        # to make a device accept commands) then fails the check and the row reads as mode off.
        encrypted = self._encrypt(
            record.device_id,
            {
                "mode": record.mode.value,
                "added_write_names": list(record.added_write_names),
                "changed_at": record.changed_at.isoformat() if record.changed_at is not None else None,
                "changed_by": record.changed_by,
            },
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO energy_manager_state(device_id,mode,encrypted)
                   VALUES(?,?,?)
                   ON CONFLICT(device_id) DO UPDATE SET mode=excluded.mode,
                       encrypted=excluded.encrypted""",
                (record.device_id, record.mode.value, encrypted),
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
                # `mode` is authoritative from the authenticated blob, never from the cleartext
                # column. A missing `mode` key raises KeyError and the row is skipped, so it reads
                # as the business-target default (fail closed). A cleartext column edited out of
                # step with the blob fails the equality check.
                mode = SocTargetMode(_authenticated_str(data, "mode"))
                if mode is not SocTargetMode(row["mode"]):
                    raise ValueError("soc target mode integrity mismatch")
                policies[device_id] = SocTargetPolicy(
                    device_id=device_id,
                    mode=mode,
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
        # `mode` is mirrored into the authenticated blob as well as its cleartext column: the
        # cleartext stays readable offline, but get_soc_target_policies() trusts and cross-checks
        # the blob copy, so the derivation policy that steers the device-level SoC target cannot be
        # changed by editing the SQLite row without the Fernet key.
        encrypted = self._encrypt(
            policy.device_id,
            {
                "mode": policy.mode.value,
                "below_margin_percent": policy.below_margin_percent,
                "note": policy.note,
            },
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

    def reset_identity_bound_state(self, device_ids: list[str]) -> None:
        """Clear every identity-bound persistent fact of each device in ONE transaction.

        A device-list reconfiguration that frees or re-addresses a device_id must not let the old
        physical device's capability evidence, engineering-mode switch or SoC-target policy survive
        onto whatever device now answers at that id. Done row-by-row across the three tables, a
        failure partway leaves a persisted mix (e.g. capabilities wiped but the policy kept) while
        the old graph still runs — the reconfiguration cannot roll that back. One transaction makes
        it all-or-nothing, so the in-memory registries can be updated after the commit returns.

        Capabilities and the SoC-target policy are deleted: an absent row is exactly what the
        in-memory registries read as the shipped default (unverified capability, business-target
        policy), so the persisted and the published state agree. The device-configuration row is
        kept but its engineering-mode switch is cleared, because the power limits in it are a
        site/installation property the operator set, not a hardware-identity claim — only the
        engineering switch is identity-bound. Engineering mode lives in BOTH the cleartext column
        and the authenticated blob, and ``get_device_configs`` trusts the blob and discards the whole
        row (power limits included) on a mismatch. Clearing only the cleartext column would therefore
        make a device that had engineering mode on lose its preserved limits on the next read, so the
        reset rewrites both representations together: the blob is re-encrypted with the same limits
        and ``engineering_mode`` false.
        """
        if not device_ids:
            return
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for device_id in device_ids:
                db.execute("DELETE FROM dispatch_capabilities WHERE device_id=?", (device_id,))
                db.execute("DELETE FROM dispatch_soc_target_policy WHERE device_id=?", (device_id,))
                self._clear_engineering_mode(db, device_id)

    def _clear_engineering_mode(self, db: sqlite3.Connection, device_id: str) -> None:
        """Clear engineering mode in both the cleartext column and the authenticated blob, keeping
        the stored power limits. A missing row updates nothing; a row whose blob cannot be decrypted
        or lacks the limits is left exactly as it is so it keeps failing closed on read instead of
        being rewritten into a readable row with assumed values.
        """
        row = db.execute(
            "SELECT encrypted FROM dispatch_device_config WHERE device_id=?", (device_id,)
        ).fetchone()
        if row is None:
            return
        try:
            data = self._decrypt(device_id, row["encrypted"])
            limits = {
                "max_charge_power_w": float(data["max_charge_power_w"]),
                "max_discharge_power_w": float(data["max_discharge_power_w"]),
            }
            # Usable capacity is a site/installation property like the power limits, not a
            # hardware-identity claim, so it is preserved across an identity reset. Absent in an
            # older blob means it stays unset.
            raw_capacity = data.get("usable_capacity_wh")
            if raw_capacity is not None:
                limits["usable_capacity_wh"] = float(raw_capacity)
        except (DispatchRecordCorrupt, ValueError, TypeError, KeyError):
            return
        encrypted = self._encrypt(device_id, {**limits, "engineering_mode": False})
        db.execute(
            "UPDATE dispatch_device_config SET engineering_mode=0, encrypted=? WHERE device_id=?",
            (encrypted, device_id),
        )

    def delete(self, device_id: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM dispatch_operations WHERE device_id=?", (device_id,))

    def close(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
