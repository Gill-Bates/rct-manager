#!/usr/bin/env python3
#
# tests/test_structure_smoke.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Structural smoke tests of the ownership and security boundaries (task 9.5)."""

import ast
import asyncio
import re
import socket
from pathlib import Path

from app.config import EndpointKey
from app.transport.endpoint import EndpointConfig, TransportEndpoint
from tests.conftest import AutoClock

APP = Path(__file__).resolve().parent.parent / "app"


def _modules() -> list[Path]:
    return sorted(APP.rglob("*.py"))


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found





def test_no_wholesale_buffer_drain() -> None:
    pattern = re.compile(r"\.read\(\s*-1\s*\)|\.read\(\s*\)|\._buffer\b|\.readall\(")
    modules = _modules()
    assert len(modules) >= 40
    offenders = [p.name for p in modules if pattern.search(p.read_text(encoding="utf-8"))]
    assert not offenders


def _uses_open_connection(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_as_name = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "asyncio"
        and any(alias.name == "open_connection" for alias in node.names)
        for node in ast.walk(tree)
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "open_connection":
            return True
        # Bare-name call, e.g. `from asyncio import open_connection` then `open_connection(...)`
        # (Finding 6): only counts as a use when the import above is actually present, so an
        # unrelated local function named `open_connection` is not flagged.
        if imported_as_name and isinstance(node, ast.Name) and node.id == "open_connection":
            return True
    return False


def test_open_connection_only_in_endpoint() -> None:
    users = [p.relative_to(APP).as_posix() for p in _modules() if _uses_open_connection(p)]
    assert users == ["transport/endpoint.py"]


def test_neutral_models_do_not_import_vendor_models() -> None:
    models = APP / "api" / "models.py"
    assert models.is_file()
    assert not any(name.startswith("app.api.models_vendor") for name in _imports(models))


def test_routers_do_not_import_the_rct_adapter() -> None:
    routers = APP / "api" / "routers"
    assert routers.is_dir()
    modules = list(routers.glob("*.py"))
    assert modules
    for path in modules:
        assert not any(name.startswith("app.gateway.rct") for name in _imports(path)), path.name


def test_socket_options_on_a_local_listener() -> None:
    async def scenario() -> tuple[int, int]:
        async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read()

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        endpoint = TransportEndpoint("endpoint-1", EndpointKey("127.0.0.1", port), EndpointConfig(), AutoClock())
        await endpoint.start()
        raw = endpoint._writer.get_extra_info("socket")
        options = (
            raw.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY),
            raw.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE),
        )
        await endpoint.close()
        server.close()
        await server.wait_closed()
        return options

    nodelay, keepalive = asyncio.run(scenario())
    assert nodelay and keepalive
