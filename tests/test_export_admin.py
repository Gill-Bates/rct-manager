#!/usr/bin/env python3
#
# tests/test_export_admin.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Export settings through the admin API: validation, write-only secrets, persistence."""


import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.admin.api import _changed_session_only, _restart_export, _run_on_loop, _TransitionBusy
from app.api.app_factory import create_app
from app.config import DbType, QuestDbDownsampling, Settings


async def test_export_settings_round_trip(tmp_path):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    password = app.state.first_start_password
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        headers = {"X-CSRF-Token": changed.json()["csrf_token"]}
        shown = (await client.get("/admin/api/settings")).json()["settings"]
        assert shown["db_type"] is None and shown["questdb_downsampling"] == "off"
        assert "questdb_password" not in shown and shown["questdb_password_configured"] is False

        incomplete = await client.put("/admin/api/settings", headers=headers, json={"db_type": "influxdb_v2"})
        assert incomplete.status_code == 400
        body = {"db_type": "questdb", "questdb_hostname": "localhost", "questdb_username": "u",
                "questdb_password": "pw-secret", "questdb_downsampling": "medium",
                "questdb_raw_retention_days": 3, "questdb_retention_days": 90}
        saved = await client.put("/admin/api/settings", headers=headers, json=body)
        assert saved.status_code == 200 and "pw-secret" not in saved.text
        assert saved.json()["settings"]["questdb_password_configured"] is True
        # TSDB target/connection/retention settings restart the push exporter task in place,
        # not the whole application: none of them appear in restart_required any more.
        assert "restart_required" not in saved.json()
        assert "db_type" in saved.json()["live"]
        # an empty secret keeps the stored one; an invalid raw retention is rejected
        keep = await client.put("/admin/api/settings", headers=headers,
                                json={"questdb_password": "", "questdb_retention_days": 60})
        assert keep.status_code == 200
        bad = await client.put("/admin/api/settings", headers=headers, json={"questdb_raw_retention_days": 99})
        assert bad.status_code == 400
    restarted = create_app(settings)
    desired = restarted.state.admin_desired_settings
    assert desired.db_type is DbType.QUESTDB and desired.questdb_downsampling is QuestDbDownsampling.MEDIUM
    assert desired.questdb_password.get_secret_value() == "pw-secret" and desired.questdb_retention_days == 60
    assert restarted.state.runtime.settings.db_type is DbType.QUESTDB


async def test_metrics_export_enabled_toggle_is_orthogonal_to_db_type(tmp_path):
    """`metrics_export_enabled` defaults to True (existing db_type configs keep exporting) and,
    once set to False, withholds the export regardless of db_type; the connection fields stay
    saved and visible, unlike resetting db_type back to "" (Disabled)."""
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    assert settings.metrics_export_enabled is True
    app = create_app(settings)
    password = app.state.first_start_password
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        headers = {"X-CSRF-Token": changed.json()["csrf_token"]}
        shown = (await client.get("/admin/api/settings")).json()["settings"]
        assert shown["metrics_export_enabled"] is True

        body = {"db_type": "questdb", "questdb_hostname": "localhost", "questdb_downsampling": "off"}
        saved = await client.put("/admin/api/settings", headers=headers, json=body)
        assert saved.status_code == 200

        paused = await client.put("/admin/api/settings", headers=headers,
                                  json={"metrics_export_enabled": False})
        assert paused.status_code == 200
        assert "restart_required" not in paused.json()
        assert "metrics_export_enabled" in paused.json()["live"]
        after_pause = (await client.get("/admin/api/settings")).json()["settings"]
        # db_type and the hostname are still there: pausing does not reset the connection config.
        assert after_pause["metrics_export_enabled"] is False
        assert after_pause["db_type"] == "questdb" and after_pause["questdb_hostname"] == "localhost"


async def test_dashboard_tsdb_tile_distinguishes_paused_from_unconfigured_and_failing(tmp_path):
    """`_tsdb_view` (surfaced via GET /admin/api/devices as `tsdb`) must let the dashboard tell
    a deliberately paused export apart from an unconfigured one and from a genuinely stale one:
    `healthy` stays `None` (neutral) when `db_type` is unset, and `export_enabled` is `False`
    while `healthy` is `False` only when the export loop is actually not pushing because the
    user paused it, not because of a real failure."""
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    password = app.state.first_start_password
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        headers = {"X-CSRF-Token": changed.json()["csrf_token"]}

        not_configured = (await client.get("/admin/api/devices")).json()["tsdb"]
        assert not_configured["configured"] is False and not_configured["healthy"] is None

        body = {"db_type": "questdb", "questdb_hostname": "localhost", "questdb_downsampling": "off"}
        assert (await client.put("/admin/api/settings", headers=headers, json=body)).status_code == 200

        paused = await client.put("/admin/api/settings", headers=headers,
                                  json={"metrics_export_enabled": False})
        assert paused.status_code == 200
        tsdb = (await client.get("/admin/api/devices")).json()["tsdb"]
        assert tsdb["configured"] is True and tsdb["export_enabled"] is False
        # The frontend tile keys the neutral "Export paused" branch off export_enabled, not off
        # healthy, so healthy may legitimately be False here; the regression this guards against
        # is export_enabled going missing or inverted, which would make the tile fall back to
        # the red "Failing" branch for a deliberately paused export.


