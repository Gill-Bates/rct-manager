#!/usr/bin/env python3
#
# tests/test_metrics.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Prometheus metrics: names and labels, HELP texts and unit conventions, StateSet series for enums."""

import logging
import math
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.cache import MemoryCache
from app.catalog.registry import RegistryCatalog
from app.errors import ConfigError
from app.observability.exporter import DeviceView, EndpointView, MetricsExporter
from app.observability.names import build_metric_names, metric_help
from app.observability.stats import Histogram, ServiceCounters
from app.transport.counters import EndpointCounters
from tests.api_helpers import (
    HOST,
    PORT,
    READ_TOKEN,
    SLAVE_NETWORK_ID,
    make_settings,
    running_app,
)

CATALOG = RegistryCatalog.from_file(make_settings().object_registry_path)


NUMERIC = [e for e in CATALOG.entries() if e.value_type.value not in ("string", "object")]


NAMES = build_metric_names(NUMERIC)


EXPOSED = [e.name for e in NUMERIC]


DEVICES = ["main", "slave1", "dev-3"]


ENDPOINTS = ["ep1", "ep2"]


SAMPLE = re.compile(r"(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})? (?P<value>\S+)")


LABEL = re.compile(r'([a-z_]+)="((?:[^"\\]|\\.)*)"')


NOW = 1000.0


def _parse(text: str) -> list[tuple[str, dict[str, str], float]]:
    rows = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = SAMPLE.fullmatch(line)
        assert match, line
        labels = dict(LABEL.findall(match["labels"] or ""))
        assert len(labels) == (match["labels"] or "").count("=")
        rows.append((match["name"], labels, float(match["value"])))
    return rows


@st.composite
def _state(draw):
    entries = draw(
        st.lists(
            st.tuples(
                st.sampled_from(DEVICES),
                st.sampled_from(EXPOSED),
                st.floats(-1e6, 1e6, allow_nan=False),
                st.floats(0, 60),  # age in seconds against ttl 10 and grace 20
                st.booleans(),  # invalidated after the put
            ),
            max_size=12,
        )
    )
    counters = draw(
        st.lists(st.tuples(st.integers(0, 10**6), st.integers(0, 10**6), st.integers(0, 99)), min_size=2, max_size=2)
    )
    return entries, counters


# Feature: rct-manager, Property 19: metric names and labels separate device and transport endpoint
@settings(max_examples=100, deadline=None)
@given(state=_state())
def test_metric_names_and_labels_separate_device_and_transport(state) -> None:
    entries, counter_values = state
    cache = MemoryCache(10.0, 20.0)
    for device, metric, value, age, invalidated in entries:
        key = (device, metric)
        cache.put(key, value, measured_at=datetime.now(UTC), received_monotonic=NOW - age, origin="transaction")
        if invalidated:
            cache.invalidate(key)
    # Present and within TTL plus grace (30 s) means exported; everything else must be omitted.
    expected = {
        (d, m)
        for d in DEVICES
        for m in EXPOSED
        if (e := cache.get((d, m))) is not None and NOW - e.received_monotonic <= 30
    }
    endpoints = []
    for endpoint_id, (discarded, unexpected, queue) in zip(ENDPOINTS, counter_values, strict=True):
        counters = EndpointCounters(discarded_bytes=discarded, unexpected_frames=unexpected)
        endpoints.append(EndpointView(endpoint_id, counters, lambda q=queue: q, lambda: 7, lambda: False))
    devices = [DeviceView(d, Histogram(), lambda: 0, lambda: None, lambda: 0, lambda: 0, lambda: 0) for d in DEVICES]
    devices[0].durations.observe(0.3)
    exporter = MetricsExporter(cache, lambda: NOW, ServiceCounters(), devices, endpoints, NAMES, EXPOSED)
    text = exporter.render()
    rows = _parse(text)

    by_name = {v: k for k, v in NAMES.items()}
    seen_values: list[tuple[str, str]] = []  # collected without dedup so a duplicate series is caught below
    for name, labels, value in rows:
        assert set(labels) <= {"device", "endpoint", "metric", "le"}
        if "le" in labels:
            assert name.endswith("_bucket")
        if name.startswith("rct_transport_"):
            assert "endpoint" in labels and "device" not in labels
            assert labels["endpoint"] in ENDPOINTS
        elif name.startswith(("rct_api_",)):
            assert not labels
        else:
            assert "device" in labels and "endpoint" not in labels
        if name in by_name:
            seen_values.append((labels["device"], by_name[name]))
            assert not math.isnan(value)
        for label_value in labels.values():
            assert not re.search(r"0x[0-9a-fA-F]{8}", label_value)
            assert HOST not in label_value and str(PORT) not in label_value and str(SLAVE_NETWORK_ID) not in label_value
    # Prometheus must not emit duplicate samples with an identical label set: a set-based
    # comparison alone would silently absorb such a duplicate instead of catching it.
    assert len(seen_values) == len(set(seen_values))
    assert set(seen_values) == expected  # absent entries are omitted, not zero or NaN
    assert "NaN" not in text


