#!/usr/bin/env python3
#
# tests/test_admin_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Admin bootstrap, browser session, PAT and encrypted persistence behavior."""

import re
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.admin.api import _normalize_devices
from app.admin.store import AdminStore
from app.api.app_factory import create_app
from app.api.server import first_start_banner
from app.config import Settings


def test_admin_host_rule_is_a_plausibility_check() -> None:
    assert _normalize_devices([{"host": "bad-.example", "port": 8899}])[0]["host"] == "bad-.example"
    with pytest.raises(HTTPException, match="plain IP address or host name"):
        _normalize_devices([{"host": "https://example.org/path", "port": 8899}])


@pytest.mark.asyncio
async def test_admin_first_login_change_and_pat(tmp_path):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    password = app.state.first_start_password
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        before = await client.get("/admin/api/session")
        assert before.json()["authenticated"] is False
        csrf = before.json()["csrf_token"]
        denied = await client.post("/admin/api/login", json={"username": "admin", "password": password})
        assert denied.status_code == 403
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        assert login.status_code == 200
        assert login.json()["must_change_password"] is True
        csrf = login.json()["csrf_token"]
        assert (await client.get("/admin/api/settings")).status_code == 403
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": csrf},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        assert changed.status_code == 200
        csrf = changed.json()["csrf_token"]
        assert (await client.get("/admin/api/settings")).status_code == 200
        saved = await client.put("/admin/api/settings", headers={"X-CSRF-Token": csrf},
                                 json={"docs_public": True, "influxdb_token": "private-influx-token"})
        assert saved.status_code == 200
        assert saved.json()["settings"]["influxdb_token_configured"] is True
        assert "private-influx-token" not in saved.text
        assert app.state.runtime.settings.docs_public is True
        assert app.state.admin_store.get("operator_settings")["influxdb_token"] == "private-influx-token"
        assert b"private-influx-token" not in (tmp_path / "rct.db").read_bytes()
        params = await client.get("/admin/api/parameters")
        assert params.status_code == 200
        updated_params = await client.put("/admin/api/parameters", headers={"X-CSRF-Token": csrf},
                                          json={"exposed_names": [], "write_names": []})
        assert updated_params.status_code == 200
        assert updated_params.json()["exposed_names"] == []
        assert (await client.post("/admin/api/tokens", json={"name": "test", "role": "read"})).status_code == 403
        made = await client.post("/admin/api/tokens", headers={"X-CSRF-Token": csrf},
                                 json={"name": "test", "role": "read"})
        assert made.status_code == 201
        token = made.json()["token"]
        assert app.state.security.tokens.authenticate("Bearer " + token).role.value == "read"
        listed = await client.get("/admin/api/tokens")
        assert token not in listed.text
        removed = await client.delete("/admin/api/tokens/" + made.json()["id"],
                                      headers={"X-CSRF-Token": csrf})
        assert removed.status_code == 200
        from app.errors import AuthenticationError

        with pytest.raises(AuthenticationError):
            app.state.security.tokens.authenticate("Bearer " + token)
    restarted = create_app(settings)
    assert restarted.state.first_start_password is None
    assert restarted.state.runtime.exporter._exposed == []
    assert restarted.state.admin_desired_settings.influxdb_token.get_secret_value() == "private-influx-token"


def test_admin_database_requires_original_secret(tmp_path):
    path = tmp_path / "rct.db"
    first = AdminStore(path, "x" * 48)
    assert first.initialize()
    first.put("operator_settings", {"docs_public": True})
    with pytest.raises(ValueError, match="HMAC_SECRET"):
        AdminStore(path, "y" * 48).initialize()


def test_database_pat_revocation_persists_across_restart(tmp_path):
    path = tmp_path / "rct.db"
    store = AdminStore(path, "x" * 48)
    store.initialize()
    _, token = store.create_token("test", "read/write", None)
    entry = store.authenticate_token(token)
    assert entry is not None and entry.role == "read/write"
    assert store.delete_token(entry.id)
    restarted = AdminStore(path, "x" * 48)
    restarted.initialize()
    assert restarted.authenticate_token(token) is None


def test_bootstrap_password_is_eight_chars_with_one_special() -> None:
    from app.admin.store import _BOOTSTRAP_SPECIALS, _bootstrap_password

    for _ in range(200):
        password = _bootstrap_password()
        assert len(password) == 8
        assert sum(c in _BOOTSTRAP_SPECIALS for c in password) == 1