async def test_resaving_an_unchanged_enabled_value_does_not_restart_the_exporter(tmp_path):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    password = app.state.first_start_password
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        headers = {"X-CSRF-Token": changed.json()["csrf_token"]}
        base = await client.put("/admin/api/settings", headers=headers,
                                json={"db_type": "questdb", "questdb_hostname": "localhost"})
        assert base.status_code == 200
        from unittest.mock import patch

        with patch("app.admin.api._restart_export") as mocked:
            resend_same_value = await client.put("/admin/api/settings", headers=headers,
                                                  json={"metrics_export_enabled": True})
            assert resend_same_value.status_code == 200
            mocked.assert_not_called()


async def test_export_target_change_takes_effect_without_a_restart(tmp_path):
    """Saving a new TSDB target restarts the running push-exporter task in place: the next cycle
    pushes to the new target, no application restart needed (restart_required stays empty)."""
    import asyncio
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from tests.api_helpers import make_settings, running_app

    class _Server:
        def __init__(self) -> None:
            self.hits = 0
            outer = self

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self) -> None:
                    outer.hits += 1
                    length = int(self.headers.get("Content-Length") or 0)
                    self.rfile.read(length)
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

                def do_GET(self) -> None:
                    body = json.dumps({"dataset": []}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args) -> None:
                    pass

            self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
            self.url = f"http://127.0.0.1:{self.httpd.server_port}"
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

        def close(self) -> None:
            self.httpd.shutdown()
            self.httpd.server_close()

    first, second = _Server(), _Server()
    try:
        settings = make_settings(
            hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db", db_type="questdb",
            questdb_hostname=first.url, metrics_export_interval_seconds=5,
        )
        async with running_app(settings, settle=False) as h:
            assert h.app.state.admin_store.change_password(
                h.app.state.first_start_password, "replacement-test-password"
            )
            h.client.headers.pop("Authorization")
            csrf = (await h.client.get("/admin/api/session")).json()["csrf_token"]
            login = await h.client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                        json={"username": "admin", "password": "replacement-test-password"})
            assert login.status_code == 200
            for _ in range(100):
                if first.hits:
                    break
                await asyncio.sleep(0.05)
            assert first.hits >= 1, "exporter never pushed to the original target"
            before_task = h.app.state.runtime.export_task
            assert before_task is not None

            response = await h.client.put(
                "/admin/api/settings", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                json={"questdb_hostname": second.url},
            )
            assert response.status_code == 200, response.text
            assert "restart_required" not in response.json()
            assert "questdb_hostname" in response.json()["live"]
            assert h.app.state.runtime.settings.questdb_hostname == second.url

            for _ in range(100):
                if second.hits:
                    break
                await asyncio.sleep(0.05)
            assert second.hits >= 1, "exporter never picked up the new target"
            # The old task was cancelled and a new one started, not left running alongside it.
            assert before_task.cancelled() or before_task.done()
            assert h.app.state.runtime.export_task is not None
            assert h.app.state.runtime.export_task is not before_task
            assert before_task not in h.app.state.runtime.tasks
    finally:
        first.close()
        second.close()


