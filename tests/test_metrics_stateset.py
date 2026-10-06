#!/usr/bin/env python3
#
# tests/test_metrics_stateset.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Enum metrics are exported as StateSet series with a `state` label (Requirement 20.39)."""

import logging
import re
from datetime import UTC, datetime

import pytest

from app.cache import MemoryCache
from app.catalog.registry import RegistryCatalog
from app.observability.exporter import DeviceView, MetricsExporter
from app.observability.names import build_metric_names
from app.observability.stats import Histogram, ServiceCounters
from tests.api_helpers import make_settings

CATALOG = RegistryCatalog.from_file(make_settings().object_registry_path)
NUMERIC = [e for e in CATALOG.entries() if e.value_type.value not in ("string", "object")]
NAMES = build_metric_names(NUMERIC)
ENUMS = {e.name: e.enum_labels for e in CATALOG.entries() if e.enum_labels}
LABELS = set(ENUMS["inverter_state"].values())
NOW = 1000.0
ROW = re.compile(r'rct_inverter_state\{device="main",state="([^"]*)"\} (\d+)')


def _render(value: float | None, age: float = 0.0, log_name: str | None = None) -> tuple[str, MetricsExporter]:
    cache = MemoryCache(10.0, 20.0)
    if value is not None:
        cache.put(("main", "inverter_state"), value, measured_at=datetime.now(UTC), received_monotonic=NOW - age,
                  origin="transaction")  # fmt: skip
    device = DeviceView("main", Histogram(), lambda: 0, lambda: None, lambda: 0, lambda: 0, lambda: 0)
    exporter = MetricsExporter(cache, lambda: NOW, ServiceCounters(), [device], [], NAMES, ["inverter_state"], ENUMS)
    return exporter.render(), exporter


def _matches(text: str) -> list[tuple[str, int]]:
    """Every matched (state, value) row, without dedup, so a duplicate series can be caught."""
    return [(m[1], int(m[2])) for m in ROW.finditer(text)]


def _rows(text: str) -> dict[str, int]:
    matches = _matches(text)
    assert len(matches) == len({state for state, _ in matches})  # no duplicate state label emitted twice
    return dict(matches)


def test_current_state_is_one_all_others_zero() -> None:
    text, _ = _render(6)
    rows = _rows(text)
    assert set(rows) == LABELS and len(rows) == len(LABELS)
    assert rows["power_check"] == 1 and sum(rows.values()) == 1
    assert "# TYPE rct_inverter_state gauge" in text
    assert re.search(r"^rct_inverter_state\{device=\"main\"\} ", text, re.MULTILINE) is None  # no numeric code


def test_codes_sharing_a_label_light_that_label_once() -> None:
    for code in (0, 2):
        rows = _rows(_render(code)[0])
        assert rows["standby"] == 1 and sum(rows.values()) == 1


def test_unknown_code_exports_fixed_unknown_state_and_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.observability.exporter"):
        text, exporter = _render(99)
        exporter.render()
    rows = _rows(text)
    assert rows["unknown"] == 1 and sum(rows.values()) == 1 and len(rows) == len(LABELS) + 1
    assert "99" not in text
    assert len([r for r in caplog.records if "99" in r.getMessage()]) == 1


def test_expired_or_missing_value_has_no_state_rows() -> None:
    assert "rct_inverter_state" not in _render(6, age=31)[0]
    assert "rct_inverter_state" not in _render(None)[0]


def test_grace_value_keeps_states_and_reports_age() -> None:
    text, _ = _render(13, age=15)
    assert _rows(text)["feed_in"] == 1
    assert 'rct_device_metric_age_seconds{device="main",metric="inverter_state"} 15' in text