def test_total_counters_stay_monotonic_across_a_live_view_swap() -> None:
    """A live device-list reload rebuilds the per-binding/endpoint counters from zero; the exported
    _total families must carry the old values forward instead of dropping within one process life."""
    cache = MemoryCache(10.0, 20.0)
    before_device = DeviceView("main", Histogram(), lambda: 4, lambda: None, lambda: 0, lambda: 10, lambda: 3)
    before_device.durations.observe(0.3)
    before_device.durations.observe(0.3)
    before_endpoint = EndpointView(
        "ep1", EndpointCounters(discarded_bytes=70, crc_errors=2, framing_errors=1, unexpected_frames=5),
        lambda: 0, lambda: 0, lambda: False,
    )
    exporter = MetricsExporter(cache, lambda: NOW, ServiceCounters(), [before_device], [before_endpoint], NAMES, [])

    def totals(text: str) -> dict[str, float]:
        return {name: value for name, _labels, value in _parse(text)
                if name.endswith("_total") or name.endswith(("_count", "_sum"))}

    first = totals(exporter.render())
    assert first["rct_api_cache_hits_total"] == 10 and first["rct_api_cache_misses_total"] == 3
    assert first["rct_device_errors_total"] == 4
    assert first["rct_device_request_duration_seconds_count"] == 2
    assert first["rct_transport_bytes_discarded_total"] == 70 and first["rct_transport_crc_errors_total"] == 2

    # The reload replaces the views with freshly built, zeroed ones for the same device/endpoint.
    exporter.set_devices([DeviceView("main", Histogram(), lambda: 0, lambda: None, lambda: 0, lambda: 0, lambda: 0)])
    exporter.set_endpoints([EndpointView("ep1", EndpointCounters(), lambda: 0, lambda: 0, lambda: False)])
    after = totals(exporter.render())
    for name, value in first.items():
        assert after[name] >= value, f"{name} fell from {value} to {after[name]} across the swap"

    # New activity on the rebuilt views adds on top of the carried baseline.
    exporter.set_devices([DeviceView("main", Histogram(), lambda: 1, lambda: None, lambda: 0, lambda: 2, lambda: 1)])
    grown = totals(exporter.render())
    assert grown["rct_api_cache_hits_total"] == 12 and grown["rct_device_errors_total"] == 5


async def test_metrics_route_is_served_when_enabled_and_absent_when_disabled() -> None:
    async with running_app(make_settings(enable_metrics_endpoint=True)) as h:
        assert (await h.client.get("/metrics")).status_code == 200
    async with running_app(make_settings(enable_metrics_endpoint=False)) as h:
        assert (await h.client.get("/metrics")).status_code == 404
        assert (await h.client.get("/api/v1/metrics")).status_code == 200  # the catalog is a different route


async def test_metrics_require_token_survives_the_auth_opt_out() -> None:
    """P2-4: METRICS_REQUIRE_TOKEN is the operator's explicit decision; AUTH_REQUIRED cannot void it."""
    settings = make_settings(auth_required=False, metrics_require_token=True)
    async with running_app(settings, authorize=False) as h:
        tokenless = await h.client.get("/metrics")
        assert tokenless.status_code == 401 and tokenless.json()["code"] == "missing_token"
        assert (await h.client.get("/metrics", headers={"Authorization": f"Bearer {READ_TOKEN}"})).status_code == 200


async def test_metrics_are_reachable_without_token_when_the_scrape_token_is_not_required() -> None:
    settings = make_settings(auth_required=False, metrics_require_token=False)
    async with running_app(settings, authorize=False) as h:
        assert (await h.client.get("/metrics")).status_code == 200


HELP_CATALOG = RegistryCatalog.from_file(Path(__file__).parents[1] / "app/catalog/objects.json")


HELP_NUMERIC = [e for e in HELP_CATALOG.entries() if e.value_type.value not in ("string", "object")]


