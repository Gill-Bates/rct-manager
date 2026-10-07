#!/usr/bin/env python3
#
# tests/test_server.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Server startup and shutdown: process smoke test, signal handling, bind warning, access-log level, docs log line, startup gate and first-start password handover."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

from app.admin.store import AdminStore
from app.api import server
from app.api.app_factory import create_app
from app.api.middleware import access_log_level
from app.api.server import (
    FIRST_START_PASSWORD_FILE,
    GracefulServer,
    write_first_start_password,
)
from app.catalog.registry import RegistryCatalog
from app.gateway.base import DeviceState
from app.protocol.frames import encode_frame
from app.protocol.stream import StreamParser
from app.protocol.types import Command
from tests.api_helpers import (
    REGISTRY_FIXTURE,
    make_settings,
    running_app,
    wait_settled,
)
from tests.conftest import settings_env_names

ROOT = Path(__file__).resolve().parent.parent


SLOW_DELAY = 1.5


def _free_http_port() -> socket.socket:
    """Reserve a listening socket for the HTTP server to hand to the child (Finding 1).

    uvicorn (through ``asyncio.loop.create_server``) accepts a list of pre-bound sockets, so the
    port the child binds to is exactly the one reserved here: no separate "probe a free port,
    close it, hope nothing else grabs it before the child rebinds" step, which is the race this
    replaces. The socket is passed to the child via its file descriptor (``SO_REUSEADDR`` plus
    ``set_inheritable`` on fork/exec), see ``_BOOT`` and ``RCT_API_BOUND_FD`` in app/__main__.py.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def _fake_device(result: dict, slow_object: int, ready: threading.Event, slow_hit: threading.Event) -> None:
    from app.protocol.frames import Frame

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        parser = StreamParser()
        while data := await reader.read(4096):
            for frame in parser.feed(data):
                if frame.object_id == slow_object:
                    slow_hit.set()  # the slow request actually reached the device (Finding 2)
                    await asyncio.sleep(SLOW_DELAY)
                payload = struct.pack(">f", 1.5) if frame.object_id != 0 else b""
                writer.write(encode_frame(Frame(Command.RESPONSE, frame.object_id, payload, frame.plant_address)))
                await writer.drain()

    async def main() -> None:
        # Bind on port 0 directly and read back the actually-assigned port: no separate
        # free-port probe that could race another process for the same port (Finding 1).
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        result["port"] = server.sockets[0].getsockname()[1]
        result["server"] = server
        result["loop"] = asyncio.get_running_loop()
        ready.set()
        with contextlib.suppress(asyncio.CancelledError):
            async with server:
                await server.serve_forever()

    asyncio.run(main())


def _get(url: str, token: str | None = None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def _wait_for_status(base: str, token: str | None, expected: int, deadline: float) -> dict:
    """Poll ``/health`` until it reports ``expected`` or the deadline passes (Finding 2)."""
    last: tuple[int, dict] | None = None
    while time.monotonic() < deadline:
        try:
            last = _get(f"{base}/health", token)
            if last[0] == expected:
                return last[1]
        except OSError:
            pass
        time.sleep(0.02)
    raise AssertionError(f"/health never reached status {expected}, last seen: {last}")


# OBJECT_REGISTRY_PATH is not operator-settable, so the child swaps in the fixture after loading the settings.
# RCT_API_BOUND_FD, when set, makes the server bind on a socket inherited from the parent instead
# of binding BIND_PORT itself (see app/__main__.py); the harness below relies on this to avoid a
# bind-probe-then-rebind race for the HTTP port (Finding 1).
_BOOT = (
    "import sys, pathlib, app.__main__ as m; path = pathlib.Path(sys.argv.pop(1)); load = m.load_settings; "
    "m.load_settings = lambda f: load(f).model_copy(update={'object_registry_path': path, "
    "'write_allowlist_path': path.with_name('objects_write_allowed.json')}); "
    "sys.exit(m.main(sys.argv[1:]))"
)


def test_sigterm_drains_answers_503_and_exits_cleanly(tmp_path) -> None:
    slow = RegistryCatalog.from_file(make_settings().object_registry_path).object_entry("battery_soc").object_id
    device_ready = threading.Event()
    slow_hit = threading.Event()
    device_result: dict = {}
    device_thread = threading.Thread(
        target=_fake_device, args=(device_result, slow, device_ready, slow_hit), daemon=True
    )
    device_thread.start()
    store: AdminStore | None = None
    proc: subprocess.Popen | None = None
    http_sock = _free_http_port()
    try:
        assert device_ready.wait(5)
        device_port = device_result["port"]
        http_port = http_sock.getsockname()[1]
        env_file = tmp_path / "settings.env"
        secret = "k" * 48
        env_file.write_text(
            f"BIND_PORT={http_port}\nADMIN_DB_PATH={tmp_path / 'rct.db'}\nHMAC_SECRET={secret}\n", encoding="utf-8"
        )
        env_file.chmod(0o600)
        # Devices and tokens are GUI-only, so seed the admin database the way the GUI would.
        store = AdminStore(tmp_path / "rct.db", secret)
        password = store.initialize()
        assert store.change_password(password, "a much stronger password")
        token = store.create_token("smoke", "read", None)[1]
        store.put(
            "operator_settings", {"devices": [{"device_id": "main", "host": "127.0.0.1", "port": device_port}]}
        )
        store.close()
        store = None  # the child owns the database file from here on
        proc = subprocess.Popen(
            [sys.executable, "-c", _BOOT, str(REGISTRY_FIXTURE), "serve", "--env-file", str(env_file)],
            cwd=ROOT,
            env={
                **{k: v for k, v in os.environ.items() if k not in set(settings_env_names())},
                "RCT_API_BOUND_FD": str(http_sock.fileno()),
            },
            pass_fds=(http_sock.fileno(),),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{http_port}"
        for _ in range(100):
            try:
                if _get(f"{base}/health", token)[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("server did not start")
        in_flight: list[int] = []
        worker = threading.Thread(
            target=lambda: in_flight.append(_get(f"{base}/api/v1/devices/main/metrics/battery_soc?fresh=true", token)[0])
        )
        worker.start()
        # Wait for the slow frame to actually reach the fake device before sending SIGTERM, instead
        # of guessing with a fixed sleep (Finding 2).
        assert slow_hit.wait(5), "the slow request never reached the fake device"
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        body = _wait_for_status(base, token, 503, time.monotonic() + 5)
        assert body["code"] == "not_ready"
        assert _get(f"{base}/api/v1/devices/main/metrics/grid_power", token)[0] == 503
        worker.join(10)
        assert in_flight == [200]  # the running transaction was finished, not cut off
        assert proc.wait(15) == 0
        assert time.monotonic() - started < 20  # SHUTDOWN_GRACE_SECONDS default
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        if store is not None:
            store.close()
        http_sock.close()
        server = device_result.get("server")
        loop = device_result.get("loop")
        if server is not None and loop is not None:
            loop.call_soon_threadsafe(server.close)
        device_thread.join(5)


@pytest.mark.parametrize(
    ("method", "path", "status", "level"),
    [
        ("GET", "/admin/static/css/admin.css", 200, logging.DEBUG),
        ("GET", "/login", 200, logging.DEBUG),
        ("GET", "/", 303, logging.DEBUG),
        ("GET", "/ui/dashboard", 200, logging.DEBUG),
        ("GET", "/admin/api/session", 200, logging.DEBUG),
        ("GET", "/admin/api/devices", 200, logging.DEBUG),
        ("POST", "/admin/api/login", 200, logging.INFO),
        ("POST", "/admin/api/logout", 200, logging.INFO),
        ("POST", "/admin/api/change-password", 200, logging.INFO),
        ("PUT", "/admin/api/settings", 200, logging.INFO),
        ("DELETE", "/admin/api/tokens/abc", 200, logging.INFO),
        ("GET", "/api/v1/devices", 200, logging.INFO),
        ("GET", "/metrics", 200, logging.DEBUG),
        ("GET", "/metrics", 401, logging.WARNING),
        ("GET", "/health", 200, logging.INFO),
        ("POST", "/admin/api/login", 401, logging.WARNING),
        ("GET", "/admin/static/missing.js", 404, logging.WARNING),
        ("GET", "/api/v1/devices", 500, logging.ERROR),
    ],
)
def test_access_log_level(method, path, status, level):
    assert access_log_level(method, path, status) == level


def _settings(address: str) -> SimpleNamespace:
    return SimpleNamespace(bind_address=address, bind_port=8000)


def test_warns_on_loopback_in_container(monkeypatch, caplog) -> None:
    # IPv4 and IPv6 loopback hit the same is_loopback branch; one address covers it.
    monkeypatch.setattr(server, "_in_container", lambda: True)
    with caplog.at_level(logging.WARNING, logger=server.log.name):
        server.warn_if_loopback_in_container(_settings("127.0.0.1"))
    assert any("loopback" in r.getMessage() and "0.0.0.0" in r.getMessage() for r in caplog.records)
    assert any("behind reverse proxy" in r.getMessage() for r in caplog.records)


def test_silent_for_wildcard_bind_in_container(monkeypatch, caplog) -> None:
    monkeypatch.setattr(server, "_in_container", lambda: True)
    with caplog.at_level(logging.WARNING, logger=server.log.name):
        server.warn_if_loopback_in_container(_settings("0.0.0.0"))
    assert not caplog.records


def test_silent_for_loopback_outside_container(monkeypatch, caplog) -> None:
    monkeypatch.setattr(server, "_in_container", lambda: False)
    with caplog.at_level(logging.WARNING, logger=server.log.name):
        server.warn_if_loopback_in_container(_settings("127.0.0.1"))
    assert not caplog.records


def test_marker_variable_marks_container(monkeypatch) -> None:
    monkeypatch.setenv("RCT_API_CONTAINER", "1")
    assert server._in_container()


def _docs_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("API documentation")]


@pytest.mark.parametrize(
    ("address", "url"),
    [("127.0.0.1", "http://127.0.0.1:8123/docs"), ("::1", "http://[::1]:8123/docs")],
)
def test_enabled_docs_log_their_url(caplog: pytest.LogCaptureFixture, address: str, url: str) -> None:
    with caplog.at_level(logging.INFO, logger="app.api.app_factory"):
        create_app(make_settings(docs_public=True, bind_address=address, bind_port=8123))
    assert _docs_lines(caplog) == [f"API documentation: {url} (OpenAPI: /openapi.json)"]


def test_disabled_docs_say_so(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.api.app_factory"):
        create_app(make_settings(docs_public=False))
    assert _docs_lines(caplog) == ["API documentation disabled (DOCS_PUBLIC=false)"]


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


NEW_PASSWORD = "a much stronger password"


def _gate_settings(tmp_path):
    return make_settings(hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")


def _states(app):
    runtime = app.state.runtime
    return {runtime.gateway.device_status(d).state for d in runtime.devices}


@pytest.mark.asyncio
async def test_jobs_wait_for_password_change_then_start(tmp_path, caplog):
    settings = _gate_settings(tmp_path)
    caplog.set_level(logging.WARNING)
    async with running_app(settings, settle=False, authorize=False) as harness:
        password = harness.app.state.first_start_password
        # The lifespan gate (app_factory._await_password_change) decides whether to start the
        # heartbeat/periodic/refresh tasks synchronously, before the context manager above ever
        # yields: the warning log line and the STARTING state are both already settled by this
        # point, not something a fixed sleep could prove any more reliably (minor note).
        assert _states(harness.app) == {DeviceState.STARTING}  # no heartbeat while the bootstrap password stands
        assert "Initial admin password not changed yet" in caplog.text
        assert (await harness.client.get("/login")).status_code == 200  # the admin UI stays reachable
        assert harness.app.state.admin_store.change_password(password, NEW_PASSWORD)
        await wait_settled(harness.app, timeout=8)
        assert DeviceState.STARTING not in _states(harness.app)


@pytest.mark.asyncio
async def test_later_start_with_changed_password_runs_jobs_immediately(tmp_path, caplog):
    settings = _gate_settings(tmp_path)
    async with running_app(settings, settle=False, authorize=False) as first:
        password = first.app.state.first_start_password
        assert first.app.state.admin_store.change_password(password, NEW_PASSWORD)
    caplog.set_level(logging.WARNING)
    caplog.clear()
    async with running_app(settings, authorize=False) as second:  # settle=True: heartbeat ran at once
        assert DeviceState.STARTING not in _states(second.app)
    assert "Initial admin password not changed yet" not in caplog.text


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
