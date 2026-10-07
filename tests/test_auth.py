#!/usr/bin/env python3
#
# tests/test_auth_offloaded.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Token authentication does SQLite I/O and must not run on the event loop thread."""

import asyncio
import threading
from types import SimpleNamespace

from app.config import TokenRole
from app.security.dependencies import require_read
from app.security.tokens import Principal


class _SlowTokens:
    def __init__(self, blocked: threading.Event, release: threading.Event) -> None:
        self.thread: int | None = None
        self._blocked = blocked
        self._release = release

    def authenticate(self, authorization: str | None) -> Principal:
        self.thread = threading.get_ident()
        self._blocked.set()  # proves the worker thread is blocked, not the event loop
        self._release.wait(timeout=5)
        return Principal("tok", TokenRole.READ)


async def test_require_read_keeps_the_event_loop_responsive() -> None:
    """Deterministic version of the offloading check: the worker blocks on a threading.Event,
    the test proves the event loop still runs a coroutine while blocked, then releases the worker.
    No timing-based tick-count assumption and no un-awaited cancelled task."""
    blocked = threading.Event()
    release = threading.Event()
    tokens = _SlowTokens(blocked, release)
    limiter = SimpleNamespace(
        check_auth_blocked=lambda a: None, check_request=lambda c: None, record_auth_failure=lambda a: None
    )
    ctx = SimpleNamespace(tokens=tokens, limiter=limiter, client_ip=SimpleNamespace(resolve=lambda peer, headers: "192.0.2.1"))
    request = SimpleNamespace(client=SimpleNamespace(host="192.0.2.1"), headers={}, scope={})
    ticked = asyncio.Event()

    async def ticker() -> None:
        while True:
            await asyncio.sleep(0.01)
            ticked.set()

    task = asyncio.create_task(ticker())
    auth_task = asyncio.create_task(require_read(request, ctx))  # type: ignore[arg-type]
    await asyncio.to_thread(blocked.wait, 5)  # worker is now blocked in authenticate()
    assert blocked.is_set()
    await asyncio.wait_for(ticked.wait(), timeout=5)  # the loop still runs a coroutine meanwhile
    release.set()
    principal = await auth_task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert principal.token_id == "tok"
    assert tokens.thread != threading.get_ident()
