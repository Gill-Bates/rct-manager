#!/usr/bin/env python3
#
# tests/test_restart_integration.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The background restart end to end: the real entry point as a child process, no pre-bound socket.

Only a started ``run.py serve`` proves the pieces that unit tests replace: the graceful shutdown,
``os.execv`` with the original command line, the settings read back from the admin database, signal
handling after the exec and a new listener on a fresh port.
"""

import contextlib
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from app.admin.store import AdminStore
from tests.conftest import settings_env_names

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "a much stronger password"
SECRET = "k" * 48
WAIT_SECONDS = 25


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _answers(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _wait_until(condition, what: str, seconds: float = WAIT_SECONDS) -> float:
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if condition():
            return time.monotonic() - started
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def _stop(proc: subprocess.Popen) -> None:
    """Terminate the process group, so no child outlives a failed test."""
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def _cmdline(pid: int) -> bytes:
    return Path(f"/proc/{pid}/cmdline").read_bytes()


class _Service:
    def __init__(self, tmp_path: Path, port: int, device_port: int) -> None:
        self.port = port
        self.log_path = tmp_path / "server.log"
        env_file = tmp_path / "settings.env"
        env_file.write_text(
            f"BIND_PORT={port}\nADMIN_DB_PATH={tmp_path / 'rct.db'}\nHMAC_SECRET={SECRET}\n", encoding="utf-8"
        )
        env_file.chmod(0o600)
        store = AdminStore(tmp_path / "rct.db", SECRET)
        self.bootstrap_password = store.initialize()
        store.put("operator_settings", {"devices": [{"device_id": "sim", "host": "127.0.0.1", "port": device_port}]})
        store.close()
        drop = set(settings_env_names()) | {"RCT_API_CONTAINER", "RCT_API_RESTARTED_AT", "RCT_API_BOUND_FD"}
        env = {**{k: v for k, v in os.environ.items() if k not in drop}, "PYTHONUNBUFFERED": "1"}
        self._log = self.log_path.open("wb")
        self.proc = subprocess.Popen(
            [sys.executable, "run.py", "serve", "--env-file", str(env_file)],
            cwd=ROOT, env=env, stdout=self._log, stderr=subprocess.STDOUT, start_new_session=True,
        )

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def close(self) -> None:
        _stop(self.proc)
        self._log.close()


@pytest.fixture
def service(tmp_path):
    device_port = _free_port()
    device = subprocess.Popen(
        [sys.executable, "-m", "tests.e2e.fake_inverter", str(device_port)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    running = _Service(tmp_path, _free_port(), device_port)
    try:
        _wait_until(lambda: _answers(running.port) or running.proc.poll() is not None, "the service to start")
        assert running.proc.poll() is None, running.log()
        yield running
    finally:
        running.close()
        _stop(device)


def _login(service: _Service) -> tuple[httpx.Client, dict[str, str]]:
    # Mutations fail closed without a same-origin Origin header, as a browser sends it.
    base = f"http://127.0.0.1:{service.port}"
    client = httpx.Client(base_url=base, headers={"Origin": base}, timeout=10)
    csrf = client.get("/admin/api/session").json()["csrf_token"]
    login = client.post(
        "/admin/api/login", headers={"X-CSRF-Token": csrf},
        json={"username": "admin", "password": service.bootstrap_password},
    )
    assert login.status_code == 200 and login.json()["must_change_password"] is True, login.text
    changed = client.post(
        "/admin/api/change-password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
        json={"current_password": service.bootstrap_password, "new_password": PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    return client, {"X-CSRF-Token": changed.json()["csrf_token"]}


def test_listener_change_replaces_the_process_image_and_serves_on_the_new_port(service):
    client, headers = _login(service)
    pid, cmdline = service.proc.pid, _cmdline(service.proc.pid)
    first, second = _free_port(), _free_port()

    # A change to the listener answers at once and applies in the background.
    saved = client.put("/admin/api/settings", headers=headers, json={"bind_port": first})
    assert saved.status_code == 200 and saved.json()["applying"] is True
    assert saved.json()["settings"]["bind_port"] == first
    _wait_until(lambda: _answers(first), "the new port to answer")
    assert not _answers(service.port), "the old listener must be gone"
    assert service.proc.poll() is None
    assert _cmdline(pid) == cmdline  # execv keeps the PID and the exact command line

    # The session cookie is not bound to a port: the same login works on the new origin.
    client.base_url = f"http://127.0.0.1:{first}"
    client.headers["Origin"] = f"http://127.0.0.1:{first}"
    session = client.get("/admin/api/settings")
    assert session.status_code == 200 and session.json()["settings"]["bind_port"] == first

    log = service.log()
    assert len(re.findall(rf"starting \(.*\), pid={pid}\b", log)) == 2, log  # the same PID booted twice
    assert log.index("Restarting in the background") < log.index("Shutdown finished") < log.rindex("starting (")
    assert "Traceback" not in log and "Re-exec failed" not in log, log

    # A listener that cannot be bound is refused and nothing is scheduled.
    scheduled = log.count("Background restart scheduled")
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        refused = client.put("/admin/api/settings", headers=headers, json={"bind_port": taken.getsockname()[1]})
    assert refused.status_code == 409 and "previous listener stays active" in refused.json()["detail"]
    time.sleep(2.5)  # longer than the debounce
    assert _answers(first) and service.log().count("Background restart scheduled") == scheduled

    # A second change right after a restart waits out the minimum distance instead of looping.
    again = client.put("/admin/api/settings", headers=headers, json={"bind_port": second})
    assert again.status_code == 200 and again.json()["applying"] is True
    time.sleep(3)
    assert service.proc.poll() is None and _answers(first) and not _answers(second)
    delay = float(re.findall(r"Background restart scheduled in ([\d.]+) s", service.log())[-1])
    assert 15 < delay <= 30

    # Signals still work after the exec: SIGTERM drains and exits cleanly, without another re-exec.
    service.proc.send_signal(signal.SIGTERM)
    assert service.proc.wait(20) == 0
    assert service.log().count("Shutdown finished") == 2
    assert not _answers(first)
    client.close()


def test_quick_changes_collapse_into_one_restart_and_a_revert_cancels_it(service):
    client, headers = _login(service)
    original = service.port

    # Setting the port back before the debounce ends leaves the listener alone.
    assert client.put("/admin/api/settings", headers=headers, json={"bind_port": _free_port()}).json()["applying"]
    assert client.put("/admin/api/settings", headers=headers, json={"bind_port": original}).json()["applying"] is False
    time.sleep(3)
    assert _answers(original) and "Restarting in the background" not in service.log()

    # Two quick changes end in one restart onto the last value; the stored value, not the
    # BIND_PORT of settings.env, decides the listener of the new process.
    skipped, last = _free_port(), _free_port()
    client.put("/admin/api/settings", headers=headers, json={"bind_port": skipped})
    assert client.put("/admin/api/settings", headers=headers, json={"bind_port": last}).json()["applying"]
    _wait_until(lambda: _answers(last), "the last port to answer")
    assert not _answers(skipped) and not _answers(original)
    assert service.log().count("Restarting in the background") == 1
    client.close()