@pytest.mark.asyncio
async def test_first_start_issues_no_token(tmp_path, capsys):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    assert capsys.readouterr().out == ""  # the banner is printed by the server after startup
    banner = first_start_banner(
        app.state.runtime.settings, app.state.first_start_password, tmp_path / "initial-admin-password"
    )
    assert app.state.first_start_password and "token" not in banner.lower()
    assert app.state.admin_store.list_tokens() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_required", [True, False])
async def test_admin_writes_need_admin_session_or_read_write_pat(tmp_path, auth_required):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db",
                        auth_required=auth_required)
    app = create_app(settings)
    password = app.state.first_start_password
    store = app.state.admin_store
    change = {"docs_public": True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        def bearer(token):
            return {"Authorization": "Bearer " + token}

        # Before the first login every PAT is inert for the admin API.
        early = store.create_token("early", "read/write", None)[1]
        assert (await client.put("/admin/api/settings", json=change, headers=bearer(early))).status_code == 403
        assert (await client.get("/admin/api/settings", headers=bearer(early))).status_code == 403
        assert store.change_password(password, "a much stronger password")
        write_pat = store.create_token("rw", "read/write", None)[1]
        read_pat = store.create_token("ro", "read", None)[1]
        for method, url, body in (("PUT", "/admin/api/settings", change),
                                  ("PUT", "/admin/api/parameters", {"exposed_names": [], "write_names": []}),
                                  ("POST", "/admin/api/tokens", {"name": "x", "role": "read"})):
            assert (await client.request(method, url, json=body)).status_code == 401
            assert (await client.request(method, url, json=body, headers=bearer(read_pat))).status_code == 403
            assert (await client.request(method, url, json=body, headers=bearer("pat_" + "A" * 46))).status_code == 401
        assert (await client.get("/admin/api/settings")).status_code == 401
        assert (await client.get("/admin/api/settings", headers=bearer(read_pat))).status_code == 200
        done = await client.put("/admin/api/settings", json=change, headers=bearer(write_pat))
        assert done.status_code == 200 and done.json()["settings"]["docs_public"] is True
        made = await client.post("/admin/api/tokens", json={"name": "x", "role": "read"}, headers=bearer(write_pat))
        assert made.status_code == 201


def test_admin_password_minimum_is_eight_characters():
    from app.admin.api import PasswordChange

    assert PasswordChange(current_password="x", new_password="12345678")
    with pytest.raises(ValidationError):
        PasswordChange(current_password="x", new_password="1234567")


def test_first_start_banner_carries_the_password_and_names_the_fallback_file(tmp_path):
    from app.api.server import first_start_banner
    from app.config import Settings

    password_file = tmp_path / "initial-admin-password"
    text = first_start_banner(Settings(_env_file=None, bind_port=8123), "XnQ3!4wk", password_file)
    assert "XnQ3!4wk" in text  # deliberate product choice: copy-pasteable from the console
    assert str(password_file) in text and "http://127.0.0.1:8123/" in text
    assert text.startswith("\n=") and text.endswith("=\n")
    assert re.search(r"Password:\s+(\S+)", text).group(1) == "XnQ3!4wk"


@pytest.mark.asyncio
async def test_server_prints_the_banner_after_startup_only_on_first_start(tmp_path, capsys):
    import asyncio
    import socket

    import uvicorn

    from app.api.server import GracefulServer

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db", bind_port=port)

    async def run_once(sock: socket.socket) -> str:
        # The bound socket stays open and is handed straight to uvicorn (no close-then-rebind
        # gap), so another process cannot steal the port in between (TOCTOU). uvicorn's own
        # shutdown() closes the socket it was given, so each run needs a fresh one on the same port.
        app = create_app(settings)
        server = GracefulServer(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None), app.state.runtime)
        server.install_signal_handlers = lambda: None
        task = asyncio.create_task(server.serve(sockets=[sock]))
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    task.result()  # re-raise if serve() already failed instead of looping forever
                await asyncio.sleep(0.05)
        server.should_exit = True
        await task
        return capsys.readouterr().out

    first = await run_once(sock)
    assert "FIRST START - admin login" in first and f"http://127.0.0.1:{port}/" in first
    second_sock = socket.socket()
    second_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    second_sock.bind(("127.0.0.1", port))
    assert "FIRST START" not in await run_once(second_sock)


@pytest.mark.asyncio
async def test_admin_api_errors_keep_their_own_detail(tmp_path):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        bad = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                json={"username": "admin", "password": "wrong-password"})
        assert bad.status_code == 401 and bad.json() == {"detail": "Invalid credentials"}
        assert "bearer" not in (await client.get("/admin/api/settings")).text.lower()
        assert (await client.get("/api/v1/devices")).json()["code"] == "missing_token"  # API problems unchanged


