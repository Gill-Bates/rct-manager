#!/usr/bin/env python3
#
# tests/test_questdb_admin.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""QuestDB provisioning SQL: presets, raw/total retention, administrator TTLs preserved."""

import json
import re
from pathlib import Path

import pytest

from app.catalog.registry import RegistryCatalog
from app.export.questdb_admin import (
    MAX_EXEC_TARGET_BYTES,
    PRESETS,
    QuestDbProvisioner,
    effective_raw_days,
    exec_target,
    rollup_ddl,
    rollup_view_name,
    rollup_views,
    schema_signature,
    ttl_days,
)
from app.observability.names import prometheus_name

_ROOT = Path(__file__).resolve().parents[1]

COLUMNS = {"timestamp": "TIMESTAMP", "device": "SYMBOL", "rct_grid_power": "DOUBLE", "rct_api_requests_total": "DOUBLE"}
_HASH = schema_signature(COLUMNS)


class FakeQuestDb:
    def __init__(self, ttl=(0, ""), recorded=None, view_ok=True, columns=COLUMNS) -> None:
        self.ttl, self.recorded, self.view_ok, self.columns = ttl, recorded, view_ok, columns
        self.sql: list[str] = []

    def __call__(self, sql: str) -> dict:
        self.sql.append(sql)
        if "table_columns" in sql:
            return {"dataset": [[k, v] for k, v in self.columns.items()]}
        if "materialized_views()" in sql:
            view = rollup_view_name("rct", PRESETS["medium"], schema_signature(self.columns))
            rows = [[view, "rct", "valid", 5, 5 if self.view_ok else 9]]
            if "WHERE base_table_name" in sql:
                rows.append(["rct_rollup_1m_vold", "rct", "valid", 5, 5])
            return {"dataset": rows}
        if "ttlValue" in sql:
            return {"dataset": [[self.ttl[0], self.ttl[1]]]}
        if "LATEST ON" in sql:
            return {"dataset": [] if self.recorded is None else [[self.recorded]]}
        return {}

    def altered(self) -> list[str]:
        return [s for s in self.sql if s.startswith("ALTER")]


def test_presets_and_effective_raw_days() -> None:
    assert PRESETS["low"].raw_retention_days == 30 and PRESETS["medium"].raw_retention_days == 7
    assert (PRESETS["high"].raw_retention_days, PRESETS["high"].rollup_interval) == (1, "5m")
    assert effective_raw_days("off", 5, 100) is None
    assert effective_raw_days("manual", 5, 100) == 5
    with pytest.raises(ValueError, match="requires raw retention days"):
        effective_raw_days("manual", None, 100)
    assert effective_raw_days("medium", None, 100) == 7
    assert effective_raw_days("medium", 20, 100) == 20  # explicit value overrides the preset
    assert effective_raw_days("low", None, 10) == 10  # preset default never exceeds the total
    assert effective_raw_days("low", None, 0) == 30


def test_explicit_raw_above_total_is_clamped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """An explicit preset raw value above total is clamped to total, and the clamp is observable."""
    with caplog.at_level("WARNING", logger="app.export.questdb_admin"):
        assert effective_raw_days("medium", 200, 90) == 90  # raw never exceeds total
    assert any(
        record.levelname == "WARNING" and "clamping raw retention" in record.getMessage() for record in caplog.records
    )
    # An explicit value at or below total keeps its value and emits no clamp warning.
    caplog.clear()
    with caplog.at_level("WARNING", logger="app.export.questdb_admin"):
        assert effective_raw_days("medium", 20, 90) == 20
    assert not caplog.records


def test_rollup_ddl() -> None:
    ddl = rollup_ddl("rct", PRESETS["medium"], COLUMNS, rollup_view_name("rct", PRESETS["medium"], _HASH))
    assert f'CREATE MATERIALIZED VIEW IF NOT EXISTS "rct_rollup_1m_v{_HASH}" WITH BASE "rct" REFRESH IMMEDIATE' in ddl
    assert 'avg("rct_grid_power") AS "rct_grid_power_avg"' in ddl
    assert 'last("rct_api_requests_total") AS "rct_api_requests_total_last"' in ddl
    assert '"rct_api_requests_total_avg"' not in ddl
    assert "SAMPLE BY 1m ALIGN TO CALENDAR" in ddl and '"device"' in ddl
    assert rollup_view_name("rct", PRESETS["high"], _HASH) == f"rct_rollup_5m_v{_HASH}"
    with pytest.raises(ValueError):
        rollup_ddl("rct", PRESETS["low"], {"timestamp": "TIMESTAMP"}, "rct_rollup_1m_vdeadbeef")


