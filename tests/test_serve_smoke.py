#!/usr/bin/env python3
#
# tests/test_serve_smoke.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Process smoke test: SIGTERM drains in-flight work, answers 503 meanwhile and exits with 0 (task 10.4)."""

import asyncio
import contextlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.admin.store import AdminStore
from app.catalog.registry import RegistryCatalog
from app.protocol.frames import encode_frame
from app.protocol.stream import StreamParser
from app.protocol.types import Command
from tests.api_helpers import REGISTRY_FIXTURE, make_settings
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
