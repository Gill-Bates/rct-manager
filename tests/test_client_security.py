#!/usr/bin/env python3
#
# tests/test_secret_guard.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Credentials stay out of the read API and cannot be re-enabled through a mounted registry."""

import json
from pathlib import Path

import pytest

from app.allowlist import Allowlist
from app.catalog.registry import RegistryCatalog
from app.errors import ConfigError
from tests.api_helpers import make_settings, running_app

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SECRET = {
    "name": "wifi_password",
    "object_id": "0x14C0E627",
    "data_type": "t_string",
    "unit": "",
    "value_type": "string",
    "idempotent_write": False,
    "is_action": False,
    "preselected": False,
    "description": "wifi.password",
    "writable": True,
}


def test_shipped_files_do_not_contain_the_wifi_password() -> None:
    catalog = RegistryCatalog.from_file(ROOT / "app/catalog/objects.json")
    allowed = Allowlist.load(ROOT / "app/catalog/default_write_allowlist.json", catalog)
    assert "wifi_password" not in {e.name for e in catalog.entries()}
    assert allowed.entry("wifi_password") is None
    assert "wifi_authentication_method" in {e.name for e in catalog.entries()}  # an algorithm name, no secret


async def test_wifi_password_is_unknown_over_the_read_api() -> None:
    settings = make_settings(
        object_registry_path=ROOT / "app/catalog/objects.json", write_allowlist_path=ROOT / "app/catalog/default_write_allowlist.json"
    )
    async with running_app(settings, settle=False) as h:
        single = await h.client.get("/api/v1/devices/main/metrics/wifi_password")
        batch = await h.client.get("/api/v1/devices/main/metrics", params={"names": "wifi_password"})
    assert single.status_code == 404 and single.json()["code"] == "unknown_metric"
    assert batch.status_code == 422


@pytest.mark.parametrize("name", ["wifi_password", "renamed_secret"])
def test_registry_with_the_wifi_password_aborts_the_start(tmp_path: Path, name: str) -> None:
    raw = json.loads((FIXTURES / "objects.json").read_text(encoding="utf-8"))
    raw["entries" if "entries" in raw else "objects"].append({**SECRET, "name": name})
    path = tmp_path / "objects_read.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        RegistryCatalog.from_file(path)
    assert "secret" in exc.value.context["detail"]


def test_allowlist_with_the_wifi_password_aborts_the_start(tmp_path: Path) -> None:
    catalog = RegistryCatalog.from_file(FIXTURES / "objects.json")
    path = tmp_path / "objects_write_allowed.json"
    path.write_text(
        json.dumps({"version": 1, "entries": [{"name": "wifi_password", "data_type": "t_string"}]}), encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        Allowlist.load(path, catalog)