def test_schema_signature_changes_with_the_column_set() -> None:
    other = {**COLUMNS, "rct_grid_frequency": "DOUBLE"}
    assert schema_signature(COLUMNS) != schema_signature(other)
    assert schema_signature(COLUMNS) == schema_signature(dict(COLUMNS))  # stable for the same columns


def test_ttl_days() -> None:
    assert ttl_days(2, "WEEKS") == 14 and ttl_days(0, "") == 0 and ttl_days(3, "FORTNIGHT") is None


def test_off_applies_total_retention_to_the_table() -> None:
    db = FakeQuestDb()
    assert QuestDbProvisioner(db, "rct", "off", None, 90).run()
    assert db.altered() == ['ALTER TABLE "rct" SET TTL 90 DAYS;']


def test_manual_applies_only_explicit_raw_retention_without_a_rollup() -> None:
    db = FakeQuestDb()
    assert QuestDbProvisioner(db, "rct", "manual", 5, 90).run()
    assert db.altered() == ['ALTER TABLE "rct" SET TTL 5 DAYS;']
    assert not any(sql.startswith("CREATE MATERIALIZED VIEW") for sql in db.sql)


def test_existing_administrator_ttl_is_kept() -> None:
    db = FakeQuestDb(ttl=(14, "DAY"))
    assert QuestDbProvisioner(db, "rct", "off", None, 90).run()
    assert db.altered() == []


def test_own_ttl_is_updated_but_unlimited_never_alters() -> None:
    db = FakeQuestDb(ttl=(14, "DAY"), recorded=14)
    QuestDbProvisioner(db, "rct", "off", None, 90).run()
    assert db.altered() == ['ALTER TABLE "rct" SET TTL 90 DAYS;']
    db = FakeQuestDb(ttl=(14, "DAY"), recorded=14)
    QuestDbProvisioner(db, "rct", "off", None, 0).run()
    assert db.altered() == []


def test_a_ttl_without_an_ownership_marker_is_kept_as_foreign_even_when_it_matches() -> None:
    """P2 fail-closed ownership: a TTL this service did not record belongs to the administrator and
    is never adopted — not even when its length happens to equal the desired one. Adopting it would
    let a later retention change shorten a genuinely foreign TTL against the administrator's
    decision. No ALTER and no ownership INSERT is written; the TTL is left untouched.
    """
    db = FakeQuestDb(ttl=(90, "DAY"), recorded=None)
    assert QuestDbProvisioner(db, "rct", "off", None, 90).run()
    assert db.altered() == []  # the TTL is correct and foreign; it is kept, never re-ALTERed
    assert not any(s.startswith("INSERT INTO") and "_rct_export_state" in s for s in db.sql)


def test_an_unreadable_ttl_unit_without_a_marker_is_kept_not_overwritten() -> None:
    """A TTL whose unit the service cannot decode (ttl_days() -> None) and that carries no ownership
    marker must be kept, not overwritten: the service cannot even read its real length, so treating
    it as foreign is the only safe choice.
    """
    db = FakeQuestDb(ttl=(3, "FORTNIGHT"), recorded=None)
    assert QuestDbProvisioner(db, "rct", "off", None, 90).run()
    assert db.altered() == []


def test_an_own_recorded_ttl_that_already_matches_needs_no_reconcile() -> None:
    """The counterpart: TTL correct and already recorded as ours writes neither an ALTER nor a
    duplicate ownership marker.
    """
    db = FakeQuestDb(ttl=(90, "DAY"), recorded=90)
    assert QuestDbProvisioner(db, "rct", "off", None, 90).run()
    assert db.altered() == []
    assert not any(s.startswith("INSERT INTO") and "_rct_export_state" in s for s in db.sql)


