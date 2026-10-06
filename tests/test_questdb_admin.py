#!/usr/bin/env python3
#
# tests/test_questdb_admin.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""QuestDB provisioning SQL: presets, raw/total retention, administrator TTLs preserved."""

import pytest

from app.export.questdb_admin import (
    PRESETS,
    QuestDbProvisioner,
    effective_raw_days,
    rollup_ddl,
    rollup_view_name,
    schema_signature,
    ttl_days,
)

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
            return {"dataset": [[view, "rct", "valid", 5, 5 if self.view_ok else 9]]}
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


def test_raw_ttl_waits_for_a_current_view_and_for_data() -> None:
    db = FakeQuestDb(view_ok=False)
    assert not QuestDbProvisioner(db, "rct", "medium", None, 90).run()
    assert db.altered() == []
    empty = FakeQuestDb(columns={"timestamp": "TIMESTAMP", "device": "SYMBOL"})
    assert not QuestDbProvisioner(empty, "rct", "medium", None, 90).run()
    assert not any(s.startswith("CREATE MATERIALIZED") for s in empty.sql)
