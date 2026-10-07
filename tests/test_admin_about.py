#!/usr/bin/env python3
#
# tests/test_admin_about.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""About page routing, session guard, and rendered project metadata."""

import httpx
import pytest

from app import __version__
from app.api.app_factory import create_app
from app.config import Settings


@pytest.mark.asyncio
async def test_about_requires_session_and_renders_project_details(tmp_path):
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"))
    password = app.state.first_start_password
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        unauthenticated = await client.get("/ui/about", follow_redirects=False)
        assert unauthenticated.status_code == 303
        assert unauthenticated.headers["location"] == "/login"

        assert "admin-footer" not in (await client.get("/login")).text

        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post(
            "/admin/api/login",
            headers={"X-CSRF-Token": csrf},
            json={"username": "admin", "password": password},
        )
        assert login.status_code == 200
        changed = await client.post(
            "/admin/api/change-password",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={"current_password": password, "new_password": "a much stronger password"},
        )
        assert changed.status_code == 200

        response = await client.get("/ui/about")
        assert response.status_code == 200
        assert 'href="/ui/about" aria-current="page"' in response.text
        assert f"<code>{__version__}</code>" in response.text
        assert 'class="admin-footer"' in response.text and f"v{__version__}" in response.text
        assert "Application Details" in response.text
        assert "Dependencies" in response.text
        assert "<code>" in response.text
        assert response.text.count("/rct-manager/releases") == 1
        assert "https://gill-bates.github.io/rct-manager/" in response.text
        assert "https://github.com/Gill-Bates/rct-manager/releases" in response.text
        assert (await client.get("/admin/static/css/about.css")).status_code == 200