def test_downsampling_sets_view_and_raw_ttl() -> None:
    db = FakeQuestDb()
    assert QuestDbProvisioner(db, "rct", "medium", 3, 90).run()
    assert any(s.startswith("CREATE MATERIALIZED VIEW") for s in db.sql)
    view = rollup_view_name("rct", PRESETS["medium"], _HASH)
    assert db.altered() == [f'ALTER MATERIALIZED VIEW "{view}" SET TTL 90 DAYS;',
                            'ALTER TABLE "rct" SET TTL 3 DAYS;']


def test_rollup_view_follows_a_changed_column_set() -> None:
    """A metric added after a restart gets a new view name instead of staying out of a stale one."""
    db = FakeQuestDb()
    assert QuestDbProvisioner(db, "rct", "medium", 3, 90).run()
    first_view = rollup_view_name("rct", PRESETS["medium"], _HASH)
    assert any(f'"{first_view}"' in s for s in db.sql if s.startswith("CREATE MATERIALIZED"))

    extended_columns = {**COLUMNS, "rct_new_metric": "DOUBLE"}
    db2 = FakeQuestDb(columns=extended_columns)
    assert QuestDbProvisioner(db2, "rct", "medium", 3, 90).run()
    second_view = rollup_view_name("rct", PRESETS["medium"], schema_signature(extended_columns))
    assert second_view != first_view
    assert any(f'"{second_view}"' in s for s in db2.sql if s.startswith("CREATE MATERIALIZED"))
    assert any('"rct_new_metric_avg"' in s for s in db2.sql if s.startswith("CREATE MATERIALIZED"))


def test_superseded_rollup_views_are_kept_not_dropped() -> None:
    """A schema change can only rebuild the new view from the raw table, which may hold less
    history than an old, already-shortened raw TTL allows the superseded view to cover. Dropping
    it would destroy that historical rollup data irrecoverably, so it must survive the run."""
    db = FakeQuestDb()
    assert QuestDbProvisioner(db, "rct", "medium", 3, 90).run()
    assert not any(s.startswith("DROP MATERIALIZED VIEW") for s in db.sql)
    assert not any(s.startswith("UPDATE") and "ttl_days = NULL" in s for s in db.sql)


class SupersededQuestDb(FakeQuestDb):
    """Fake whose set of superseded sibling views can grow between provisioning runs, to exercise
    the once-per-view WARNING de-duplication across the periodic recheck cycles.
    """

    def __init__(self, superseded: list[str]) -> None:
        super().__init__()
        self.superseded = superseded

    def __call__(self, sql: str) -> dict:
        if "materialized_views()" in sql and "WHERE base_table_name" in sql:
            self.sql.append(sql)
            current = rollup_view_name("rct", PRESETS["medium"], schema_signature(self.columns))
            rows = [[current, "rct", "valid", 5, 5]]
            rows += [[name, "rct", "valid", 5, 5] for name in self.superseded]
            return {"dataset": rows}
        return super().__call__(sql)


def test_superseded_view_warning_is_logged_once_per_process(caplog: pytest.LogCaptureFixture) -> None:
    """The provisioner reruns every recheck cycle over an unchanged schema, so a static set of
    superseded views must warn only once per process; a view first seen on a later run still warns.
    """
    caplog.set_level("DEBUG", logger="app.export.questdb_admin")
    db = SupersededQuestDb(["rct_rollup_1m_vold"])
    provisioner = QuestDbProvisioner(db, "rct", "medium", 3, 90)

    assert provisioner.run()
    first = [r for r in caplog.records if r.levelname == "WARNING" and "is superseded by" in r.getMessage()]
    assert len(first) == 1 and "rct_rollup_1m_vold" in first[0].getMessage()

    caplog.clear()
    assert provisioner.run()  # same superseded set: no fresh WARNING, downgraded to DEBUG
    assert not [r for r in caplog.records if r.levelname == "WARNING" and "is superseded by" in r.getMessage()]
    assert [r for r in caplog.records if r.levelname == "DEBUG" and "is superseded by" in r.getMessage()]

    caplog.clear()
    db.superseded.append("rct_rollup_1m_vnew")  # a later schema change leaves a new superseded view
    assert provisioner.run()
    fresh = [r for r in caplog.records if r.levelname == "WARNING" and "is superseded by" in r.getMessage()]
    assert len(fresh) == 1 and "rct_rollup_1m_vnew" in fresh[0].getMessage()


