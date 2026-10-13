#!/usr/bin/env python3
#
# tests/test_dashboard_widget_registry.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Anti-drift guard for the dashboard widget registry.

The widget id identity used to be hand-synced across three sites (the dashboard.html DOM blocks,
DEFAULT_LAYOUT in dashboard.js, and _DASHBOARD_WIDGETS in app/admin/api.py). Phase 1 introduces a
shared id source, app/admin/widget_ids.json, that the JS registry and the Python allowlist are both
tied to BY TEST, not by runtime IO: _DASHBOARD_WIDGETS stays a hand-written literal frozenset and the
import path reads no file. This module is the only code that opens widget_ids.json; if the literal and
the JSON ever drift, these tests fail (design §8.2).
"""

import json
from pathlib import Path

import pytest

import app.admin
from app.admin.api import _DASHBOARD_WIDGETS, DashboardLayout, DashboardWidgetLayout, _validate_dashboard_layout

_WIDGET_IDS_JSON = Path(app.admin.__file__).parent / "widget_ids.json"


def _load_widget_ids() -> list[str]:
    return json.loads(_WIDGET_IDS_JSON.read_text(encoding="utf-8"))


def test_widget_ids_json_is_well_formed():
    """The shared id source parses to all thirteen available ids, with no duplicates."""
    ids = _load_widget_ids()
    assert isinstance(ids, list)
    assert all(isinstance(widget_id, str) for widget_id in ids)
    assert len(ids) == 13
    assert len(set(ids)) == len(ids)


def test_python_allowlist_set_equals_the_shared_id_source():
    """_DASHBOARD_WIDGETS (hand-written literal, no import-time IO) set-equals widget_ids.json."""
    assert _DASHBOARD_WIDGETS == frozenset(_load_widget_ids())


def test_validate_dashboard_layout_accepts_every_registry_id():
    """A layout placing every registered id once passes validation unchanged."""
    widgets = [
        DashboardWidgetLayout(id=widget_id, x=0, y=i, w=2, h=2, visible=True)
        for i, widget_id in enumerate(_load_widget_ids())
    ]
    # No HTTPException raised.
    _validate_dashboard_layout(DashboardLayout(version=1, widgets=widgets))


def test_validate_dashboard_layout_rejects_an_unknown_id():
    """Regression guard: an id outside the allowlist is still rejected (422)."""
    from fastapi import HTTPException

    body = DashboardLayout(
        version=1,
        widgets=[DashboardWidgetLayout(id="not-a-real-widget", x=0, y=0, w=2, h=2, visible=True)],
    )
    with pytest.raises(HTTPException) as excinfo:
        _validate_dashboard_layout(body)
    assert excinfo.value.status_code == 422


@pytest.mark.parametrize("widget_id", ["solar-forecast-status", "pv-forecast-curve"])
def test_unavailable_weather_widgets_are_not_selectable(widget_id):
    from fastapi import HTTPException

    body = DashboardLayout(version=1, widgets=[DashboardWidgetLayout(
        id=widget_id, x=0, y=0, w=2, h=2, visible=True,
    )])
    with pytest.raises(HTTPException) as excinfo:
        _validate_dashboard_layout(body)
    assert excinfo.value.status_code == 422
