#!/usr/bin/env python3
#
# tests/test_review_findings_j.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Regressions for the external review: readback comparison, allowlist rules, registry version, secret handover."""

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from pydantic import ValidationError

from app.allowlist import Allowlist, AllowlistEntry
from app.api.server import (
    FIRST_START_PASSWORD_FILE,
    GracefulServer,
    write_first_start_password,
)
from app.catalog.registry import ObjectRegistry
from app.errors import ConfigError, WriteRejected
from app.protocol.types import Command, DataType
from tests.api_helpers import (
    ACTION_NAME,
    TARGET_NAME,
    TARGET_OBJECT_ID,
    WRITE_TOKEN,
    float_payload,
    make_settings,
    running_app,
    write_fixtures,
)

AUTH = {"Authorization": f"Bearer {WRITE_TOKEN}"}
BOOL_NAME, BOOL_ID = "switch_flag", 0x1234ABCE
_IGNORE_WRITE = lambda frame: "ignore" if frame.command is Command.WRITE else "respond"


def _settings_with_bool(tmp_path: Path):
    paths = write_fixtures(tmp_path)
    reg = json.loads(paths["object_registry_path"].read_text(encoding="utf-8"))
    reg["entries"].append(
        {
            "name": BOOL_NAME,
            "object_id": f"0x{BOOL_ID:08X}",
            "data_type": "t_bool",
            "unit": "none",
            "value_type": "boolean",
            "writable": True,
            "idempotent_write": True,
            "is_action": False,
            "preselected": False,
        }
    )
    paths["object_registry_path"].write_text(json.dumps(reg), encoding="utf-8")
    allow = json.loads(paths["write_allowlist_path"].read_text(encoding="utf-8"))
    allow["entries"].append({"name": BOOL_NAME, "data_type": "t_bool"})
    paths["write_allowlist_path"].write_text(json.dumps(allow), encoding="utf-8")
    return make_settings(enable_write_support=True, **paths)


async def _put(h, name: str, value, readback: bytes):
    h.net.freeze_writes = True  # the test dictates what the device reads back
    h.net.payloads[BOOL_ID if name == BOOL_NAME else TARGET_OBJECT_ID] = readback
    return await h.client.put(f"/api/v1/devices/main/metrics/{name}", json={"value": value}, headers=AUTH)


async def test_bool_write_is_confirmed_by_a_nonzero_readback(tmp_path) -> None:
    async with running_app(_settings_with_bool(tmp_path), behavior=_IGNORE_WRITE) as h:
        response = await _put(h, BOOL_NAME, True, b"\x07")
        assert response.status_code == 200, response.text
        assert response.json()["confirmed"] is True and response.json()["readback_value"] is True


async def test_bool_write_with_opposite_readback_stays_unconfirmed(tmp_path) -> None:
    async with running_app(_settings_with_bool(tmp_path), behavior=_IGNORE_WRITE) as h:
        response = await _put(h, BOOL_NAME, True, b"\x00")
        assert response.status_code == 502 and response.json()["code"] == "write_outcome_unknown"


async def test_float_write_is_confirmed_despite_float32_rounding(tmp_path) -> None:
    async with running_app(_settings_with_bool(tmp_path), behavior=_IGNORE_WRITE) as h:
        response = await _put(h, TARGET_NAME, 0.1, float_payload(0.1))
        assert response.status_code == 200, response.text
        wrong = await _put(h, TARGET_NAME, 0.1, float_payload(0.5))
        assert wrong.status_code == 502 and wrong.json()["code"] == "write_outcome_unknown"


async def test_integer_valued_float_for_an_enum_is_422_not_500(tmp_path) -> None:
    async with running_app(_settings_with_bool(tmp_path)) as h:
        response = await h.client.post(f"/api/v1/devices/main/actions/{ACTION_NAME}", json={"value": 1.0}, headers=AUTH)
        assert response.status_code == 422 and response.json()["code"] == "value_type_mismatch", response.text


@pytest.mark.parametrize("kind", [DataType.UINT8, DataType.INT32, DataType.ENUM])
def test_allowlist_rejects_a_float_for_integer_types(kind) -> None:
    entry = AllowlistEntry(name="x", data_type=kind, minimum=0, maximum=10)
    for value in (1.0, 1.5):
        with pytest.raises(WriteRejected) as info:
            Allowlist._check_value(entry, value)
        assert info.value.code == "value_type_mismatch"
    Allowlist._check_value(entry, 1)


@pytest.mark.parametrize("extra", [{"minimum": 0}, {"maximum": 1}, {"step": 1}, {"allowed_values": [0]}])
@pytest.mark.parametrize("kind", [DataType.BOOL, DataType.STRING])
def test_allowlist_rejects_restrictions_for_bool_and_string(kind, extra) -> None:
    with pytest.raises(ValidationError):
        AllowlistEntry(name="x", data_type=kind, **extra)
    AllowlistEntry(name="x", data_type=kind)


BOOTSTRAP_PASSWORD = "Xk7-pw29"


async def test_first_start_password_goes_to_stdout_and_is_also_saved_to_a_0600_file(
    tmp_path, capsys, monkeypatch
) -> None:
    """Deliberate product choice (reverses the former P2-3 restriction): the console banner must
    carry the password in clear text so it can be copy-pasted directly; the 0600 file remains as a
    fallback for runs without a visible console."""
    settings = make_settings(admin_db_path=tmp_path / "data" / "rct.db")
    app = SimpleNamespace(
        state=SimpleNamespace(first_start_password=BOOTSTRAP_PASSWORD, runtime=SimpleNamespace(settings=settings))
    )

    async def no_listener(self, sockets=None) -> None:
        return None

    monkeypatch.setattr(uvicorn.Server, "startup", no_listener)
    server = GracefulServer(uvicorn.Config(app), None)
    server.started = True
    await server.startup()

    path = settings.admin_db_path.parent / FIRST_START_PASSWORD_FILE
    printed = capsys.readouterr().out
    assert BOOTSTRAP_PASSWORD in printed and str(path.resolve()) in printed
    assert path.read_text(encoding="utf-8").strip() == BOOTSTRAP_PASSWORD
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_rewriting_the_password_file_cannot_leave_a_readable_mode(tmp_path) -> None:
    settings = make_settings(admin_db_path=tmp_path / "data" / "rct.db")
    first = write_first_start_password(settings, BOOTSTRAP_PASSWORD)
    first.chmod(0o644)  # a previous run or an operator widened it
    again = write_first_start_password(settings, BOOTSTRAP_PASSWORD)
    assert again == first and stat.S_IMODE(again.stat().st_mode) == 0o600


@pytest.mark.parametrize("version", [0, 2, 999])
def test_registry_version_other_than_one_aborts_the_start(tmp_path, version) -> None:
    shipped = json.loads(Path(make_settings().object_registry_path).read_text(encoding="utf-8"))
    shipped["version"] = version
    path = tmp_path / "objects_read.json"
    path.write_text(json.dumps(shipped), encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        ObjectRegistry.load(path)
    assert "version" in str(info.value.context["detail"])
