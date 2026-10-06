#!/usr/bin/env python3
#
# tests/test_registry_preselected.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Registry-controlled exports and periodic reads (`preselected`)."""

import asyncio
import json

import pytest

from app.api.app_factory import _DASHBOARD_METRIC_NAMES, create_app
from app.catalog.registry import RegistryCatalog
from app.errors import ConfigError
from app.observability.names import build_metric_names
from app.scheduling.periodic import MAX_PERIODIC_PER_DEVICE
from tests.api_helpers import REGISTRY_FIXTURE, make_settings, running_app

CATALOG = RegistryCatalog.from_file(REGISTRY_FIXTURE)
PRESELECTED = list(CATALOG.preselected())
NUMERIC_PRESELECTED = [n for n in PRESELECTED if CATALOG.describe(n).value_type.value not in ("string", "object")]


def _registry_with_preselected(tmp_path, extra: int):
    data = json.loads(REGISTRY_FIXTURE.read_text(encoding="utf-8"))
    for i in range(extra):
        data["entries"].append(
            {
                "name": f"extra_metric_{i}",
                "object_id": f"0x7A{i:06X}",
                "data_type": "t_float",
                "unit": "",
                "value_type": "number",
                "writable": False,
                "idempotent_write": True,
                "is_action": False,
                "preselected": True,
                "description": "synthetic",
            }
        )
    path = tmp_path / "objects.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


async def _binding_periodic(h):
    binding = h.runtime.gateway._devices["main"]
    async with asyncio.timeout(3):
        while binding.periodic is not None and not binding.periodic.available:
            await asyncio.sleep(0.01)
    return binding.periodic


async def test_periodic_falls_back_to_numeric_preselected() -> None:
    async with running_app(make_settings()) as h:
        periodic = await _binding_periodic(h)
        assert periodic is not None
        expected = {CATALOG.object_entry(n).object_id for n in NUMERIC_PRESELECTED}
        # Dashboard card values ride along on the same plan when the registry knows them.
        expected |= {CATALOG.object_entry(n).object_id for n in _DASHBOARD_METRIC_NAMES if CATALOG.exists(n)}
        assert set(periodic.object_ids) == expected


async def test_explicit_periodic_metrics_win_and_switch_disables() -> None:
    async with running_app(make_settings(periodic_metrics=["battery_soc"])) as h:
        periodic = await _binding_periodic(h)
        assert periodic.object_ids == (CATALOG.object_entry("battery_soc").object_id,)
    async with running_app(make_settings(enable_periodic_reads=False)) as h:
        assert h.runtime.gateway._devices["main"].periodic is None


def test_more_than_64_preselected_is_a_config_error(tmp_path) -> None:
    path = _registry_with_preselected(tmp_path, MAX_PERIODIC_PER_DEVICE)
    with pytest.raises(ConfigError) as info:
        create_app(make_settings(object_registry_path=path))
    assert "preselected" in str(info.value.context["detail"]) and "64" in str(info.value.context["detail"])


async def test_exporter_shows_preselected_values_and_default_collection_exceeds_batch_limit() -> None:
    limit = 2
    assert len(PRESELECTED) > limit
    async with running_app(make_settings(max_metrics_per_request=limit, max_fresh_metrics_per_request=1)) as h:
        listed = await h.client.get("/api/v1/devices/main/metrics")
        assert listed.status_code == 200
        assert {m["name"] for m in listed.json()["metrics"]} == set(PRESELECTED)
        explicit = await h.client.get("/api/v1/devices/main/metrics?names=" + ",".join(PRESELECTED[: limit + 1]))
        assert explicit.status_code in (400, 422)
        too_big_fresh = await h.client.get("/api/v1/devices/main/metrics?fresh=true")
        assert too_big_fresh.status_code in (400, 422)
        text = (await h.client.get("/metrics")).text
        names = build_metric_names(e for e in CATALOG.entries() if e.name in NUMERIC_PRESELECTED)
        for name in NUMERIC_PRESELECTED:
            assert f"{names[name]}{{" in text, name
