#!/usr/bin/env python3
#
# tests/test_write_path.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Write path: no values in logs, atomic write plus readback, actions without a WRITE answer."""

import asyncio
import json
import logging
from pathlib import Path

from app.protocol.types import Command
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

WRITER = {"Authorization": f"Bearer {WRITE_TOKEN}"}
SECRET = "S3cr3t-Pa55w0rd-synthetic"
SECRET_NAME = "secret_text"


def _settings(tmp_path: Path, **extra):
    return make_settings(
        enable_write_support=True,
        write_response_timeout_ms=100,
        **write_fixtures(tmp_path),
        **extra,
    )


def _with_string_entry(tmp_path: Path):
    paths = write_fixtures(tmp_path)
    registry = json.loads(paths["object_registry_path"].read_text(encoding="utf-8"))
    registry["entries"].append(
        {
            "name": SECRET_NAME,
            "object_id": "0x1234ABCE",
            "data_type": "t_string",
            "unit": "",
            "value_type": "string",
            "writable": True,
            "idempotent_write": False,
            "is_action": False,
            "preselected": False,
            "description": "synthetic secret",
        }
    )
    paths["object_registry_path"].write_text(json.dumps(registry), encoding="utf-8")
    allowlist = json.loads(paths["write_allowlist_path"].read_text(encoding="utf-8"))
    allowlist["entries"].append({"name": SECRET_NAME, "data_type": "t_string"})
    paths["write_allowlist_path"].write_text(json.dumps(allowlist), encoding="utf-8")
    return make_settings(enable_write_support=True, write_response_timeout_ms=100, **paths)


def _leaks(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if SECRET in r.getMessage() or SECRET in str(r.args)]


async def test_secret_string_write_never_reaches_the_log(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    async with running_app(_with_string_entry(tmp_path)) as h:
        url = f"/api/v1/devices/main/metrics/{SECRET_NAME}"
        ok = await h.client.put(url, json={"value": SECRET}, headers=WRITER)
        h.net.freeze_writes = True
        h.net.payloads[0x1234ABCE] = b"\x00\x00\x00\x00"
        failed = await h.client.put(url, json={"value": SECRET + "x"}, headers=WRITER)
        rejected = await h.client.put(url, json={"value": SECRET + "\x00"}, headers=WRITER)
        wrong_type = await h.client.put(
            f"/api/v1/devices/main/metrics/{TARGET_NAME}", json={"value": SECRET}, headers=WRITER
        )
    assert ok.status_code == 200, ok.text
    assert failed.status_code == 502 and rejected.status_code == 422 and wrong_type.status_code == 422
    assert _leaks(caplog) == []


async def test_concurrent_writes_to_one_object_are_each_confirmed(tmp_path) -> None:
    async with running_app(_settings(tmp_path)) as h:
        url = f"/api/v1/devices/main/metrics/{TARGET_NAME}"
        responses = await asyncio.gather(
            h.client.put(url, json={"value": 0.2}, headers=WRITER),
            h.client.put(url, json={"value": 0.8}, headers=WRITER),
        )
        order = [(f.command, f.payload) for _, f in h.net.frames if f.object_id == TARGET_OBJECT_ID]
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert [round(r.json()["readback_value"], 3) for r in responses] == [0.2, 0.8]
    assert [c for c, _ in order] == [Command.WRITE, Command.READ, Command.WRITE, Command.READ]


async def test_action_without_write_answer_and_readback_ok_is_200(tmp_path) -> None:
    async with running_app(_settings(tmp_path)) as h:
        response = await h.client.post(f"/api/v1/devices/main/actions/{ACTION_NAME}", json={"value": 1}, headers=WRITER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action_confirmed"] is False and body["requested_value"] == 1 and body["readback_value"] is not None


async def test_action_without_readback_is_502_outcome_unknown(tmp_path) -> None:
    def behavior(frame):
        return "ignore" if frame.command is Command.READ and frame.object_id != TARGET_OBJECT_ID else "respond"

    async with running_app(_settings(tmp_path), behavior=behavior) as h:
        h.net.payloads[TARGET_OBJECT_ID] = float_payload(0.5)
        response = await h.client.post(f"/api/v1/devices/main/actions/{ACTION_NAME}", json={"value": 1}, headers=WRITER)
    assert response.status_code == 502 and response.json()["code"] == "action_outcome_unknown", response.text