@pytest.mark.asyncio
async def test_resaving_an_unchanged_export_field_does_not_restart_the_exporter(tmp_path):
    """Regression for the questdb-rollup-warning investigation (2026-10-04): a debounced autosave
    that resends an _EXPORT_RESTART_KEYS field with its already-stored value (e.g. an unrelated
    keystroke in the same TSDB form) must not restart the export task -- only PUTs that actually
    change one of those values may do that."""
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    password = app.state.first_start_password
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        headers = {"X-CSRF-Token": changed.json()["csrf_token"]}
        base = await client.put("/admin/api/settings", headers=headers,
                                json={"db_type": "questdb", "questdb_hostname": "localhost"})
        assert base.status_code == 200

        with patch("app.admin.api._restart_export") as mocked:
            resend_same_value = await client.put("/admin/api/settings", headers=headers,
                                                  json={"questdb_hostname": "localhost"})
            assert resend_same_value.status_code == 200
            mocked.assert_not_called()

            resend_with_change = await client.put("/admin/api/settings", headers=headers,
                                                   json={"questdb_hostname": "example.org"})
            assert resend_with_change.status_code == 200
            mocked.assert_called_once()


@pytest.mark.asyncio
async def test_switching_db_type_still_restarts_the_exporter(tmp_path):
    """Not a regression test by itself, kept next to the unchanged-value case above to make the
    diff visible from both directions: a real change (including empty-to-value and type-to-type)
    must still trigger a restart."""
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    password = app.state.first_start_password
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        headers = {"X-CSRF-Token": changed.json()["csrf_token"]}

        with patch("app.admin.api._restart_export") as mocked:
            first_type = await client.put("/admin/api/settings", headers=headers,
                                          json={"db_type": "questdb", "questdb_hostname": "localhost"})
            assert first_type.status_code == 200
            mocked.assert_called_once()

            mocked.reset_mock()
            second_type = await client.put(
                "/admin/api/settings", headers=headers,
                json={"db_type": "influxdb_v2", "influxdb_hostname": "localhost", "influxdb_organization": "o",
                      "influxdb_bucket": "b", "influxdb_token": "t"},
            )
            assert second_type.status_code == 200
            mocked.assert_called_once()


def test_fresh_install_starts_with_empty_write_selection(tmp_path):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db", enable_write_support=True)
    app = create_app(settings)
    assert app.state.admin_store.get("write_names") == []
    assert app.state.default_write_entries  # the shipped catalog stays selectable in the GUI
    assert len(app.state.runtime.gateway._allowlist) == 0  # nothing is writable until the operator selects it


def test_devices_and_energy_manager_share_one_rct_energy_readings_instance(tmp_path):
    """The dashboard energy_flow projection (devices()) and the EnergyManager must read through the
    SAME RctEnergyReadings instance, so a verified sign is reflected in both without a restart and a
    future refactor cannot silently split them (design §4.2 / §16 item 2). The reach into the
    manager's private ``_readings`` is intentional: the port exposes no public readings accessor and
    ``runtime.energy`` is typed ``EnergyManagerPort`` (which has none)."""
    # Dispatch disabled: a single default RctEnergyReadings, shared with the manager.
    disabled = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(disabled)
    runtime = app.state.runtime
    assert runtime.dispatch is None
    assert runtime.energy is not None
    assert runtime.energy_readings is runtime.energy._readings

    # Dispatch enabled: still exactly one shared instance, and it uses the LIVE dispatch capability
    # registry (not a fresh CapabilityRegistry()), so a verification reaches the published sign.
    from app.dispatch.capabilities import CapabilityRegistry

    enabled = Settings(
        _env_file=None,
        hmac_secret="s" * 48,
        admin_db_path=tmp_path / "rct-enabled.db",
        dispatch_db_path=tmp_path / "rct-dispatch.db",
        enable_write_support=True,
    )
    app2 = create_app(enabled)
    runtime2 = app2.state.runtime
    assert runtime2.dispatch is not None
    assert runtime2.energy is not None
    assert runtime2.energy_readings is runtime2.energy._readings
    # The shared instance's registry is the live dispatch registry, not an empty default.
    assert isinstance(runtime2.energy_readings._capabilities, CapabilityRegistry)
    assert runtime2.energy_readings._capabilities is runtime2.dispatch._capabilities