async def test_disabling_export_stops_the_running_task_without_a_restart(tmp_path):
    """Toggling `metrics_export_enabled` to False while export is running stops the push task in
    place (no application restart); toggling back to True with the same db_type starts it again."""
    import asyncio
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from unittest.mock import patch

    from tests.api_helpers import WRITE_TOKEN, make_settings, running_app

    class _Server:
        def __init__(self) -> None:
            self.hits = 0
            outer = self

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self) -> None:
                    outer.hits += 1
                    length = int(self.headers.get("Content-Length") or 0)
                    self.rfile.read(length)
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

                def do_GET(self) -> None:
                    body = json.dumps({"dataset": []}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args) -> None:
                    pass

            self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
            self.url = f"http://127.0.0.1:{self.httpd.server_port}"
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

        def close(self) -> None:
            self.httpd.shutdown()
            self.httpd.server_close()

    server = _Server()
    try:
        settings = make_settings(
            hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db", db_type="questdb",
            questdb_hostname=server.url, metrics_export_interval_seconds=5,
        )
        from app.admin.store import AdminStore
        from app.security.tokens import TokenStore

        # Pre-change the bootstrap password at admin_db_path, same pattern as
        # test_app_starts_with_export_and_contains_failures above: create_app() (inside
        # running_app) builds its own admin_store from settings.admin_db_path and that store's
        # own password-change gate must already be past, independently of the second AdminStore
        # running_app() builds for session/token handling in its own temporary directory.
        store = AdminStore(settings.admin_db_path, "s" * 48)
        try:
            assert store.change_password(store.initialize(), "a much stronger password")
            with patch("app.admin.store.generate_pat", side_effect=[WRITE_TOKEN]):
                store.create_token("write test token", "read/write", None)
            async with running_app(settings, settle=False) as h:
                # Swap in the token store that actually authenticates against settings.admin_db_path
                # (the admin_store running_app() builds and installs lives in its own, unrelated
                # temporary directory, so its default WRITE_TOKEN fixture is not valid against this
                # app's admin_store).
                h.app.state.security.tokens = TokenStore(auth_required=settings.auth_required, admin_store=store)
                for _ in range(100):
                    if server.hits:
                        break
                    await asyncio.sleep(0.05)
                assert server.hits >= 1, "exporter never pushed before being paused"
                running_task = h.app.state.runtime.export_task
                assert running_task is not None

                paused = await h.client.put(
                    "/admin/api/settings", headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
                    json={"metrics_export_enabled": False},
                )
                assert paused.status_code == 200, paused.text
                assert "restart_required" not in paused.json()
                for _ in range(100):
                    if running_task.cancelled() or running_task.done():
                        break
                    await asyncio.sleep(0.05)
                assert running_task.cancelled() or running_task.done()
                assert running_task not in h.app.state.runtime.tasks
                # Finding 3/4: an explicit handle, not a 0.3 s timing-based negative assertion, proves
                # that disabling export left no export task registered at all (not even a replacement).
                assert h.app.state.runtime.export_task is None

                hits_while_paused = server.hits

                resumed = await h.client.put(
                    "/admin/api/settings", headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
                    json={"metrics_export_enabled": True},
                )
                assert resumed.status_code == 200, resumed.text
                for _ in range(100):
                    if server.hits > hits_while_paused:
                        break
                    await asyncio.sleep(0.05)
                assert server.hits > hits_while_paused, "export never resumed after re-enabling"
        finally:
            store.close()
    finally:
        server.close()


async def test_app_starts_with_export_and_contains_failures(tmp_path):
    """Lifespan with the export enabled against a closed port: the API stays up, health is exposed."""
    import asyncio
    import socket

    from tests.api_helpers import make_settings, running_app

    # Established pattern from test_export_pusher.py: bind an ephemeral port, note the address,
    # close it, then use that now-provably-closed address instead of the unguaranteed 127.0.0.1:1.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    closed_url = f"http://127.0.0.1:{probe.getsockname()[1]}"
    probe.close()

    settings = make_settings(
        hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db", db_type="questdb",
        questdb_hostname=closed_url, metrics_export_interval_seconds=5,
    )
    from app.admin.store import AdminStore

    store = AdminStore(settings.admin_db_path, "s" * 48)  # the export only starts after the first password change
    try:
        assert store.change_password(store.initialize(), "a much stronger password")
    finally:
        store.close()
    async with running_app(settings, settle=False) as h:
        for _ in range(100):
            if h.app.state.runtime.exporter._service.export_failures:
                break
            await asyncio.sleep(0.05)
        assert h.app.state.runtime.exporter._service.export_failures >= 1
        text = (await h.client.get("/metrics")).text
        assert 'rct_export_pushes_total{result="error"}' in text
        assert (await h.client.get("/admin/api/session")).status_code == 200


def _request_with_loop(**hooks):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    state = SimpleNamespace(loop=loop, **hooks)
    return SimpleNamespace(app=SimpleNamespace(state=state)), loop, thread


def test_a_timed_out_transition_blocks_the_next_one_until_it_finishes():
    release = threading.Event()

    async def slow() -> None:
        while not release.is_set():
            await asyncio.sleep(0.01)

    request, loop, thread = _request_with_loop(restart_export=slow)
    try:
        with pytest.raises(TimeoutError):
            _run_on_loop(request, "restart_export", 0.05)
        with pytest.raises(_TransitionBusy):
            _run_on_loop(request, "restart_export", 0.05)
        release.set()
        request.app.state.live_transition.result(timeout=2)
        _run_on_loop(request, "restart_export", 2)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_a_failed_export_restart_is_reported_not_swallowed():
    async def broken() -> None:
        raise RuntimeError("boom")

    request, loop, thread = _request_with_loop(restart_export=broken)
    try:
        with pytest.raises(HTTPException) as caught:
            _restart_export(request)
        assert caught.value.status_code == 500
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "body",
    [{"questdb_downsampling": "high"}, {"influxdb_token": None}, {"questdb_password": None}],
)
def test_a_pat_cannot_change_retention_presets_or_delete_credentials(body):
    settings = Settings(_env_file=None, hmac_secret="s" * 48)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(admin_desired_settings=settings)))
    assert _changed_session_only(request, body) == set(body)