HELP_NAMES = build_metric_names(HELP_NUMERIC)


HELPS = {e.name: metric_help(e) for e in HELP_NUMERIC}


HELP_ENUMS = {e.name: e.enum_labels for e in HELP_CATALOG.entries() if e.enum_labels}


def _device() -> DeviceView:
    return DeviceView("main", Histogram(), lambda: 0, lambda: None, lambda: 0, lambda: 0, lambda: 0)


def _render_help(exposed: list[str], helps: dict[str, str] = HELPS, enums: dict = HELP_ENUMS) -> str:
    cache = MemoryCache(10.0, 20.0)
    for name in exposed:
        cache.put(("main", name), 1.0, measured_at=datetime.now(UTC), received_monotonic=NOW, origin="transaction")
    exporter = MetricsExporter(cache, lambda: NOW, ServiceCounters(), [_device()], [], HELP_NAMES, exposed, enums, helps)
    return exporter.render()


def test_help_uses_registry_description_and_falls_back_for_raw_names() -> None:
    text = _render_help(["battery_ah_capacity", "energy_e_load_day", "inverter_state"])
    assert "# HELP rct_battery_ah_capacity_ampere_hours Battery capacity in Ah" in text
    assert "# HELP rct_energy_e_load_day_watt_hours Energy e load day (Wh)\n" in text
    assert "Value of" not in text
    assert "# HELP rct_inverter_state Inverter state machine state. One series per state label" in text


def test_help_is_escaped() -> None:
    text = _render_help(["battery_soc"], {"battery_soc": "a\\b\nc"}, {})
    assert "# HELP rct_battery_soc_ratio a\\\\b\\nc\n" in text


def test_timestamp_and_interval_names() -> None:
    assert HELP_NAMES["power_mng_bat_next_calib_date"] == "rct_power_mng_bat_next_calib_date_timestamp_seconds"
    assert HELP_NAMES["power_mng_bat_calib_reqularity"] == "rct_power_mng_bat_calib_reqularity_days"


def test_capacity_preselected_and_amp_hours_not() -> None:
    selected = HELP_CATALOG.preselected()
    assert "battery_ah_capacity" in selected and "power_mng_amp_hours" not in selected


def test_duplicate_exposed_names_report_each_name_once_in_sorted_order() -> None:
    with pytest.raises(ConfigError) as error:
        _render_help(["inverter_state", "battery_soc", "inverter_state", "battery_soc", "inverter_state"])
    assert error.value.code == "invalid_metrics_exposed_names"
    assert error.value.context["detail"] == "named more than once: battery_soc, inverter_state"


ENUMS = {e.name: e.enum_labels for e in CATALOG.entries() if e.enum_labels}


LABELS = set(ENUMS["inverter_state"].values())


ROW = re.compile(r'rct_inverter_state\{device="main",state="([^"]*)"\} (\d+)')


def _render_stateset(value: float | None, age: float = 0.0, log_name: str | None = None) -> tuple[str, MetricsExporter]:
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
    text, _ = _render_stateset(6)
    rows = _rows(text)
    assert set(rows) == LABELS and len(rows) == len(LABELS)
    assert rows["power_check"] == 1 and sum(rows.values()) == 1
    assert "# TYPE rct_inverter_state gauge" in text
    assert re.search(r"^rct_inverter_state\{device=\"main\"\} ", text, re.MULTILINE) is None  # no numeric code


def test_codes_sharing_a_label_light_that_label_once() -> None:
    for code in (0, 2):
        rows = _rows(_render_stateset(code)[0])
        assert rows["standby"] == 1 and sum(rows.values()) == 1


def test_unknown_code_exports_fixed_unknown_state_and_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.observability.exporter"):
        text, exporter = _render_stateset(99)
        exporter.render()
    rows = _rows(text)
    assert rows["unknown"] == 1 and sum(rows.values()) == 1 and len(rows) == len(LABELS) + 1
    assert "99" not in text
    assert len([r for r in caplog.records if "99" in r.getMessage()]) == 1


def test_expired_or_missing_value_has_no_state_rows() -> None:
    assert "rct_inverter_state" not in _render_stateset(6, age=31)[0]
    assert "rct_inverter_state" not in _render_stateset(None)[0]


def test_grace_value_keeps_states_and_reports_age() -> None:
    text, _ = _render_stateset(13, age=15)
    assert _rows(text)["feed_in"] == 1
    assert 'rct_device_metric_age_seconds{device="main",metric="inverter_state"} 15' in text