def test_raw_ttl_waits_for_a_current_view_and_for_data() -> None:
    db = FakeQuestDb(view_ok=False)
    assert not QuestDbProvisioner(db, "rct", "medium", None, 90).run()
    assert db.altered() == []
    empty = FakeQuestDb(columns={"timestamp": "TIMESTAMP", "device": "SYMBOL"})
    assert not QuestDbProvisioner(empty, "rct", "medium", None, 90).run()
    assert not any(s.startswith("CREATE MATERIALIZED") for s in empty.sql)


def test_a_view_that_is_not_refreshed_yet_warns_only_when_the_lag_persists(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG", logger="app.export.questdb_admin")
    provisioner = QuestDbProvisioner(FakeQuestDb(view_ok=False), "rct", "medium", None, 90)
    for _ in range(7):
        assert not provisioner.run()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert not provisioner.run()  # 8th consecutive attempt
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "after 8 attempts" in warnings[0].getMessage()


_CATALOG_VALUES = sorted(
    {prometheus_name(e) for e in RegistryCatalog.from_file(_ROOT / "app" / "catalog" / "objects.json").entries()}
)
CATALOG_COLUMNS = {
    "timestamp": "TIMESTAMP",
    **dict.fromkeys(("device", "endpoint", "state", "metric"), "SYMBOL"),
    **dict.fromkeys(_CATALOG_VALUES, "DOUBLE"),
}


def _aliases(name: str) -> list[str]:
    if name.endswith(("_total", "_sum", "_count")):
        return [f"{name}_last"]
    return [f"{name}_{aggregate}" for aggregate in ("avg", "min", "max", "last")]


class ShardedQuestDb(FakeQuestDb):
    """Fake that knows the views it was asked to create, plus ``preexisting`` ones; views named in
    ``lagging`` report a base table transaction they have not refreshed to yet.
    """

    def __init__(self, preexisting: list[str] | None = None, lagging: set[str] | None = None) -> None:
        super().__init__(columns=CATALOG_COLUMNS)
        self.preexisting, self.lagging = preexisting or [], lagging or set()
        self.created: list[str] = []

    def __call__(self, sql: str) -> dict:
        if sql.startswith("CREATE MATERIALIZED VIEW"):
            self.sql.append(sql)
            self.created.append(re.search(r'IF NOT EXISTS "([^"]+)"', sql).group(1))
            return {}
        if "materialized_views()" in sql:
            self.sql.append(sql)
            known = dict.fromkeys([*self.preexisting, *self.created])
            if "WHERE base_table_name" in sql:
                return {"dataset": [[name, "rct", "valid", 5, 5] for name in known]}
            name = re.search(r"WHERE view_name = '([^']+)'", sql).group(1)
            latest = 9 if name in self.lagging else 5
            return {"dataset": [[name, "rct", "valid", 5, latest]] if name in known else []}
        return super().__call__(sql)


def _superseded_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and "is superseded by" in r.getMessage()]


def test_every_exec_request_for_the_whole_catalog_stays_under_the_budget() -> None:
    assert len(_CATALOG_VALUES) == 894
    assert MAX_EXEC_TARGET_BYTES <= 32 * 1024 < 64 * 1024
    for preset in PRESETS.values():
        views = rollup_views("rct", preset, CATALOG_COLUMNS)
        assert len(views) > 1
        assert all(len(exec_target(ddl)) <= MAX_EXEC_TARGET_BYTES for _, ddl in views)
    db = ShardedQuestDb()
    assert QuestDbProvisioner(db, "rct", "medium", 3, 90).run()
    assert max(len(exec_target(sql)) for sql in db.sql) <= MAX_EXEC_TARGET_BYTES


