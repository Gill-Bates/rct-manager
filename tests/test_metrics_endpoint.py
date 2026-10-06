#!/usr/bin/env python3
#
# tests/test_metrics_endpoint.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Property 19: metric names and labels separate device and transport endpoint."""

import math
import re
from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from app.cache import MemoryCache
from app.catalog.registry import RegistryCatalog
from app.observability.exporter import DeviceView, EndpointView, MetricsExporter
from app.observability.names import build_metric_names
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


# Feature: rct-rest-api, Property 19: metric names and labels separate device and transport endpoint
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
