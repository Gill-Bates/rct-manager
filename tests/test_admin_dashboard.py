#!/usr/bin/env python3
#
# tests/test_admin_dashboard.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Dashboard layout storage and the /admin/api/dashboard-layout endpoints."""

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from app.admin.energy_api import (
    _solar_dashboard_view,
    _spot_dashboard_view,
    dashboard_forecast,
)
from app.admin.forecast_refresh import spot_config_key
from app.api.app_factory import create_app
from app.clock import SystemClock
from app.config import Settings
from app.optimizer.models import SolarForecast, SolarForecastInterval
from app.spotmarket.models import (
    SpotMarketConfig,
    SpotMarketProvider,
    SpotPriceInterval,
    SpotPriceSnapshot,
)
from app.weather.models import WeatherConfig


@contextlib.asynccontextmanager
async def _logged_in(tmp_path) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Lifespan-aware admin client past the forced first-login password change, with its CSRF token."""
    app = create_app(
        Settings(
            _env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"
        )
    )
    password = app.state.first_start_password
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as client,
    ):
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post(
            "/admin/api/login",
            headers={"X-CSRF-Token": csrf},
            json={"username": "admin", "password": password},
        )
        csrf = login.json()["csrf_token"]
        changed = await client.post(
            "/admin/api/change-password",
            headers={"X-CSRF-Token": csrf},
            json={
                "current_password": password,
                "new_password": "a much stronger password",
            },
        )
        csrf = changed.json()["csrf_token"]
        accepted = await client.post(
            "/admin/api/accept-disclaimer", headers={"X-CSRF-Token": csrf}
        )
        assert accepted.status_code == 200
        yield client, csrf


_VALID_WIDGET = {"id": "pv-power", "x": 0, "y": 0, "w": 3, "h": 2, "visible": True}


def test_forecast_projection_converts_units_and_excludes_past_data():
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    solar = SolarForecast(
        created_at=now,
        intervals=(
            SolarForecastInterval(now - timedelta(hours=1), now, 1000),
            SolarForecastInterval(now, now + timedelta(minutes=30), 1500),
        ),
    )
    solar_view = _solar_dashboard_view(solar, now)
    assert solar_view["status"] == "available"
    assert len(solar_view["future_curve"]) == 1
    assert solar_view["future_curve"][0]["power_kw"] == 3.0

    current_interval = SpotPriceInterval(
        now, now + timedelta(minutes=15), Decimal("0.124")
    )
    next_interval = SpotPriceInterval(
        now + timedelta(days=1), now + timedelta(days=1, minutes=15), Decimal("0.2")
    )
    stored = SpotPriceSnapshot(
        SpotMarketProvider.ENERGY_CHARTS,
        "DE-LU",
        "EUR",
        now - timedelta(hours=20),
        (current_interval, next_interval),
        timezone="Europe/Berlin",
        source_generated_at=now - timedelta(minutes=3),
    )
    config = SpotMarketConfig(enabled=True)
    # The data is a day old (one retrieval per day) and still "available": age alone is no criterion.
    spot_view = _spot_dashboard_view(config, stored, False, now)
    assert (
        spot_view["status"] == "available"
        and spot_view["current_status"] == "available"
    )
    assert spot_view["current_price_ct_per_kwh"] == 12.4
    assert spot_view["future_curve"][0]["price_ct_per_kwh"] == 12.4
    assert spot_view["timezone"] == "Europe/Berlin"
    assert spot_view["source_generated_at"] == (now - timedelta(minutes=3)).isoformat()
    assert spot_view["current_valid_until"] == (now + timedelta(minutes=15)).isoformat()
    failed_view = _spot_dashboard_view(config, stored, True, now)
    assert failed_view["status"] == "stale" and failed_view["current_status"] == "stale"
    assert failed_view["current_price_ct_per_kwh"] == 12.4
    gap_view = _spot_dashboard_view(config, stored, False, now + timedelta(hours=1))
    assert gap_view["current_status"] == "stale"
    assert gap_view["current_price_ct_per_kwh"] is None


@pytest.mark.asyncio
async def test_forecast_projection_requires_admin_auth(tmp_path):
    app = create_app(
        Settings(
            _env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "anon.db"
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (
            await client.get("/admin/api/energy/dashboard-forecast")
        ).status_code == 401


@pytest.mark.asyncio
async def test_logged_in_forecast_projection_reports_disabled_for_both_sections(
    tmp_path,
):
    # A fresh app has spot market and the weather forecast both disabled, so each section reports its
    # published "disabled" state (not "error"/"unavailable", which signal a configured-but-failed or
    # not-yet-cached forecast). Solar mirrors spot here: a non-forecastable WeatherConfig is disabled.
    async with _logged_in(tmp_path) as (client, _csrf):
        response = await client.get(
            "/admin/api/energy/dashboard-forecast?include=spot_current&include=spot_day_ahead&include=solar"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["spot"]["status"] == "disabled"
        assert data["spot"]["current_status"] == "disabled"
        assert data["spot"]["future_curve"] == []
        assert data["solar"]["status"] == "disabled"
        assert data["solar"]["future_curve"] == []


@pytest.mark.asyncio
async def test_forecast_projection_only_reads_requested_caches():
    calls = []

    def get_spot(config_key):
        calls.append("day_ahead")
        return []

    state = SimpleNamespace(
        spotmarket_config=SpotMarketConfig(enabled=True),
        admin_store=SimpleNamespace(get_spot_market_intervals=get_spot),
        runtime=SimpleNamespace(clock=SystemClock()),
    )
    request = Request({"type": "http", "app": SimpleNamespace(state=state)})
    current = await dashboard_forecast(request, None, ["spot_current"])
    assert list(current) == ["spot"]
    assert calls == ["day_ahead"]
    calls.clear()
    day_ahead = await dashboard_forecast(request, None, ["spot_day_ahead"])
    assert list(day_ahead) == ["spot"]
    assert calls == ["day_ahead"]
    calls.clear()
    solar = await dashboard_forecast(request, None, ["solar"])
    assert list(solar) == ["solar"]
    assert calls == []


@pytest.mark.asyncio
async def test_dashboard_forecast_without_an_admin_store_is_a_graceful_503_not_a_500():
    """A missing admin store must yield the same 503 the other admin endpoints return, never an
    unhandled AttributeError -> 500. (In the running app the admin router is only mounted with a
    store present; this guards the handler's own contract against a stateless request.)
    """
    from fastapi import HTTPException

    state = SimpleNamespace(
        spotmarket_config=SpotMarketConfig(enabled=True), admin_store=None
    )
    request = Request({"type": "http", "app": SimpleNamespace(state=state)})
    with pytest.raises(HTTPException) as excinfo:
        await dashboard_forecast(request, None, ["spot_current"])
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_dashboard_empty_solar_cache_is_unavailable():
    config = WeatherConfig(
        enabled=True,
        latitude=51.18,
        longitude=7.19,
        timezone="Europe/Berlin",
        pv_peak_power_wp=10000,
        tilt_deg=35,
        azimuth_deg=0,
    )
    state = SimpleNamespace(
        spotmarket_config=SpotMarketConfig(),
        weather_config=config,
        admin_store=SimpleNamespace(get_solar_forecast_intervals=lambda *args: []),
        runtime=SimpleNamespace(clock=SystemClock()),
    )
    request = Request({"type": "http", "app": SimpleNamespace(state=state)})
    result = await dashboard_forecast(request, None, ["solar"])
    assert result["solar"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_dashboard_current_price_comes_from_the_stored_intervals(tmp_path):
    async with _logged_in(tmp_path) as (client, _csrf):
        app = client._transport.app
        config = SpotMarketConfig(enabled=True)
        app.state.spotmarket_config = config
        start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(
            minutes=1
        )
        end = start + timedelta(minutes=15)
        app.state.admin_store.replace_spot_market_intervals(
            [
                {
                    "config_key": spot_config_key(config),
                    "local_date": start.date().isoformat(),
                    "interval_start": start.isoformat(),
                    "interval_end": end.isoformat(),
                    "price_eur_per_kwh": 0.37,
                    "timezone": "UTC",
                    "fetched_at": start.isoformat(),
                }
            ]
        )
        response = await client.get(
            "/admin/api/energy/dashboard-forecast?include=spot_current"
        )
        assert response.status_code == 200
        spot = response.json()["spot"]
        assert spot["current_price_ct_per_kwh"] == 37
        assert spot["current_valid_until"] == end.isoformat()


@pytest.mark.asyncio
async def test_forecast_projection_rejects_invalid_selection(tmp_path):
    async with _logged_in(tmp_path) as (client, _csrf):
        for query in (
            "",
            "?include=unknown",
            "?include=spot_current&include=spot_current",
        ):
            response = await client.get(f"/admin/api/energy/dashboard-forecast{query}")
            assert response.status_code == 422


def _layout(*widgets) -> dict:
    return {"version": 1, "widgets": list(widgets)}


@pytest.mark.asyncio
async def test_get_layout_with_none_stored(tmp_path):
    async with _logged_in(tmp_path) as (client, _csrf):
        response = await client.get("/admin/api/dashboard-layout")
        assert response.status_code == 200
        assert response.json() == {"layout": None}


@pytest.mark.asyncio
async def test_put_then_get_returns_the_stored_layout(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout(
            _VALID_WIDGET,
            {"id": "devices", "x": 0, "y": 4, "w": 12, "h": 8, "visible": True},
        )
        put = await client.put(
            "/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body
        )
        assert put.status_code == 200
        assert put.json() == {"layout": body}
        get = await client.get("/admin/api/dashboard-layout")
        assert get.json() == {"layout": body}


@pytest.mark.asyncio
async def test_hidden_placed_widget_round_trips(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout(
            {
                "id": "spot-price-curve",
                "x": 6,
                "y": 20,
                "w": 6,
                "h": 4,
                "visible": False,
                "placed": True,
            }
        )
        response = await client.put(
            "/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body
        )
        assert response.status_code == 200
        assert (await client.get("/admin/api/dashboard-layout")).json() == {
            "layout": body
        }


@pytest.mark.asyncio
async def test_days_to_calibration_widget_is_in_the_allowlist(tmp_path):
    # The "x days to next calibration" tile is a real, addable widget: a layout that places it must
    # pass _validate_dashboard_layout, exactly like the other hero/KPI tiles.
    from app.admin.api import _DASHBOARD_WIDGETS

    assert "days-to-calibration" in _DASHBOARD_WIDGETS
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout({**_VALID_WIDGET, "id": "days-to-calibration"})
        response = await client.put(
            "/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body
        )
        assert response.status_code == 200
        assert response.json() == {"layout": body}


@pytest.mark.asyncio
async def test_delete_removes_the_stored_layout(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout(_VALID_WIDGET)
        await client.put(
            "/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body
        )
        deleted = await client.delete(
            "/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"layout": None}
        assert (await client.get("/admin/api/dashboard-layout")).json() == {
            "layout": None
        }


@pytest.mark.asyncio
async def test_put_requires_authentication(tmp_path):
    app = create_app(
        Settings(
            _env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "anon.db"
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as client:
        response = await client.put(
            "/admin/api/dashboard-layout", json=_layout(_VALID_WIDGET)
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_put_requires_valid_csrf(tmp_path):
    async with _logged_in(tmp_path) as (client, _csrf):
        response = await client.put(
            "/admin/api/dashboard-layout", json=_layout(_VALID_WIDGET)
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_unknown_widget_id_is_rejected(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout({**_VALID_WIDGET, "id": "not-a-real-widget"})
        response = await client.put(
            "/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_widget_id_is_rejected(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout(_VALID_WIDGET, {**_VALID_WIDGET, "x": 3})
        response = await client.put(
            "/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_width_exceeding_the_grid_is_rejected(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout({**_VALID_WIDGET, "x": 10, "w": 5})
        response = await client.put(
            "/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_height_exceeding_the_vertical_bound_is_rejected(tmp_path):
    # A widget whose top is pinned at the max row but whose body spills past the vertical bound
    # (y + h > 1000) is rejected, mirroring the horizontal x + w <= 12 rule. Such a layout cannot be
    # resolved below the bound and would otherwise force the client resolver to discard it.
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout({**_VALID_WIDGET, "y": 1000, "h": 2})
        response = await client.put(
            "/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_negative_position_is_rejected(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout({**_VALID_WIDGET, "x": -1})
        response = await client.put(
            "/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_oversized_widget_list_is_rejected(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        widgets = [{**_VALID_WIDGET, "id": "pv-power", "y": i} for i in range(33)]
        response = await client.put(
            "/admin/api/dashboard-layout",
            headers={"X-CSRF-Token": csrf},
            json=_layout(*widgets),
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_unknown_field_is_rejected():
    """extra="forbid": a stored widget record must never carry arbitrary extra data such as HTML."""
    from pydantic import ValidationError

    from app.admin.api import DashboardWidgetLayout

    with pytest.raises(ValidationError):
        DashboardWidgetLayout(
            id="pv-power", x=0, y=0, w=3, h=2, content="<script>alert(1)</script>"
        )


@pytest.mark.asyncio
async def test_stored_layout_survives_a_new_app_and_store_access(tmp_path):
    db_path = tmp_path / "rct.db"
    body = _layout(_VALID_WIDGET)
    first = create_app(
        Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=db_path)
    )
    password = first.state.first_start_password
    async with (
        first.router.lifespan_context(first),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first),
            base_url="http://testserver",
            headers={"Origin": "http://testserver"},
        ) as client,
    ):
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post(
            "/admin/api/login",
            headers={"X-CSRF-Token": csrf},
            json={"username": "admin", "password": password},
        )
        csrf = login.json()["csrf_token"]
        changed = await client.post(
            "/admin/api/change-password",
            headers={"X-CSRF-Token": csrf},
            json={
                "current_password": password,
                "new_password": "a much stronger password",
            },
        )
        csrf = changed.json()["csrf_token"]
        await client.post(
            "/admin/api/accept-disclaimer", headers={"X-CSRF-Token": csrf}
        )
        await client.put(
            "/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body
        )

    restarted = create_app(
        Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=db_path)
    )
    # The layout is reconstructed from its own dedicated table, not read back from the settings blob.
    assert restarted.state.admin_store.get_dashboard_layout() == body
    assert restarted.state.admin_store.get("dashboard_layout") is None


def _make_store(tmp_path):
    from app.admin.store import AdminStore

    store = AdminStore(tmp_path / "layout.db", "s" * 48)
    store.initialize()
    return store


def test_dashboard_layout_table_has_the_expected_strict_shape(tmp_path):
    store = _make_store(tmp_path)
    with store.connect() as db:
        layout_sql = db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='dashboard_layout'"
        ).fetchone()["sql"]
        meta_sql = db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='dashboard_layout_meta'"
        ).fetchone()["sql"]
    assert "widget_id TEXT PRIMARY KEY" in layout_sql
    assert "position INTEGER NOT NULL" in layout_sql
    assert "visible INTEGER NOT NULL CHECK (visible IN (0, 1))" in layout_sql
    assert layout_sql.strip().endswith("STRICT;") or layout_sql.rstrip().endswith(
        "STRICT"
    )
    assert "version INTEGER NOT NULL" in meta_sql
    assert "STRICT" in meta_sql


def test_dashboard_layout_store_round_trip_preserves_order_and_version(tmp_path):
    store = _make_store(tmp_path)
    layout = {
        "version": 1,
        "widgets": [
            {"id": "devices", "x": 0, "y": 4, "w": 12, "h": 8, "visible": True},
            {"id": "pv-power", "x": 0, "y": 0, "w": 3, "h": 2, "visible": False},
            {"id": "battery-soc", "x": 3, "y": 0, "w": 3, "h": 2, "visible": True},
        ],
    }
    store.replace_dashboard_layout(layout)
    read_back = store.get_dashboard_layout()
    # Exact JSON shape, widget order and per-field values survive the structured round-trip.
    assert read_back == layout
    assert [w["id"] for w in read_back["widgets"]] == [
        "devices",
        "pv-power",
        "battery-soc",
    ]


def test_dashboard_layout_store_replace_is_atomic_and_total(tmp_path):
    store = _make_store(tmp_path)
    store.replace_dashboard_layout(
        _layout(
            {"id": "pv-power", "x": 0, "y": 0, "w": 3, "h": 2, "visible": True},
            {"id": "devices", "x": 0, "y": 4, "w": 12, "h": 8, "visible": True},
        )
    )
    # A second replace must not leave orphan rows from the first layout.
    store.replace_dashboard_layout(
        _layout(
            {"id": "battery-soc", "x": 0, "y": 0, "w": 3, "h": 2, "visible": True},
        )
    )
    read_back = store.get_dashboard_layout()
    assert [w["id"] for w in read_back["widgets"]] == ["battery-soc"]


def test_dashboard_layout_store_empty_and_clear_map_to_none(tmp_path):
    store = _make_store(tmp_path)
    assert store.get_dashboard_layout() is None
    store.replace_dashboard_layout(
        _layout(
            {"id": "pv-power", "x": 0, "y": 0, "w": 3, "h": 2, "visible": True},
        )
    )
    assert store.get_dashboard_layout() is not None
    store.clear_dashboard_layout()
    assert store.get_dashboard_layout() is None
    # Clearing an already-empty layout is idempotent, not an error.
    store.clear_dashboard_layout()
    assert store.get_dashboard_layout() is None
