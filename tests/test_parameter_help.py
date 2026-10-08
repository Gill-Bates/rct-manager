#!/usr/bin/env python3
#
# tests/test_parameter_help.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Operator-facing explanation of a writable parameter: catalog field, admin payload, GUI markup."""

import re
from pathlib import Path

import httpx
import pytest

from app.api.app_factory import create_app
from app.catalog.registry import RegistryCatalog, RegistryEntry
from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
ADMIN_JS = ROOT / "app" / "admin" / "static" / "js" / "admin.js"
# Documented in docs/operation.md, the catalog's own enum labels and the shipped allowlist.
DOCUMENTED = "com_service"
# Carries only its vendor object path, so the catalog cannot explain what it stands for.
UNDOCUMENTED = "wifi_server_ip"


async def _logged_in(tmp_path):
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"))
    password = app.state.first_start_password
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}
    )
    await client.__aenter__()
    csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
    login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                              json={"username": "admin", "password": password})
    await client.post("/admin/api/change-password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                      json={"current_password": password, "new_password": "a much stronger password"})
    return client


def _entry(**overrides) -> dict:
    base = {"name": "synthetic", "object_id": "0x00000001", "data_type": "t_float", "unit": "W",
            "value_type": "number", "idempotent_write": True, "writable": True}
    return base | overrides


def test_help_text_is_optional_so_an_existing_catalog_keeps_validating():
    """Backward compatibility: a registry file written before the field stays valid and renders nothing."""
    assert RegistryEntry.model_validate(_entry()).help_text == ""
    assert RegistryEntry.model_validate(_entry(help_text="What it stands for.")).help_text == "What it stands for."


def test_shipped_catalog_explains_only_writable_parameters_it_can_substantiate():
    catalog = RegistryCatalog.from_file(ROOT / "app/catalog/objects.json")
    explained = [e for e in catalog.entries() if e.help_text]
    assert explained, "the shipped catalog should explain at least the documented writable parameters"
    for entry in explained:
        assert entry.writable, f"{entry.name}: an explanation belongs to a writable parameter"
        # The GUI already shows name and description; an explanation that repeats either adds nothing.
        assert entry.help_text.strip() not in (entry.name, entry.description)
        assert not re.search(r"[äöüßÄÖÜ]", entry.help_text), f"{entry.name}: GUI text is English"
    assert catalog.object_entry(DOCUMENTED).help_text
    # Deliberately left empty rather than guessed; the GUI must cope with that for most parameters.
    assert catalog.object_entry(UNDOCUMENTED).help_text == ""


@pytest.mark.asyncio
async def test_admin_parameters_carry_the_explanation_for_the_gui(tmp_path):
    client = await _logged_in(tmp_path)
    available = (await client.get("/admin/api/parameters")).json()["available"]
    by_name = {item["name"]: item for item in available}
    assert all("help_text" in item for item in available)
    assert "action" in by_name[DOCUMENTED]["help_text"].lower()
    assert by_name[UNDOCUMENTED]["writable"] is True
    assert by_name[UNDOCUMENTED]["help_text"] == ""
    await client.aclose()


def test_writable_rows_render_the_explanation_as_text_tied_to_the_checkbox():
    """Visible text, not a tooltip, and described-by so the checkbox announces it; nothing when empty."""
    js = ADMIN_JS.read_text(encoding="utf-8")
    assert "if (item.help_text) {" in js
    assert "element('small', 'parameter-help', item.help_text)" in js
    assert "box.setAttribute('aria-describedby', help.id)" in js
    assert "help.id = `write-help-${item.name}`" in js
    # The search over the writable list covers the explanation as well.
    assert "${item.help_text || ''}" in js


def test_inverters_page_names_the_explanation_and_the_limit_semantics():
    html = (ROOT / "app/admin/templates/inverters.html").read_text(encoding="utf-8")
    assert "its explanation appears under the name" in html
    assert "wire type limits, not safe operating limits" in html
