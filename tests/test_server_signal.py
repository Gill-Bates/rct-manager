#!/usr/bin/env python3
#
# tests/test_server_signal.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""A SIGTERM arriving before the event loop is known must not be lost (R6)."""

import signal
from types import SimpleNamespace

import uvicorn

from app.api.server import GracefulServer


def test_early_signal_is_remembered_and_starts_drain_in_serve(monkeypatch) -> None:
    server = GracefulServer(uvicorn.Config(lambda *a: None), SimpleNamespace(shutdown=None))
    server.handle_exit(signal.SIGTERM, None)
    assert server._early_signal and not server._drain_requested

    started: list[bool] = []
    monkeypatch.setattr(server, "_start_drain", lambda: started.append(True))

    async def fake_serve(self, sockets=None) -> None:
        return None

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    import asyncio

    asyncio.run(server.serve())
    assert started == [True] and server._drain_requested
