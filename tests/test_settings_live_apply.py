#!/usr/bin/env python3
#
# tests/test_settings_live_apply.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Every saved setting takes effect on its own: live in place, or by a debounced background re-exec."""

import asyncio
import contextlib
import logging
import socket
from types import SimpleNamespace

import pytest
import uvicorn

from app.admin.store import AdminStore
from app.api import server
from app.api.restart import BackgroundRestart, probe_listener
from app.errors import ReconfigurationBuildError
from tests.api_helpers import admin_session_headers, make_settings, running_app

PASSWORD = "a much stronger password"


@pytest.fixture(autouse=True)
def _restore_root_log_level():
    level = logging.getLogger().level
    yield
    logging.getLogger().setLevel(level)


@contextlib.asynccontextmanager
async def _admin(tmp_path, **overrides):
    settings = make_settings(hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db", **overrides)
    store = AdminStore(settings.admin_db_path, "s" * 48)
    try:
        assert store.change_password(store.initialize(), PASSWORD)  # jobs start without the password gate
    finally:
        store.close()
    async with running_app(settings, authorize=False) as h:
        yield h, await admin_session_headers(h.client, PASSWORD)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _fake_restarter(app, calls: list[str], *, debounce: float = 0.2):
    old = app.state.background_restart
    app.state.background_restart = BackgroundRestart(
        lambda: calls.append("restart"), needed=old._needed, debounce=debounce, min_interval=0, last_restart=0
    )


async def _put(h, headers, body):
    return await h.client.put("/admin/api/settings", headers=headers, json=body)


async def test_log_level_applies_to_the_running_logger_and_survives_a_failed_save(tmp_path):
    async with _admin(tmp_path) as (h, headers):
        level = (await _put(h, headers, {"log_level": "DEBUG"})).json()
        assert level["applying"] is False and "restart_required" not in level
        assert logging.getLogger().level == logging.DEBUG
        assert h.app.state.runtime.settings.log_level == "DEBUG"
        bad = await _put(h, headers, {"log_level": "LOUD"})
        assert bad.status_code == 400
        assert logging.getLogger().level == logging.DEBUG  # the old config keeps running


async def test_concurrent_log_level_saves_end_in_one_consistent_state(tmp_path):
    async with _admin(tmp_path) as (h, headers):
        levels = ["DEBUG", "INFO", "WARNING", "ERROR"] * 3
        responses = await asyncio.gather(*(_put(h, headers, {"log_level": value}) for value in levels))
        assert {r.status_code for r in responses} == {200}
        desired = h.app.state.admin_desired_settings.log_level
        assert h.app.state.runtime.settings.log_level == desired
        assert logging.getLevelName(logging.getLogger().level) == desired


async def test_proxy_trust_swaps_the_client_ip_resolver_atomically(tmp_path):
    async with _admin(tmp_path) as (h, headers):
        before = h.app.state.security.client_ip
        assert before.resolve("203.0.113.9", {"x-forwarded-for": "198.51.100.7"}) == "203.0.113.9"
        saved = await _put(h, headers, {"trusted_proxies": ["203.0.113.0/24"], "forwarded_header": "X-Forwarded-For"})
        assert saved.status_code == 200 and saved.json()["applying"] is False
        after = h.app.state.security.client_ip
        assert after is not before
        assert after.resolve("203.0.113.9", {"x-forwarded-for": "198.51.100.7"}) == "198.51.100.7"
        # A header without any trusted proxy is refused as a whole; the running resolver stays.
        refused = await _put(h, headers, {"trusted_proxies": [], "forwarded_header": "X-Forwarded-For"})
        assert refused.status_code == 400
        assert h.app.state.security.client_ip is after


async def test_changed_metric_selection_rebuilds_collection_and_failure_restores_the_old_one(tmp_path):
    async with _admin(tmp_path) as (h, headers):
        view = (await h.client.get("/admin/api/parameters")).json()
        numeric = [p["name"] for p in view["available"] if p["exportable"]]
        original = view["exposed_names"]
        wanted = [name for name in numeric if name not in original][:2]
        rebuilds: list[str] = []

        async def rebuilt() -> None:
            rebuilds.append("ok")

        h.app.state.reconfigure_devices = rebuilt
        body = {"exposed_names": wanted, "write_names": []}
        saved = await h.client.put("/admin/api/parameters", headers=headers, json=body)
        assert saved.status_code == 200 and "restart_required" not in saved.json()
        assert rebuilds == ["ok"] and h.app.state.admin_store.get("exposed_names") == wanted
        same = await h.client.put("/admin/api/parameters", headers=headers, json=body)
        assert same.status_code == 200 and rebuilds == ["ok"]  # an unchanged selection rebuilds nothing

        async def refused() -> None:
            raise ReconfigurationBuildError("boom")

        h.app.state.reconfigure_devices = refused
        failed = await h.client.put(
            "/admin/api/parameters", headers=headers, json={"exposed_names": original, "write_names": []}
        )
        assert failed.status_code == 409
        assert h.app.state.admin_store.get("exposed_names") == wanted  # saved back: store matches collector


async def test_listener_change_restarts_once_in_the_background(tmp_path):
    async with _admin(tmp_path) as (h, headers):
        calls: list[str] = []
        _fake_restarter(h.app, calls)
        first, second = _free_port(), _free_port()
        saved = await _put(h, headers, {"bind_port": first})
        assert saved.status_code == 200 and saved.json()["applying"] is True
        assert (await _put(h, headers, {"bind_port": second})).json()["applying"] is True
        assert calls == []  # debounced: nothing happens inside the request
        await asyncio.sleep(0.6)
        assert calls == ["restart"]  # two quick changes, one restart
        assert h.app.state.admin_desired_settings.bind_port == second


async def test_only_listener_settings_trigger_a_restart(tmp_path):
    async with _admin(tmp_path) as (h, headers):
        calls: list[str] = []
        _fake_restarter(h.app, calls, debounce=0.05)
        for body in ({"log_level": "WARNING"}, {"docs_public": False}, {"trusted_proxies": ["192.0.2.0/24"]}):
            response = await _put(h, headers, body)
            assert response.status_code == 200 and response.json()["applying"] is False
        await asyncio.sleep(0.3)
        assert calls == []


async def test_reverting_the_listener_before_the_timer_fires_cancels_the_restart(tmp_path):
    async with _admin(tmp_path) as (h, headers):
        calls: list[str] = []
        _fake_restarter(h.app, calls)
        running = h.app.state.runtime.settings.bind_port
        assert (await _put(h, headers, {"bind_port": _free_port()})).json()["applying"] is True
        assert (await _put(h, headers, {"bind_port": running})).json()["applying"] is False
        await asyncio.sleep(0.6)
        assert calls == []


async def test_unbindable_listener_is_refused_and_nothing_changes(tmp_path):
    async with _admin(tmp_path) as (h, headers):
        calls: list[str] = []
        _fake_restarter(h.app, calls, debounce=0.05)
        before = h.app.state.admin_desired_settings
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            busy = taken.getsockname()[1]
            refused = await _put(h, headers, {"bind_port": busy})
        assert refused.status_code == 409 and "previous listener stays active" in refused.json()["detail"]
        assert h.app.state.admin_desired_settings == before
        assert h.app.state.admin_store.get("operator_settings").get("bind_port") != busy
        await asyncio.sleep(0.2)
        assert calls == []


def test_probe_tolerates_the_own_port_and_refuses_a_foreign_address():
    with socket.socket() as own:
        own.bind(("127.0.0.1", 0))
        own.listen()
        port = own.getsockname()[1]
        assert probe_listener("127.0.0.1", port, port_unchanged=True) is None
        assert probe_listener("127.0.0.1", port, port_unchanged=False) is not None
    assert "cannot listen" in probe_listener("203.0.113.77", 40000, port_unchanged=True)


async def test_background_restart_keeps_a_minimum_distance_after_a_restart():
    calls: list[str] = []
    loop = asyncio.get_running_loop()
    restart = BackgroundRestart(
        lambda: calls.append("restart"), debounce=0.01, min_interval=1.0, last_restart=100.0, wall_clock=lambda: 100.7
    )
    restart.request("listener", loop)
    await asyncio.sleep(0.15)
    assert calls == []  # a restart right after a restart waits out the guard instead of looping
    restart.cancel()


def test_restart_hands_the_inverters_back_before_the_process_is_replaced(monkeypatch):
    order: list[str] = []

    async def shutdown() -> int:
        order.append("handback")
        return 0

    app = SimpleNamespace(state=SimpleNamespace(runtime=SimpleNamespace(shutdown=SimpleNamespace(run=shutdown))))

    async def fake_serve(self, sockets=None) -> None:
        app.state.server_restart()  # what the background restarter calls on the event loop
        await self._drain
        order.append("listener closed")

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    monkeypatch.setattr(server, "_reexec", lambda: order.append("re-exec"))
    settings = make_settings()
    assert server.run_server(app, settings) == 0
    assert order == ["handback", "listener closed", "re-exec"]


def test_failed_reexec_exits_nonzero_so_the_supervisor_restarts(monkeypatch):
    app = SimpleNamespace(state=SimpleNamespace(runtime=SimpleNamespace(shutdown=None)))

    async def fake_serve(self, sockets=None) -> None:
        self.restart_requested = True

    def broken() -> None:
        raise OSError("exec failed")

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    monkeypatch.setattr(server, "_reexec", broken)
    assert server.run_server(app, make_settings()) == 1
