#!/usr/bin/env python3
#
# app/export/questdb_admin.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""QuestDB OSS provisioning: table, TTL and materialized-view rollups.

Approach ported from fritzfluxdb: a TTL that exists and was not set by this service belongs to
the administrator and is never replaced; the raw TTL only shortens after all rollup views are current.
"""

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import quote_plus, urlencode

log = logging.getLogger(__name__)

EXEC_PATH = "/exec"
# QuestDB answers 431 once the request head exceeds http.request.header.buffer.size (64 KiB default);
# half of it stays free for the request line, Host, Authorization and a possible decoded copy.
MAX_EXEC_TARGET_BYTES = 32 * 1024
_SELECT_SEP = ",\n        "

# Consecutive not-yet-refreshed attempts before warning (then again at each doubling); a power of two.
_NOT_REFRESHED_WARN_AFTER = 8

STATE_TABLE = "_rct_export_state"
_TTL_UNIT_HOURS = {
    "HOUR": 1,
    "DAY": 24,
    "WEEK": 24 * 7,
    "MONTH": 24 * 30,
    "YEAR": 24 * 365,
}
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


def effective_raw_days(
    downsampling: str, raw_days: int | None, total_days: int
) -> int | None:
    """Days of raw data: manual uses its explicit TTL; off (no preset) returns None here, and the
    total TTL is applied downstream in ``QuestDbProvisioner.run()`` rather than by this function.
    """
    if downsampling == "manual":
        if raw_days is None:
            raise ValueError("manual downsampling requires raw retention days")
        return raw_days
    preset = PRESETS.get(downsampling)
    if preset is None:
        return None
    days = raw_days if raw_days is not None else preset.raw_retention_days
    if total_days <= 0:
        return days
    if raw_days is not None and raw_days > total_days:
        # Raw retention can never exceed total; the clamp stays, but an explicit operator value
        # losing to it is otherwise invisible, so make the effective retention observable.
        log.warning(
            "QuestDB raw retention of %d day(s) exceeds total retention of %d day(s); "
            "clamping raw retention to %d day(s)",
            raw_days,
            total_days,
            total_days,
        )
    return min(days, total_days)


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
    digest = hashlib.sha256(
        ("|".join(dims) + "::" + "|".join(values)).encode()
    ).hexdigest()
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


def exec_target(sql: str, path: str = EXEC_PATH) -> str:
    """Request target of a QuestDB ``/exec`` call; the only place its query encoding is defined."""
    return f"{path}?{urlencode({'query': sql})}"


def _value_projections(name: str) -> list[str]:
    if name.endswith(_COUNTER_SUFFIXES):
        return [f"last({ident(name)}) AS {ident(name + '_last')}"]
    return [
        f"{aggregate}({ident(name)}) AS {ident(f'{name}_{aggregate}')}" for aggregate in ("avg", "min", "max", "last")
    ]


def _rollup_sql(table: str, preset: Preset, dims: list[str], values: list[str], view: str) -> str:
    projections = ["timestamp", *(ident(name) for name in dims)]
    for name in values:
        projections.extend(_value_projections(name))
    select_list = _SELECT_SEP.join(projections)
    return (
        f"CREATE MATERIALIZED VIEW IF NOT EXISTS {ident(view)} "
        f"WITH BASE {ident(table)} REFRESH IMMEDIATE AS (\n"
        f"    SELECT\n        {select_list}\n"
        f"    FROM {ident(table)}\n"
        f"    SAMPLE BY {preset.rollup_interval} ALIGN TO CALENDAR\n"
        ") PARTITION BY DAY;"
    )


def rollup_ddl(table: str, preset: Preset, columns: dict[str, str], view: str) -> str:
    """Materialized view over the symbol and DOUBLE columns present in the table."""
    dims, values = _split_columns(columns)
    if not values:
        raise ValueError("no numeric columns to roll up yet")
    return _rollup_sql(table, preset, dims, values, view)


def rollup_views(table: str, preset: Preset, columns: dict[str, str]) -> list[tuple[str, str]]:
    """``(view, ddl)`` pairs that together roll up every DOUBLE column of the table.

    One view as before while its ``/exec`` request fits ``MAX_EXEC_TARGET_BYTES``; otherwise the
    sorted value columns are packed into shards ``…_v<signature>_p<n>``. Each shard carries all
    SYMBOL columns, and its signature covers exactly its own projection, so a name never stands
    for different content and a new metric only renames the shards from its sort position on.
    """
    dims, values = _split_columns(columns)
    whole_name = rollup_view_name(table, preset, schema_signature(columns))
    whole_ddl = rollup_ddl(table, preset, columns, whole_name)
    if len(exec_target(whole_ddl)) <= MAX_EXEC_TARGET_BYTES:
        return [(whole_name, whole_ddl)]

    def base_cost(index: int) -> int:
        # Hex digits are not percent-encoded, so a zero hash has the length of the real one.
        placeholder = rollup_view_name(table, preset, "0" * 10) + f"_p{index}"
        return len(exec_target(_rollup_sql(table, preset, dims, [], placeholder)))

    # Percent-encoding works per character, so the cost of the target is additive per projection.
    shards: list[list[str]] = []
    current: list[str] = []
    used = base_cost(1)
    for name in values:
        cost = len(quote_plus("".join(_SELECT_SEP + p for p in _value_projections(name))))
        if current and used + cost > MAX_EXEC_TARGET_BYTES:
            shards.append(current)
            current = []
            used = base_cost(len(shards) + 1)
        if used + cost > MAX_EXEC_TARGET_BYTES:
            raise ValueError(f"rollup of column '{name}' does not fit one QuestDB /exec request")
        current.append(name)
        used += cost
    shards.append(current)

    views: list[tuple[str, str]] = []
    for index, shard in enumerate(shards, start=1):
        shard_columns = {name: "SYMBOL" for name in dims} | {name: "DOUBLE" for name in shard}
        view = rollup_view_name(table, preset, schema_signature(shard_columns)) + f"_p{index}"
        ddl = _rollup_sql(table, preset, dims, shard, view)
        if len(exec_target(ddl)) > MAX_EXEC_TARGET_BYTES:
            raise ValueError(f"rollup shard '{view}' exceeds the QuestDB /exec request budget")
        views.append((view, ddl))
    return views


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
    def __init__(
        self,
        execute: Execute,
        table: str,
        downsampling: str,
        raw_days: int | None,
        total_days: int,
    ) -> None:
        self._exec = execute
        self.table = table
        self.downsampling = downsampling
        self.total_days = total_days
        self.raw_days = effective_raw_days(downsampling, raw_days, total_days)
        self._not_refreshed = 0
        self._warned_superseded: set[str] = set()

    def ensure_tables(self) -> None:
        self._exec(create_table_sql(self.table))
        self._exec(create_state_table_sql())

    def run(self, *, ensure: bool = True) -> bool:
        """Apply retention and rollups; False while any rollup view is not ready (retry later)."""
        if ensure:
            self.ensure_tables()
        preset = PRESETS.get(self.downsampling)
        if preset is None:
            if self.downsampling == "manual":
                return self._apply_ttl(self.table, self.raw_days or 0, view=False)
            return self._apply_ttl(self.table, self.total_days, view=False)
        rows = (
            self._exec(
                f'SELECT "column", type FROM table_columns({literal(self.table)});'
            ).get("dataset")
            or []
        )
        columns = {str(name): str(kind).upper() for name, kind in rows}
        if not any(kind == "DOUBLE" for kind in columns.values()):
            log.info("QuestDB rollup waits for the first data in '%s'", self.table)
            return False
        # View names follow the column set (see schema_signature and rollup_views).
        plan = rollup_views(self.table, preset, columns)
        whole = rollup_view_name(self.table, preset, schema_signature(columns))
        if len(plan) > 1 and whole in self._base_view_names():
            # A single view created before sharding already covers this exact column set.
            views = [whole]
        else:
            for _, ddl in plan:
                self._exec(ddl)
            views = [view for view, _ in plan]
        lagging = next((view for view in views if not self._view_current(view)), None)
        if lagging is not None:
            self._note_not_refreshed(lagging)
            return False
        self._not_refreshed = 0
        self._warn_about_superseded_rollups(views)
        for view in views:
            if not self._apply_ttl(view, self.total_days, view=True):
                return False
        # The raw TTL only shortens once every rollup view holds the data that it would drop.
        return self._apply_ttl(self.table, self.raw_days or 0, view=False)

    def _note_not_refreshed(self, view: str) -> None:
        """A fresh or rebuilt view catches up on its own; only a persistent lag is worth a warning."""
        self._not_refreshed += 1
        attempts = self._not_refreshed
        if attempts >= _NOT_REFRESHED_WARN_AFTER and attempts & (attempts - 1) == 0:
            log.warning(
                "QuestDB rollup view '%s' is still not refreshed after %d attempts; raw TTL is unchanged",
                view,
                attempts,
            )
        else:
            log.debug(
                "QuestDB rollup view '%s' is not refreshed yet; raw TTL is unchanged",
                view,
            )

    def _base_view_names(self) -> set[str]:
        rows = (
            self._exec(
                f"SELECT view_name FROM materialized_views() WHERE base_table_name = {literal(self.table)};"
            ).get("dataset")
            or []
        )
        return {str(name) for (name, *_) in rows}

    def _warn_about_superseded_rollups(self, current: Sequence[str]) -> None:
        """Report earlier service-created rollup views of this table that a schema change replaced.

        Every name in ``current`` is part of the active rollup, so sibling shards are never superseded.

        Never auto-dropped: a schema change (e.g. a newly exported metric) rebuilds ``current``
        from the raw table alone, which can only reconstruct whatever the (possibly already
        shortened) raw TTL still holds. The superseded view may still hold older rollup data the
        raw table no longer has — exactly the historical aggregates the raw TTL was only safe to
        shorten because of. Dropping it immediately would destroy that data with no way back; it is
        left in place for an administrator to remove once a verified retention/overlap window has
        passed and no aggregate held only there is still needed.
        """
        prefix = f"{self.table}_rollup_"
        superseded = {name for name in self._base_view_names() if name.startswith(prefix) and name not in current}
        replacement = ", ".join(sorted(current))
        # Provisioning reruns every recheck cycle over an unchanged schema, so warn once per view
        # name for the process; a name only seen later (another schema change) still warns fresh.
        self._warned_superseded &= superseded  # forget names an admin has since dropped
        for name in sorted(superseded):
            message = (
                "QuestDB rollup view '%s' is superseded by '%s' and is kept, not dropped, to avoid"
                " losing historical rollup data the raw table may no longer hold"
            )
            if name in self._warned_superseded:
                log.debug(message, name, replacement)
            else:
                log.warning(message, name, replacement)
                self._warned_superseded.add(name)

    def _view_current(self, view: str) -> bool:
        rows = (
            self._exec(
                "SELECT view_name, base_table_name, view_status, refresh_base_table_txn, base_table_txn "
                f"FROM materialized_views() WHERE view_name = {literal(view)};"
            ).get("dataset")
            or []
        )
        if not rows:
            return False
        _, base, status, refreshed, latest = rows[0][:5]
        return (
            str(base) == self.table
            and str(status).lower() == "valid"
            and isinstance(refreshed, int)
            and isinstance(latest, int)
            and refreshed >= latest
        )

    def _recorded_days(self, name: str) -> int | None:
        rows = (
            self._exec(
                f"SELECT ttl_days FROM {ident(STATE_TABLE)} WHERE measurement = {literal(self.table)} "
                f"AND object = {literal(name)} LATEST ON timestamp PARTITION BY object;"
            ).get("dataset")
            or []
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def _apply_ttl(self, name: str, days: int, *, view: bool) -> bool:
        """Set the TTL on an object without one, or on one this service set earlier; never override."""
        if days <= 0:
            return True  # unlimited: an existing TTL is never dropped
        rows = (
            self._exec(
                f"SELECT ttlValue, ttlUnit FROM tables() WHERE table_name = {literal(name)};"
            ).get("dataset")
            or []
        )
        if not rows:
            log.warning("QuestDB object '%s' not found; retention is not applied", name)
            return False
        value, unit = rows[0][:2]
        existing = ttl_days(value, unit)
        recorded = self._recorded_days(name)
        if value:
            # Fail-closed ownership (module contract): a TTL this service did not record belongs to
            # the administrator and is never replaced. A missing marker is treated as foreign even
            # though a crash between ALTER and the ownership INSERT could in theory have left our
            # own TTL unrecorded — that rare case keeps its TTL and is simply not auto-adjusted
            # later, which is strictly safer than adopting a genuinely foreign TTL that happens to
            # match the desired value and then shortening it against the administrator's decision.
            if recorded is None:
                log.warning(
                    "QuestDB object '%s' already has an unowned TTL; keeping it", name
                )
                return True
            # An unknown TTL unit makes existing None: without this it would fall through to ALTER
            # and overwrite a TTL whose real length the service cannot even read.
            if existing is None or existing != recorded:
                log.warning(
                    "QuestDB object '%s' keeps its administrator TTL of %s %s instead of %d day(s)",
                    name,
                    value,
                    unit,
                    days,
                )
                return True
            if existing == days:
                return True
        kind = "MATERIALIZED VIEW" if view else "TABLE"
        self._exec(f"ALTER {kind} {ident(name)} SET TTL {int(days)} DAYS;")
        self._record_ownership(name, days)
        log.info("Set TTL of QuestDB object '%s' to %d day(s)", name, days)
        return True

    def _record_ownership(self, name: str, days: int | None = None) -> None:
        """Record that this service owns ``name``'s current TTL. ``days`` defaults to the TTL now on
        the object, so a reconcile writes the value that is actually in effect, not a stale target.
        """
        if days is None:
            rows = (
                self._exec(
                    f"SELECT ttlValue, ttlUnit FROM tables() WHERE table_name = {literal(name)};"
                ).get("dataset")
                or []
            )
            days = ttl_days(rows[0][0], rows[0][1]) if rows else None
            if days is None:
                return
        self._exec(
            f"INSERT INTO {ident(STATE_TABLE)} (timestamp, measurement, object, ttl_days) VALUES "
            f"(now(), {literal(self.table)}, {literal(name)}, {int(days)});"
        )