def test_shards_cover_every_value_column_exactly_once() -> None:
    views = rollup_views("rct", PRESETS["medium"], CATALOG_COLUMNS)
    ddls = [ddl for _, ddl in views]
    for name in _CATALOG_VALUES:
        for alias in _aliases(name):
            assert sum(ddl.count(f'AS "{alias}"') for ddl in ddls) == 1, alias
    for ddl in ddls:
        assert all(f'"{dim}"' in ddl for dim in ("device", "endpoint", "state", "metric"))
        assert "SAMPLE BY 1m ALIGN TO CALENDAR" in ddl and 'WITH BASE "rct"' in ddl
    names = [view for view, _ in views]
    assert len(set(names)) == len(names)
    assert all(re.fullmatch(rf"rct_rollup_1m_v[0-9a-f]{{10}}_p{i}", view) for i, view in enumerate(names, start=1))
    assert rollup_views("rct", PRESETS["medium"], dict(CATALOG_COLUMNS)) == views
    # A metric sorting behind the catalog only renames the shard it lands in.
    extended = [view for view, _ in rollup_views("rct", PRESETS["medium"], {**CATALOG_COLUMNS, "zzz_new": "DOUBLE"})]
    assert extended[: len(names) - 1] == names[:-1]


def test_raw_ttl_waits_until_every_shard_is_current() -> None:
    shards = [view for view, _ in rollup_views("rct", PRESETS["medium"], CATALOG_COLUMNS)]
    db = ShardedQuestDb(lagging={shards[-1]})
    provisioner = QuestDbProvisioner(db, "rct", "medium", 3, 90)
    assert not provisioner.run()
    assert db.altered() == []
    db.lagging.clear()
    assert provisioner.run()
    assert db.altered() == [
        *(f'ALTER MATERIALIZED VIEW "{view}" SET TTL 90 DAYS;' for view in shards),
        'ALTER TABLE "rct" SET TTL 3 DAYS;',
    ]


def test_sibling_shards_are_not_reported_as_superseded(caplog: pytest.LogCaptureFixture) -> None:
    db = ShardedQuestDb(preexisting=["rct_rollup_1m_vold"])
    with caplog.at_level("WARNING", logger="app.export.questdb_admin"):
        assert QuestDbProvisioner(db, "rct", "medium", 3, 90).run()
    warnings = _superseded_warnings(caplog)
    assert len(warnings) == 1 and warnings[0].startswith("QuestDB rollup view 'rct_rollup_1m_vold' is superseded")
    assert not any(s.startswith("DROP") for s in db.sql)


def test_an_existing_single_view_for_the_current_schema_is_kept_instead_of_sharding(
    caplog: pytest.LogCaptureFixture,
) -> None:
    whole = rollup_view_name("rct", PRESETS["medium"], schema_signature(CATALOG_COLUMNS))
    db = ShardedQuestDb(preexisting=[whole])
    with caplog.at_level("WARNING", logger="app.export.questdb_admin"):
        assert QuestDbProvisioner(db, "rct", "medium", 3, 90).run()
    assert not any(s.startswith("CREATE MATERIALIZED VIEW") for s in db.sql)
    assert db.altered() == [f'ALTER MATERIALIZED VIEW "{whole}" SET TTL 90 DAYS;', 'ALTER TABLE "rct" SET TTL 3 DAYS;']
    assert not _superseded_warnings(caplog)


def test_small_column_sets_keep_the_single_view() -> None:
    view = rollup_view_name("rct", PRESETS["medium"], _HASH)
    assert rollup_views("rct", PRESETS["medium"], COLUMNS) == [
        (view, rollup_ddl("rct", PRESETS["medium"], COLUMNS, view))
    ]


def test_dashboard_discovers_the_view_holding_its_rollup_column() -> None:
    dashboard = json.loads((_ROOT / "grafana" / "dashboard_questdb.json").read_text(encoding="utf-8"))
    variable = next(v for v in dashboard["templating"]["list"] if v["name"] == "rollup_view")
    for sql in (variable["definition"], variable["query"]["rawSql"]):
        assert "view_sql LIKE '%rct_grid_power_watts_avg%'" in sql and "view_status = 'valid'" in sql
    panel = next(p for p in dashboard["panels"] if p["id"] == 7)
    raw_sql = panel["targets"][0]["rawSql"]
    assert '"rct_grid_power_watts_avg"' in raw_sql and 'FROM "${rollup_view}"' in raw_sql
    ddls = [ddl for _, ddl in rollup_views("rct", PRESETS["medium"], CATALOG_COLUMNS)]
    assert sum('AS "rct_grid_power_watts_avg"' in ddl for ddl in ddls) == 1
