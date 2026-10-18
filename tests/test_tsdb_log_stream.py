#!/usr/bin/env python3
#
# tests/test_tsdb_log_stream.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The TSDB log tail: the Event Hub, the ``tsdb.log`` logging producer and the SSE endpoint.

Covers the hub buffering/fan-out invariants and the logger-name filter in isolation, then the admin
SSE endpoint (``GET /admin/api/events``) for auth, content type and an end-to-end record reaching the
stream as a ``tsdb.log`` event. The HTTP harness mirrors ``tests/test_admin_security.py``.
"""

import asyncio
import contextlib
import json
import logging
from typing import Self

import httpx
import pytest

from app.api.app_factory import create_app
from app.config import Settings
from app.observability.event_hub import BacklogPolicy, DropPolicy, EventHub
from app.observability.log_stream import (
    TSDB_LOG_EVENT,
    TSDB_LOG_POLICY,
    TsdbLogHandler,
    is_tsdb_logger,
    register_tsdb_log_type,
)

PASSWORD2 = "a much stronger password"


@pytest.fixture(autouse=True)
def _restore_root_log_level():
    """The log level and handler set are touched live on the root logger; keep them from leaking."""
    root = logging.getLogger()
    level = root.level
    handlers = root.handlers[:]
    yield
    root.setLevel(level)
    root.handlers[:] = handlers


def _settings(tmp_path, **extra):
    return Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db", **extra)


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    )


async def _login(client, password, csrf=None, **headers):
    if csrf is None:
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
    return await client.post(
        "/admin/api/login",
        headers={"X-CSRF-Token": csrf, **headers},
        json={"username": "admin", "password": password},
    )


async def _ready_client(app, password):
    """Return a client with a usable (password-changed) admin session cookie."""
    client = _client(app)
    login = await _login(client, password)
    changed = await client.post(
        "/admin/api/change-password",
        headers={"X-CSRF-Token": login.json()["csrf_token"]},
        json={"current_password": password, "new_password": PASSWORD2},
    )
    assert changed.status_code == 200
    accepted = await client.post("/admin/api/accept-disclaimer",
                                 headers={"X-CSRF-Token": changed.json()["csrf_token"]})
    assert accepted.status_code == 200
    return client


def test_is_tsdb_logger_matches_by_prefix():
    assert is_tsdb_logger("app.export")
    assert is_tsdb_logger("app.export.pusher")
    assert is_tsdb_logger("app.export.questdb_admin")
    assert is_tsdb_logger("app.api.app_factory")
    # Not a child, just a shared leading substring: must NOT match.
    assert not is_tsdb_logger("app.exporter")
    assert not is_tsdb_logger("app.api.app_factory_helper")
    assert not is_tsdb_logger("app.transport.receiver")
    assert not is_tsdb_logger("app.admin.api")


def _record(logger_name: str, message: str) -> logging.LogRecord:
    return logging.LogRecord(logger_name, logging.INFO, __file__, 0, message, None, None)


@pytest.mark.asyncio
async def test_filtered_record_reaches_the_hub_unrelated_does_not():
    hub = EventHub()
    register_tsdb_log_type(hub)
    hub.bind_loop(asyncio.get_running_loop())
    handler = TsdbLogHandler(hub)
    async with hub.subscribe(TSDB_LOG_EVENT) as (_backlog, queue):
        handler.emit(_record("app.export.pusher", "Exported 42 metrics"))
        handler.emit(_record("app.transport.receiver", "ignored"))
        await asyncio.sleep(0)  # let call_soon_threadsafe deliveries run
        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event.type == TSDB_LOG_EVENT
        assert event.data["message"] == "Exported 42 metrics"
        assert event.data["logger"] == "app.export.pusher"
        assert queue.empty()  # the unrelated record never entered the stream


@pytest.mark.asyncio
async def test_backlog_is_replayed_to_a_new_subscriber():
    hub = EventHub()
    register_tsdb_log_type(hub)
    hub.bind_loop(asyncio.get_running_loop())
    handler = TsdbLogHandler(hub)
    handler.emit(_record("app.export", "before subscribe"))  # logged before anyone subscribed
    async with hub.subscribe(TSDB_LOG_EVENT) as (backlog, _queue):
        assert [e.data["message"] for e in backlog] == ["before subscribe"]
        assert all(e.type == TSDB_LOG_EVENT for e in backlog)


@pytest.mark.asyncio
async def test_ids_are_monotonic():
    hub = EventHub()
    register_tsdb_log_type(hub)
    hub.bind_loop(asyncio.get_running_loop())
    handler = TsdbLogHandler(hub)
    handler.emit(_record("app.export", "one"))
    handler.emit(_record("app.export", "two"))
    async with hub.subscribe(TSDB_LOG_EVENT) as (backlog, _queue):
        ids = [e.id for e in backlog]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)


@pytest.mark.asyncio
async def test_full_subscriber_queue_drops_oldest_and_never_blocks_producer():
    # Tiny bounded queue so we can overflow it deterministically; drop policy is DROP_OLDEST.
    hub = EventHub(queue_size=2)
    hub.register_type(TSDB_LOG_EVENT, BacklogPolicy(backlog_size=10, drop=DropPolicy.DROP_OLDEST))
    hub.bind_loop(asyncio.get_running_loop())
    async with hub.subscribe(TSDB_LOG_EVENT) as (_backlog, queue):
        for i in range(5):  # five events into a queue of size two
            hub.publish(TSDB_LOG_EVENT, {"message": f"m{i}"})
        await asyncio.sleep(0)
        drained = []
        while not queue.empty():
            drained.append((await queue.get()).data["message"])
        # Non-blocking throughout; the two most recent survived, the oldest were dropped.
        assert drained == ["m3", "m4"]


@pytest.mark.asyncio
async def test_drop_newest_policy_keeps_the_queued_events():
    # The hub also supports DROP_NEWEST (a future type may prefer it); verify the alternative branch.
    hub = EventHub(queue_size=2)
    hub.register_type("x.evt", BacklogPolicy(backlog_size=0, drop=DropPolicy.DROP_NEWEST))
    hub.bind_loop(asyncio.get_running_loop())
    async with hub.subscribe() as (_backlog, queue):
        for i in range(5):
            hub.publish("x.evt", {"message": f"m{i}"})
        await asyncio.sleep(0)
        drained = []
        while not queue.empty():
            drained.append((await queue.get()).data["message"])
        assert drained == ["m0", "m1"]  # the first two held; later ones dropped


@pytest.mark.asyncio
async def test_ring_buffer_respects_maxlen():
    hub = EventHub()
    hub.register_type(TSDB_LOG_EVENT, BacklogPolicy(backlog_size=3, drop=DropPolicy.DROP_OLDEST))
    hub.bind_loop(asyncio.get_running_loop())
    for i in range(10):
        hub.publish(TSDB_LOG_EVENT, {"message": f"m{i}"})
    backlog = hub.backlog(TSDB_LOG_EVENT)
    assert [e.data["message"] for e in backlog] == ["m7", "m8", "m9"]


def test_tsdb_log_policy_drops_oldest():
    # The documented drop policy for tsdb.log (a tail: lose the oldest, keep the newest activity).
    assert TSDB_LOG_POLICY.drop is DropPolicy.DROP_OLDEST
    assert TSDB_LOG_POLICY.backlog_size > 0


@pytest.mark.asyncio
async def test_unsubscribe_leaves_no_subscriber_and_others_keep_working():
    hub = EventHub()
    register_tsdb_log_type(hub)
    hub.bind_loop(asyncio.get_running_loop())
    async with hub.subscribe(TSDB_LOG_EVENT) as (_b, _q):
        assert hub.subscriber_count == 1
        async with hub.subscribe(TSDB_LOG_EVENT) as (_b2, queue2):
            assert hub.subscriber_count == 2
        # The inner subscriber's context exited: its queue is gone, the outer one remains.
        assert hub.subscriber_count == 1
        hub.publish(TSDB_LOG_EVENT, {"message": "still delivered"})
        await asyncio.sleep(0)
        assert queue2.empty()  # the detached subscriber receives nothing
    assert hub.subscriber_count == 0


@pytest.mark.asyncio
async def test_events_endpoint_requires_auth(tmp_path):
    app = create_app(_settings(tmp_path))
    async with _client(app) as client:
        resp = await client.get("/admin/api/events")
        assert resp.status_code == 401


class _AsgiStream:
    """Drives the ASGI app for GET /admin/api/events directly over the protocol.

    httpx's ASGITransport does not surface an endless SSE response usefully in this test setup (it
    waits on the response iterator and only aborts via the outer timeout), so the ASGI protocol is
    driven by hand. A receive queue delivers an ``http.request`` and then, when the context exits, a
    real ``http.disconnect`` — the same signal a browser closing its EventSource produces — so the
    client-disconnect cleanup path is actually exercised, not a hard task cancel. ``send`` records
    the response start and accumulates body chunks into whole SSE event frames.
    """

    def __init__(self, app, cookies: dict):
        self._app = app
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        self._scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/admin/api/events",
            "raw_path": b"/admin/api/events",
            "query_string": b"",
            "headers": [(b"origin", b"http://testserver"), (b"cookie", cookie_header.encode())],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "app": app,
        }
        self._incoming: asyncio.Queue[dict] = asyncio.Queue()
        self._incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._started = asyncio.Event()
        self._buffer = ""
        self._frames: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def _receive(self) -> dict:
        return await self._incoming.get()

    async def _send(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode().lower(): v.decode() for k, v in message["headers"]}
            self._started.set()
        elif message["type"] == "http.response.body":
            self._buffer += message.get("body", b"").decode()
            while "event:" in self._buffer and "\n\n" in self._buffer[self._buffer.index("event:") :]:
                start = self._buffer.index("event:")
                end = self._buffer.index("\n\n", start)
                self._frames.put_nowait(self._buffer[start:end])
                self._buffer = self._buffer[end + 2 :]

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        await asyncio.wait_for(self._started.wait(), timeout=5)
        return self

    async def next_frame(self, timeout: float = 5.0) -> str:
        """The next complete ``event:`` frame; ``: ping`` heartbeats are not frames and are skipped."""
        return await asyncio.wait_for(self._frames.get(), timeout=timeout)

    async def __aexit__(self, *exc) -> None:
        # Deliver a real client disconnect, then let the app task wind down on its own.
        self._incoming.put_nowait({"type": "http.disconnect"})
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task


async def _cookies_for(app, password) -> dict:
    client = await _ready_client(app, password)
    cookies = {name: client.cookies[name] for name in client.cookies}
    await client.aclose()
    return cookies


@pytest.mark.asyncio
async def test_events_endpoint_is_event_stream_for_an_admin(tmp_path):
    app = create_app(_settings(tmp_path))
    cookies = await _cookies_for(app, app.state.first_start_password)
    async with _AsgiStream(app, cookies) as stream:
        assert stream.status == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        # RequestContextMiddleware forces no-store on every response (stronger than no-cache, fine here).
        assert stream.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_events_endpoint_carries_a_filtered_tsdb_log_line(tmp_path):
    """End-to-end: the real lifespan attaches the logging handler, a filtered record reaches the wire."""
    app = create_app(_settings(tmp_path))
    password = app.state.first_start_password
    async with app.router.lifespan_context(app):
        # The app does not call setup_logging in tests, so the root level is the default WARNING; the
        # production default is INFO (LOG_LEVEL). Set it so the INFO records below reach the handler.
        logging.getLogger().setLevel(logging.INFO)
        cookies = await _cookies_for(app, password)
        async with _AsgiStream(app, cookies) as stream:
            assert stream.status == 200
            assert stream.headers["content-type"].startswith("text/event-stream")
            # Emitted after the stream is open; the filtered record must surface as a tsdb.log event,
            # the unrelated one must not. The ring buffer may replay earlier app.api.app_factory
            # startup lines first, so read frames until the marker (and assert the noise never shows).
            # The handler lets the root level gate records, so put INFO in scope as the app's default
            # LOG_LEVEL=INFO would (the autouse fixture restores the level afterwards).
            logging.getLogger().setLevel(logging.INFO)
            logging.getLogger("app.transport.receiver").info("unrelated noise")
            logging.getLogger("app.export.pusher").info("runtime-evidence %d", 42)
            seen = []
            frame = None
            for _ in range(50):
                frame = await stream.next_frame()
                seen.append(frame)
                if "runtime-evidence 42" in frame:
                    break
            else:
                raise AssertionError("the filtered tsdb.log line never reached the stream")

    assert "event: tsdb.log" in frame
    data_line = next(line for line in frame.splitlines() if line.startswith("data:"))
    payload = json.loads(data_line[len("data:") :].strip())
    assert payload["message"] == "runtime-evidence 42"
    assert payload["logger"] == "app.export.pusher"
    assert payload["level"] == "INFO"
    assert all("unrelated noise" not in f for f in seen)  # the unrelated logger is filtered out


@pytest.mark.asyncio
async def test_client_disconnect_leaves_no_subscriber(tmp_path):
    """A real http.disconnect must drop the hub subscriber (no leaked queue/task)."""
    app = create_app(_settings(tmp_path))
    cookies = await _cookies_for(app, app.state.first_start_password)
    hub = app.state.event_hub
    assert hub.subscriber_count == 0
    async with _AsgiStream(app, cookies) as stream:
        assert stream.status == 200
        await asyncio.sleep(0.05)  # let the generator register its subscription on the loop
        assert hub.subscriber_count == 1
    # The context sent http.disconnect and awaited the task: the subscriber must be gone.
    assert hub.subscriber_count == 0
