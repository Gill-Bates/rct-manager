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

from app.admin.energy_api import _solar_dashboard_view, _spot_dashboard_view, dashboard_forecast
from app.api.app_factory import create_app
from app.config import Settings
from app.optimizer.models import SolarForecast, SolarForecastInterval
from app.spotmarket.models import SpotMarketConfig, SpotMarketProvider, SpotPriceInterval, SpotPriceSnapshot


@contextlib.asynccontextmanager
async def _logged_in(tmp_path) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Lifespan-aware admin client past the forced first-login password change, with its CSRF token."""
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"))
    password = app.state.first_start_password
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client,
    ):
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        csrf = login.json()["csrf_token"]
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": csrf},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        yield client, changed.json()["csrf_token"]


_VALID_WIDGET = {"id": "pv-power", "x": 0, "y": 0, "w": 3, "h": 2, "visible": True}


def test_forecast_projection_converts_units_and_excludes_past_data():
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    solar = SolarForecast(created_at=now, intervals=(
        SolarForecastInterval(now - timedelta(hours=1), now, 1000),
        SolarForecastInterval(now, now + timedelta(minutes=30), 1500),
    ))
    solar_view = _solar_dashboard_view(solar, now)
    assert solar_view["status"] == "available"
    assert len(solar_view["future_curve"]) == 1
    assert solar_view["future_curve"][0]["power_kw"] == 3.0

    current_interval = SpotPriceInterval(now, now + timedelta(minutes=15), Decimal("0.124"))
    next_interval = SpotPriceInterval(now + timedelta(days=1), now + timedelta(days=1, minutes=15), Decimal("0.2"))
    current = SpotPriceSnapshot(
        SpotMarketProvider.ENERGY_CHARTS, "DE-LU", "EUR", now, (current_interval,),
        valid_until=now + timedelta(minutes=15),
    )
    future = SpotPriceSnapshot(
        SpotMarketProvider.ENERGY_CHARTS, "DE-LU", "EUR", now, (next_interval,),
        timezone="Europe/Berlin", source_generated_at=now - timedelta(minutes=3),
    )
    spot_view = _spot_dashboard_view(SpotMarketConfig(enabled=True), future, False, current, False, now)
    assert spot_view["current_price_ct_per_kwh"] == 12.4
    assert spot_view["future_curve"][0]["price_ct_per_kwh"] == 20.0
    assert spot_view["timezone"] == "Europe/Berlin"
    assert spot_view["source_generated_at"] == (now - timedelta(minutes=3)).isoformat()
    assert spot_view["current_valid_until"] == (now + timedelta(minutes=15)).isoformat()
    stale_view = _spot_dashboard_view(SpotMarketConfig(enabled=True), future, True, current, False, now)
    assert stale_view["status"] == "stale"
    assert len(stale_view["future_curve"]) == 1
    delayed_view = _spot_dashboard_view(SpotMarketConfig(enabled=True), future, False, current, True, now)
    assert delayed_view["current_status"] == "stale"
    assert delayed_view["current_price_ct_per_kwh"] == 12.4
    expired_view = _spot_dashboard_view(
        SpotMarketConfig(enabled=True), future, False, current, True, now + timedelta(minutes=15),
    )
    assert expired_view["current_status"] == "stale"
    assert expired_view["current_price_ct_per_kwh"] is None


@pytest.mark.asyncio
async def test_forecast_projection_requires_admin_auth(tmp_path):
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "anon.db"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.get("/admin/api/energy/dashboard-forecast")).status_code == 401


@pytest.mark.asyncio
async def test_logged_in_forecast_projection_reports_disabled_and_unavailable(tmp_path):
    async with _logged_in(tmp_path) as (client, _csrf):
        response = await client.get("/admin/api/energy/dashboard-forecast?include=spot_current&include=spot_day_ahead&include=solar")
        assert response.status_code == 200
        data = response.json()
        assert data["spot"]["status"] == "disabled"
        assert data["spot"]["current_status"] == "disabled"
        assert data["spot"]["future_curve"] == []
        assert data["solar"]["status"] == "unavailable"
        assert data["solar"]["future_curve"] == []


@pytest.mark.asyncio
async def test_forecast_projection_only_reads_requested_caches():
    calls = []

    def cache(name):
        def get(_config):
            calls.append(name)
            return None, False

        return SimpleNamespace(get=get)

    state = SimpleNamespace(
        spotmarket_config=SpotMarketConfig(enabled=True),
        spotmarket_current_cache=cache("current"),
        spotmarket_snapshot_cache=cache("day_ahead"),
    )
    request = Request({"type": "http", "app": SimpleNamespace(state=state)})
    current = await dashboard_forecast(request, None, ["spot_current"])
    assert list(current) == ["spot"]
    assert calls == ["current"]
    calls.clear()
    day_ahead = await dashboard_forecast(request, None, ["spot_day_ahead"])
    assert list(day_ahead) == ["spot"]
    assert calls == ["day_ahead"]
    calls.clear()
    solar = await dashboard_forecast(request, None, ["solar"])
    assert list(solar) == ["solar"]
    assert calls == []


@pytest.mark.asyncio
async def test_forecast_projection_rejects_invalid_selection(tmp_path):
    async with _logged_in(tmp_path) as (client, _csrf):
        for query in ("", "?include=unknown", "?include=spot_current&include=spot_current"):
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
        body = _layout(_VALID_WIDGET, {"id": "devices", "x": 0, "y": 4, "w": 12, "h": 8, "visible": True})
        put = await client.put("/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body)
        assert put.status_code == 200
        assert put.json() == {"layout": body}
        get = await client.get("/admin/api/dashboard-layout")
        assert get.json() == {"layout": body}


@pytest.mark.asyncio
async def test_days_to_calibration_widget_is_in_the_allowlist(tmp_path):
    # The "x days to next calibration" tile is a real, addable widget: a layout that places it must
    # pass _validate_dashboard_layout, exactly like the other hero/KPI tiles.
    from app.admin.api import _DASHBOARD_WIDGETS

    assert "days-to-calibration" in _DASHBOARD_WIDGETS
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout({**_VALID_WIDGET, "id": "days-to-calibration"})
        response = await client.put("/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body)
        assert response.status_code == 200
        assert response.json() == {"layout": body}


@pytest.mark.asyncio
async def test_delete_removes_the_stored_layout(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout(_VALID_WIDGET)
        await client.put("/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body)
        deleted = await client.delete("/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf})
        assert deleted.status_code == 200
        assert deleted.json() == {"layout": None}
        assert (await client.get("/admin/api/dashboard-layout")).json() == {"layout": None}


@pytest.mark.asyncio
async def test_put_requires_authentication(tmp_path):
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "anon.db"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        response = await client.put("/admin/api/dashboard-layout", json=_layout(_VALID_WIDGET))
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_put_requires_valid_csrf(tmp_path):
    async with _logged_in(tmp_path) as (client, _csrf):
        response = await client.put("/admin/api/dashboard-layout", json=_layout(_VALID_WIDGET))
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_unknown_widget_id_is_rejected(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout({**_VALID_WIDGET, "id": "not-a-real-widget"})
        response = await client.put("/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body)
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_widget_id_is_rejected(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout(_VALID_WIDGET, {**_VALID_WIDGET, "x": 3})
        response = await client.put("/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body)
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_width_exceeding_the_grid_is_rejected(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout({**_VALID_WIDGET, "x": 10, "w": 5})
        response = await client.put("/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body)
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_negative_position_is_rejected(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        body = _layout({**_VALID_WIDGET, "x": -1})
        response = await client.put("/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body)
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_oversized_widget_list_is_rejected(tmp_path):
    async with _logged_in(tmp_path) as (client, csrf):
        widgets = [{**_VALID_WIDGET, "id": "pv-power", "y": i} for i in range(33)]
        response = await client.put("/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf},
                                    json=_layout(*widgets))
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_unknown_field_is_rejected():
    """extra="forbid": a stored widget record must never carry arbitrary extra data such as HTML."""
    from pydantic import ValidationError

    from app.admin.api import DashboardWidgetLayout

    with pytest.raises(ValidationError):
        DashboardWidgetLayout(id="pv-power", x=0, y=0, w=3, h=2, content="<script>alert(1)</script>")


@pytest.mark.asyncio
async def test_stored_layout_survives_a_new_app_and_store_access(tmp_path):
    db_path = tmp_path / "rct.db"
    body = _layout(_VALID_WIDGET)
    first = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=db_path))
    password = first.state.first_start_password
    async with (
        first.router.lifespan_context(first),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client,
    ):
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        csrf = login.json()["csrf_token"]
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": csrf},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        csrf = changed.json()["csrf_token"]
        await client.put("/admin/api/dashboard-layout", headers={"X-CSRF-Token": csrf}, json=body)

    restarted = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=db_path))
    assert restarted.state.admin_store.get("dashboard_layout") == body
