#!/usr/bin/env python3
#
# app/export/questdb_admin.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""QuestDB OSS provisioning: table, TTL and materialized-view rollups.

Approach ported from fritzfluxdb: a TTL that exists and was not set by this service belongs to
the administrator and is never replaced; the raw TTL only shortens after the rollup is current.
"""

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

STATE_TABLE = "_rct_export_state"
_TTL_UNIT_HOURS = {"HOUR": 1, "DAY": 24, "WEEK": 24 * 7, "MONTH": 24 * 30, "YEAR": 24 * 365}
_COUNTER_SUFFIXES = ("_total", "_sum", "_count")


@dataclass(frozen=True, slots=True)
class Preset:
    raw_retention_days: int
    rollup_interval: str


PRESETS: dict[str, Preset] = {
    "low": Preset(30, "1m"),
    "medium": Preset(7, "1m"),
    "high": Preset(1, "5m"),
}

Execute = Callable[[str], dict]


def ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def effective_raw_days(downsampling: str, raw_days: int | None, total_days: int) -> int | None:
    """Days of raw data; manual uses its explicit TTL, while off uses the total TTL."""
    if downsampling == "manual":
        if raw_days is None:
            raise ValueError("manual downsampling requires raw retention days")
        return raw_days
    preset = PRESETS.get(downsampling)
    if preset is None:
        return None
    days = raw_days if raw_days is not None else preset.raw_retention_days
    return min(days, total_days) if total_days > 0 else days  # an explicit raw value is validated against total


def _split_columns(columns: dict[str, str]) -> tuple[list[str], list[str]]:
    """Sorted SYMBOL (dimension) and DOUBLE (value) column names."""
    dims = sorted(name for name, kind in columns.items() if kind == "SYMBOL")
    values = sorted(name for name, kind in columns.items() if kind == "DOUBLE")
    return dims, values


def schema_signature(columns: dict[str, str]) -> str:
    """Short, stable hash of the SYMBOL/DOUBLE columns a rollup view projects.

    Tying the view name to this signature instead of a constant version makes a newly exported
    metric (a DOUBLE column Line Protocol adds after a restart) produce a new view automatically;
    the old view is never silently kept pointed at a stale projection (Requirement: no data loss
    once the raw TTL is shortened).
    """
    dims, values = _split_columns(columns)
    digest = hashlib.sha256(("|".join(dims) + "::" + "|".join(values)).encode()).hexdigest()
    return digest[:10]


def rollup_view_name(table: str, preset: Preset, schema_hash: str) -> str:
    return f"{table}_rollup_{preset.rollup_interval}_v{schema_hash}"


def create_table_sql(table: str) -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS {ident(table)} (timestamp TIMESTAMP, device SYMBOL, endpoint SYMBOL, "
        "state SYMBOL, metric SYMBOL) TIMESTAMP(timestamp) PARTITION BY DAY;"
    )


def create_state_table_sql() -> str:
    return (
        f"CREATE TABLE IF NOT EXISTS {ident(STATE_TABLE)} (timestamp TIMESTAMP, measurement SYMBOL, "
        "object SYMBOL, ttl_days LONG) TIMESTAMP(timestamp) PARTITION BY DAY;"
    )


def rollup_ddl(table: str, preset: Preset, columns: dict[str, str], view: str) -> str:
    """Materialized view over the symbol and DOUBLE columns present in the table."""
    dims, values = _split_columns(columns)
    if not values:
        raise ValueError("no numeric columns to roll up yet")
    projections = ["timestamp", *(ident(name) for name in dims)]
    for name in values:
        if name.endswith(_COUNTER_SUFFIXES):
            projections.append(f"last({ident(name)}) AS {ident(name + '_last')}")
        else:
            for aggregate in ("avg", "min", "max", "last"):
                projections.append(f"{aggregate}({ident(name)}) AS {ident(f'{name}_{aggregate}')}")
    select_list = ",\n        ".join(projections)
    return (
        f"CREATE MATERIALIZED VIEW IF NOT EXISTS {ident(view)} "
        f"WITH BASE {ident(table)} REFRESH IMMEDIATE AS (\n"
        f"    SELECT\n        {select_list}\n"
        f"    FROM {ident(table)}\n"
        f"    SAMPLE BY {preset.rollup_interval} ALIGN TO CALENDAR\n"
        ") PARTITION BY DAY;"
    )


def ttl_days(value: object, unit: object) -> int | None:
    """Whole days of a QuestDB TTL (months and years approximated); None for an unknown unit."""
    try:
        amount = int(value or 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return 0
    hours = _TTL_UNIT_HOURS.get(str(unit or "").upper().rstrip("S"))
    return None if hours is None else amount * hours // 24


class QuestDbProvisioner:
    def __init__(self, execute: Execute, table: str, downsampling: str, raw_days: int | None, total_days: int) -> None:
        self._exec = execute
        self.table = table
        self.downsampling = downsampling
        self.total_days = total_days
        self.raw_days = effective_raw_days(downsampling, raw_days, total_days)

    def ensure_tables(self) -> None:
        self._exec(create_table_sql(self.table))
        self._exec(create_state_table_sql())

    def run(self) -> bool:
        """Apply retention and rollups; False while the rollup is not ready (retry later)."""
        self.ensure_tables()
        preset = PRESETS.get(self.downsampling)
        if preset is None:
            if self.downsampling == "manual":
                return self._apply_ttl(self.table, self.raw_days or 0, view=False)
            return self._apply_ttl(self.table, self.total_days, view=False)
        rows = self._exec(
            f"SELECT \"column\", type FROM table_columns({literal(self.table)});"
        ).get("dataset") or []
        columns = {str(name): str(kind).upper() for name, kind in rows}
        if not any(kind == "DOUBLE" for kind in columns.values()):
            log.info("QuestDB rollup waits for the first data in '%s'", self.table)
            return False
        # The view name follows the column set (see schema_signature).
        view = rollup_view_name(self.table, preset, schema_signature(columns))
        self._exec(rollup_ddl(self.table, preset, columns, view))
        if not self._view_current(view):
            log.warning("QuestDB rollup view '%s' is not refreshed yet; raw TTL is unchanged", view)
            return False
        self._drop_stale_rollups(view)
        # The raw TTL only shortens once the rollup holds the data that it would drop.
        return self._apply_ttl(view, self.total_days, view=True) and self._apply_ttl(
            self.table, self.raw_days or 0, view=False
        )

    def _drop_stale_rollups(self, current: str) -> None:
        """Drop earlier service-created rollup views of this table once ``current`` is up to date."""
        rows = self._exec(
            f"SELECT view_name FROM materialized_views() WHERE base_table_name = {literal(self.table)};"
        ).get("dataset") or []
        prefix = f"{self.table}_rollup_"
        for (name, *_) in rows:
            if str(name).startswith(prefix) and name != current:
                self._exec(f"DROP MATERIALIZED VIEW IF EXISTS {ident(name)};")
                # QuestDB has no DELETE; a NULL TTL retires the recorded state of the dropped view.
                self._exec(
                    f"UPDATE {ident(STATE_TABLE)} SET ttl_days = NULL WHERE measurement = {literal(self.table)} "
                    f"AND object = {literal(name)};"
                )
                log.info("QuestDB dropped superseded rollup view '%s'", name)

    def _view_current(self, view: str) -> bool:
        rows = self._exec(
            "SELECT view_name, base_table_name, view_status, refresh_base_table_txn, base_table_txn "
            f"FROM materialized_views() WHERE view_name = {literal(view)};"
        ).get("dataset") or []
        if not rows:
            return False
        _, base, status, refreshed, latest = rows[0][:5]
        return (
            str(base) == self.table and str(status).lower() == "valid"
            and isinstance(refreshed, int) and isinstance(latest, int) and refreshed >= latest
        )

    def _recorded_days(self, name: str) -> int | None:
        rows = self._exec(
            f"SELECT ttl_days FROM {ident(STATE_TABLE)} WHERE measurement = {literal(self.table)} "
            f"AND object = {literal(name)} LATEST ON timestamp PARTITION BY object;"
        ).get("dataset") or []
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def _apply_ttl(self, name: str, days: int, *, view: bool) -> bool:
        """Set the TTL on an object without one, or on one this service set earlier; never override."""
        if days <= 0:
            return True  # unlimited: an existing TTL is never dropped
        rows = self._exec(f"SELECT ttlValue, ttlUnit FROM tables() WHERE table_name = {literal(name)};").get(
            "dataset"
        ) or []
        if not rows:
            log.warning("QuestDB object '%s' not found; retention is not applied", name)
            return False
        value, unit = rows[0][:2]
        existing = ttl_days(value, unit)
        if value and existing == days:
            return True
        if value and existing != self._recorded_days(name):
            log.warning(
                "QuestDB object '%s' keeps its administrator TTL of %s %s instead of %d day(s)", name, value, unit, days
            )
            return True
        kind = "MATERIALIZED VIEW" if view else "TABLE"
        self._exec(f"ALTER {kind} {ident(name)} SET TTL {int(days)} DAYS;")
        self._exec(
            f"INSERT INTO {ident(STATE_TABLE)} (timestamp, measurement, object, ttl_days) VALUES "
            f"(now(), {literal(self.table)}, {literal(name)}, {int(days)});"
        )
        log.info("Set TTL of QuestDB object '%s' to %d day(s)", name, days)
        return True
