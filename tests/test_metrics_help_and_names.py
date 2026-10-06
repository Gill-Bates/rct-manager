#!/usr/bin/env python3
#
# tests/test_metrics_help_and_names.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""HELP texts come from the registry; unit and timestamp names follow Prometheus conventions."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.cache import MemoryCache
from app.catalog.registry import RegistryCatalog
from app.errors import ConfigError
from app.observability.exporter import DeviceView, MetricsExporter
from app.observability.names import build_metric_names, metric_help
from app.observability.stats import Histogram, ServiceCounters

CATALOG = RegistryCatalog.from_file(Path(__file__).parents[1] / "app/catalog/objects.json")
NUMERIC = [e for e in CATALOG.entries() if e.value_type.value not in ("string", "object")]
NAMES = build_metric_names(NUMERIC)
HELPS = {e.name: metric_help(e) for e in NUMERIC}
ENUMS = {e.name: e.enum_labels for e in CATALOG.entries() if e.enum_labels}
NOW = 1000.0


def _device() -> DeviceView:
    return DeviceView("main", Histogram(), lambda: 0, lambda: None, lambda: 0, lambda: 0, lambda: 0)


def _render(exposed: list[str], helps: dict[str, str] = HELPS, enums: dict = ENUMS) -> str:
    cache = MemoryCache(10.0, 20.0)
    for name in exposed:
        cache.put(("main", name), 1.0, measured_at=datetime.now(UTC), received_monotonic=NOW, origin="transaction")
    exporter = MetricsExporter(cache, lambda: NOW, ServiceCounters(), [_device()], [], NAMES, exposed, enums, helps)
    return exporter.render()


def test_help_uses_registry_description_and_falls_back_for_raw_names() -> None:
    text = _render(["battery_ah_capacity", "energy_e_load_day", "inverter_state"])
    assert "# HELP rct_battery_ah_capacity_ampere_hours Battery capacity in Ah" in text
    assert "# HELP rct_energy_e_load_day_watt_hours Energy e load day (Wh)\n" in text
    assert "Value of" not in text
    assert "# HELP rct_inverter_state Inverter state machine state. One series per state label" in text


def test_help_is_escaped() -> None:
    text = _render(["battery_soc"], {"battery_soc": "a\\b\nc"}, {})
    assert "# HELP rct_battery_soc_ratio a\\\\b\\nc\n" in text


def test_timestamp_and_interval_names() -> None:
    assert NAMES["power_mng_bat_next_calib_date"] == "rct_power_mng_bat_next_calib_date_timestamp_seconds"
    assert NAMES["power_mng_bat_calib_reqularity"] == "rct_power_mng_bat_calib_reqularity_days"


def test_capacity_preselected_and_amp_hours_not() -> None:
    selected = CATALOG.preselected()
    assert "battery_ah_capacity" in selected and "power_mng_amp_hours" not in selected


def test_duplicate_exposed_names_report_each_name_once_in_sorted_order() -> None:
    with pytest.raises(ConfigError) as error:
        _render(["inverter_state", "battery_soc", "inverter_state", "battery_soc", "inverter_state"])
    assert error.value.code == "invalid_metrics_exposed_names"
    assert error.value.context["detail"] == "named more than once: battery_soc, inverter_state"
