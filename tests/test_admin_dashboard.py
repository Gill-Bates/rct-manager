#!/usr/bin/env python3
#
# tests/test_admin_dashboard.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Dashboard layout storage and the /admin/api/dashboard-layout endpoints."""

import contextlib
from collections.abc import AsyncIterator

import httpx
import pytest

from app.api.app_factory import create_app
from app.config import Settings


@contextlib.asynccontextmanager
async def _logged_in(tmp_path) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Lifespan-aware admin client past the forced first-login password change, with its CSRF token."""
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"))
    password = app.state.first_start_password
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        csrf = login.json()["csrf_token"]
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": csrf},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        yield client, changed.json()["csrf_token"]


_VALID_WIDGET = {"id": "pv-power", "x": 0, "y": 0, "w": 3, "h": 2, "visible": True}


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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
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
        httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url="http://testserver") as client,
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
