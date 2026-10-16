#!/usr/bin/env python3
#
# tests/test_rct_battery_probe.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The hardware diagnosis tool stays read-only by default; the periodic test is opt-in and confirmed."""

import asyncio
import importlib.util
import socket
from pathlib import Path

import pytest

from app.protocol.frames import encode_frame
from app.protocol.stream import StreamParser
from app.protocol.types import Command
from tests.e2e.fake_inverter import _by_object_id, _default_payload
from tests.fakes import response_to

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "diagnostics" / "rct_battery_probe.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("rct_battery_probe", _TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _serve_recording(port: int, seen: list[Command]) -> asyncio.Server:
    values, entries = _by_object_id()
    ids = {entry.name: oid for oid, entry in entries.items()}
    silent = {ids["battery_soc_target"], ids["battery_module_sn_6"]}  # unanswered objects

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        parser = StreamParser()
        while data := await reader.read(4096):
            for frame in parser.feed(data):
                seen.append(frame.command)
                if frame.command is not Command.READ or frame.object_id in silent:
                    continue
                payload = values.get(frame.object_id) or _default_payload(entries.get(frame.object_id))
                writer.write(encode_frame(response_to(frame, payload)))
                await writer.drain()

    return await asyncio.start_server(handle, "127.0.0.1", port)


def _argv(port: int, *extra: str) -> list[str]:
    return [
        "--device", f"fake=127.0.0.1:{port}", "--confirm-app-stopped",
        "--offsets", "0.1,0.3", "--response-timeout", "0.5", "--skip-topology", *extra,
    ]  # fmt: skip


async def test_default_run_sends_only_read_frames() -> None:
    tool = _load_tool()
    port, seen = _free_port(), []
    server = await _serve_recording(port, seen)
    async with server:
        result = await tool.run(tool.parse_args(_argv(port)))
    assert seen
    assert set(seen) == {Command.READ}
    assert result["device"]["mode"] == "read-only"
    assert result["classification_hint"]["hint"] in {"read_unstable", "read_unstable_early", "read_unstable_mixed"}
    assert result["periodic_status"]["registration_probes"] == []


def test_invasive_mode_requires_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    tool = _load_tool()
    with pytest.raises(SystemExit) as exc:
        tool.parse_args(_argv(8899, "--invasive-periodic-test"))
    assert exc.value.code == 2
    assert tool.CONFIRM_INVASIVE in capsys.readouterr().err
    with pytest.raises(SystemExit):
        tool.parse_args(_argv(8899, "--invasive-periodic-first", tool.CONFIRM_INVASIVE))
    assert tool.parse_args(_argv(8899, "--invasive-periodic-test", tool.CONFIRM_INVASIVE)).invasive_periodic_test


async def test_command_guard_blocks_periodic_outside_invasive_mode() -> None:
    tool = _load_tool()

    class _Endpoint:
        async def execute(self, request, **kwargs):
            return "sent"

    endpoint = _Endpoint()
    tool.install_command_guard(endpoint, tool.READ_COMMANDS)
    request = type("R", (), {"frame": type("F", (), {"command": Command.READ_PERIODICALLY})()})()
    with pytest.raises(RuntimeError, match="command guard"):
        await endpoint.execute(request)
