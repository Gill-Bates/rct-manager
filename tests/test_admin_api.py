#!/usr/bin/env python3
#
# tests/test_admin_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Admin bootstrap, browser session, PAT and encrypted persistence behavior."""

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from ipaddress import ip_network
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.admin.api import _normalize_devices
from app.admin.store import AdminStore
from app.api.app_factory import create_app
from app.api.server import first_start_banner
from app.config import DEVICE_ID_PATTERN, MAX_DEVICES, DeviceEntry, Settings


def test_admin_host_rule_is_a_plausibility_check() -> None:
    assert _normalize_devices([{"host": "bad-.example", "port": 8899}])[0]["host"] == "bad-.example"
    with pytest.raises(HTTPException, match="plain IP address or host name"):
        _normalize_devices([{"host": "https://example.org/path", "port": 8899}])


@pytest.mark.parametrize("device_id", ["A", "x_1.2-3", "-bad", "a" * 65, "a/b"])
def test_admin_and_config_share_device_id_rule(device_id: str) -> None:
    valid = DEVICE_ID_PATTERN.fullmatch(device_id) is not None
    if valid:
        assert _normalize_devices([{"host": "localhost", "device_id": device_id}])[0]["device_id"] == device_id
        assert DeviceEntry(device_id=device_id, host="localhost").device_id == device_id
    else:
        with pytest.raises(HTTPException, match="device id"):
            _normalize_devices([{"host": "localhost", "device_id": device_id}])
        with pytest.raises(ValidationError):
            DeviceEntry(device_id=device_id, host="localhost")


def test_admin_and_config_share_device_count_limit() -> None:
    devices = [{"host": f"host-{index}"} for index in range(MAX_DEVICES + 1)]
    with pytest.raises(HTTPException, match=f"at most {MAX_DEVICES}"):
        _normalize_devices(devices)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, devices=[DeviceEntry(device_id=f"d{index}", host=f"host-{index}")
                                           for index in range(MAX_DEVICES + 1)])


@pytest.mark.asyncio
async def test_admin_first_login_change_and_pat(tmp_path):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    password = app.state.first_start_password
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
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
        # The disclaimer must still be accepted before any normal admin endpoint is usable.
        assert (await client.get("/admin/api/settings")).status_code == 403
        accepted = await client.post("/admin/api/accept-disclaimer", headers={"X-CSRF-Token": csrf})
        assert accepted.status_code == 200 and accepted.json()["accepted"] is True
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


@pytest.mark.asyncio
async def test_live_charge_limit_freshness_persists_and_reaches_dispatch_config_after_restart(tmp_path):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    assert app.state.admin_store.change_password(app.state.first_start_password, "a much stronger password")
    app.state.admin_store.accept_disclaimer()  # a PAT stays inert until the disclaimer is accepted
    token = app.state.admin_store.create_token("settings", "read/write", None)[1]
    headers = {"Authorization": f"Bearer {token}"}
    key = "dispatch_live_charge_limit_max_age_seconds"
    assert app.state.dispatch_config.live_charge_limit_max_age_seconds == 5.0

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        for invalid in (0.9, 15.1):
            rejected = await client.put("/admin/api/settings", headers=headers, json={key: invalid})
            assert rejected.status_code == 400
        saved = await client.put("/admin/api/settings", headers=headers, json={key: 8.0})
        assert saved.status_code == 200
        assert saved.json()["settings"][key] == 8.0
        assert app.state.admin_store.get("operator_settings")[key] == 8.0
        assert app.state.dispatch_config.live_charge_limit_max_age_seconds == 5.0

    restarted = create_app(settings)
    assert restarted.state.admin_desired_settings.dispatch_live_charge_limit_max_age_seconds == 8.0
    assert restarted.state.dispatch_config.live_charge_limit_max_age_seconds == 8.0


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
    assert entry.id in store._last_touch
    assert store.delete_token(entry.id)
    assert entry.id not in store._last_touch
    restarted = AdminStore(path, "x" * 48)
    restarted.initialize()
    assert restarted.authenticate_token(token) is None


def test_unknown_pat_role_is_rejected_before_storage(tmp_path):
    store = AdminStore(tmp_path / "rct.db", "x" * 48)
    store.initialize()
    with pytest.raises(ValueError, match="invalid token role"):
        store.create_token("test", "administrator", None)
    assert store.list_tokens() == []


def test_create_token_persists_and_lists_last_four_characters(tmp_path):
    store = AdminStore(tmp_path / "rct.db", "x" * 48)
    store.initialize()
    record, token = store.create_token("test", "read", None)
    assert record["suffix"] == token[-4:] and len(record["suffix"]) == 4
    listed = store.list_tokens()
    assert listed[0]["suffix"] == token[-4:]


def test_record_without_suffix_field_lists_without_breaking(tmp_path):
    store = AdminStore(tmp_path / "rct.db", "x" * 48)
    store.initialize()
    record, _token = store.create_token("old", "read", None)
    record.pop("suffix")  # an old record predates the suffix field
    with store.connect() as db:
        db.execute("UPDATE pats SET encrypted=? WHERE id=?", (store._encode(record["id"], record), record["id"]))
    assert store.list_tokens()[0]["suffix"] is None


@pytest.mark.asyncio
async def test_get_tokens_returns_suffix(tmp_path):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    password = app.state.first_start_password
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        before = await client.get("/admin/api/session")
        csrf = before.json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        csrf = login.json()["csrf_token"]
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": csrf},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        csrf = changed.json()["csrf_token"]
        assert (await client.post("/admin/api/accept-disclaimer",
                                  headers={"X-CSRF-Token": csrf})).status_code == 200
        made = await client.post("/admin/api/tokens", headers={"X-CSRF-Token": csrf},
                                 json={"name": "test", "role": "read"})
        assert made.status_code == 201
        suffix = made.json()["token"][-4:]
        assert made.json()["suffix"] == suffix
        listed = await client.get("/admin/api/tokens")
        assert listed.json()["tokens"][0]["suffix"] == suffix


@pytest.mark.parametrize("writer_has_transaction", [False, True])
def test_pat_revocation_with_pending_last_used_writer(tmp_path, monkeypatch, writer_has_transaction):
    store = AdminStore(tmp_path / "rct.db", "x" * 48)
    store.initialize()
    record, token = store.create_token("test", "read", None)
    writer_paused = threading.Event()
    release_writer = threading.Event()
    delete_started = threading.Event()
    connect = store.connect

    def pause_writer():
        writer_paused.set()
        if not release_writer.wait(5):
            raise TimeoutError("Test did not release the token writer")

    @contextmanager
    def controlled_connect():
        is_writer = threading.current_thread().name.startswith("pat-last-used")
        with connect() as database:
            if is_writer and not writer_has_transaction:
                pause_writer()
            if not is_writer:
                database.set_trace_callback(lambda sql: delete_started.set() if sql.startswith("DELETE") else None)
            yield database
            if is_writer and writer_has_transaction:
                pause_writer()

    monkeypatch.setattr(store, "connect", controlled_connect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            assert store.authenticate_token(token) is not None
            assert writer_paused.wait(5)
            assert record["id"] in store._last_touch
            deletion = pool.submit(store.delete_token, record["id"])
            assert delete_started.wait(5)
            if writer_has_transaction:
                assert not deletion.done()
                release_writer.set()
            assert deletion.result(timeout=5)
            assert record["id"] not in store._last_touch
        finally:
            release_writer.set()
            store.close()
    assert record["id"] not in store._last_touch
    assert store.authenticate_token(token) is None
    assert not store.delete_token(record["id"])
    assert store.list_tokens() == []


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
    assert capsys.readouterr().out == ""  # create_app stays silent; the server prints the banner
    banner = first_start_banner(app.state.runtime.settings, app.state.first_start_password)
    assert app.state.first_start_password and "token" not in banner.lower()
    assert app.state.first_start_password in banner  # the one-time password is now shown on the console
    assert app.state.admin_store.list_tokens() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_required", [True, False])
async def test_admin_writes_need_admin_session_or_read_write_pat(tmp_path, auth_required):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db",
                        auth_required=auth_required)
    app = create_app(settings)
    password = app.state.first_start_password
    store = app.state.admin_store
    change = {"metrics_rate_limit_requests": 120}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        def bearer(token):
            return {"Authorization": "Bearer " + token}

        # Before the first login every PAT is inert for the admin API.
        early = store.create_token("early", "read/write", None)[1]
        assert (await client.put("/admin/api/settings", json=change, headers=bearer(early))).status_code == 403
        assert (await client.get("/admin/api/settings", headers=bearer(early))).status_code == 403
        assert store.change_password(password, "a much stronger password")
        store.accept_disclaimer()  # PATs stay inert until the disclaimer is accepted too
        write_pat = store.create_token("rw", "read/write", None)[1]
        read_pat = store.create_token("ro", "read", None)[1]
        for method, url, body in (("PUT", "/admin/api/settings", change),
                                  ("PUT", "/admin/api/parameters", {"exposed_names": [], "write_names": []}),
                                  ("POST", "/admin/api/tokens", {"name": "x", "role": "read"})):
            assert (await client.request(method, url, json=body)).status_code == 401
            assert (await client.request(method, url, json=body, headers=bearer(read_pat))).status_code == 403
            invalid = await client.request(method, url, json=body, headers=bearer("pat_" + "A" * 46))
            # Session-only endpoints refuse any PAT before looking at it.
            assert invalid.status_code == (403 if url.endswith("/tokens") else 401)
        assert (await client.get("/admin/api/settings")).status_code == 401
        # Every administration endpoint needs the read/write role, reads included.
        assert (await client.get("/admin/api/settings", headers=bearer(read_pat))).status_code == 403
        assert (await client.get("/admin/api/tokens", headers=bearer(read_pat))).status_code == 403
        done = await client.put("/admin/api/settings", json=change, headers=bearer(write_pat))
        assert done.status_code == 200 and done.json()["settings"]["metrics_rate_limit_requests"] == 120
        # Token endpoints and trust/authentication settings (read or write) are session-only; a PAT
        # may read the other settings but never sees those values.
        assert (await client.get("/admin/api/tokens", headers=bearer(write_pat))).status_code == 403
        seen = (await client.get("/admin/api/settings", headers=bearer(write_pat))).json()["settings"]
        assert "metrics_rate_limit_requests" in seen
        assert not ({"docs_public", "auth_required", "trusted_proxies", "bind_address", "behind_reverse_proxy",
                     "forwarded_header", "metrics_require_token", "metrics_trusted_sources"} & set(seen))
        # Retention stays readable for a PAT; only writing it is session-only.
        assert "questdb_retention_days" in seen
        bad_id = await client.put("/admin/api/settings", json={"devices": [{"host": "192.0.2.5", "device_id": ["x"]}]},
                                  headers=bearer(write_pat))
        assert bad_id.status_code == 400  # a non-text device id is a client error, not a 500
        made = await client.post("/admin/api/tokens", json={"name": "x", "role": "read"}, headers=bearer(write_pat))
        assert made.status_code == 403
        for key, value in (
            ("auth_required", not auth_required), ("trusted_proxies", ["10.0.0.0/8"]),
            ("enable_write_support", True), ("docs_public", True), ("devices", [{"host": "192.0.2.99", "port": 8899}]),
            ("influxdb_hostname", "attacker.example"), ("questdb_hostname", "attacker.example"),
            ("influxdb_token", "stolen"), ("questdb_password", "stolen"), ("db_type", "questdb"),
            ("bind_port", 9999), ("log_level", "DEBUG"),
            ("questdb_retention_days", 1), ("questdb_raw_retention_days", 1),
        ):
            denied = await client.put("/admin/api/settings", json={key: value}, headers=bearer(write_pat))
            assert denied.status_code == 403, key
        # Echoing the current value of a protected field is not a change.
        echo = await client.put("/admin/api/settings", json={"auth_required": auth_required, "docs_public": False},
                                headers=bearer(write_pat))
        assert echo.status_code == 200
        # Widening the write allowlist needs the session too.
        widened = await client.put(
            "/admin/api/parameters", headers=bearer(write_pat),
            json={"exposed_names": [], "write_names": ["power_mng_soc_target_set"]},
        )
        assert widened.status_code == 403


def test_admin_password_minimum_is_eight_characters():
    from app.admin.api import PasswordChange

    assert PasswordChange(current_password="x", new_password="12345678")
    with pytest.raises(ValidationError):
        PasswordChange(current_password="x", new_password="1234567")


def test_first_start_banner_prints_the_one_time_password():
    from app.api.server import first_start_banner
    from app.config import Settings

    text = first_start_banner(Settings(_env_file=None, bind_port=8123), "XnQ3!4wk")
    assert "XnQ3!4wk" in text  # the one-time password is shown directly on the console now
    assert "FIRST START" in text and "Admin user:" in text and "change this password" in text
    assert "http://127.0.0.1:8123/" in text
    assert text.startswith("\n=") and text.endswith("=\n")


@pytest.mark.asyncio
async def test_server_prints_the_banner_before_startup_only_on_first_start(tmp_path, capsys):
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
    assert "FIRST START" in first and f"http://127.0.0.1:{port}/" in first
    second_sock = socket.socket()
    second_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    second_sock.bind(("127.0.0.1", port))
    assert "FIRST START" not in await run_once(second_sock)


@pytest.mark.asyncio
async def test_bootstrap_password_is_printed_even_when_uvicorn_startup_fails(tmp_path, capsys):
    """P2-1: a failing first start (e.g. socket bind) must still emit the one-time password.

    The admin row and its hash are committed by create_app before the server runs, so printing the
    plaintext only after a successful startup would lose it forever. The banner is emitted at the
    start of serve(), before super().serve() binds the socket.
    """
    import asyncio

    import uvicorn

    from app.api.server import GracefulServer

    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    password = app.state.first_start_password
    assert password

    async def failing_startup(*args, **kwargs):
        raise OSError("simulated bind failure")

    server = GracefulServer(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None), app.state.runtime)
    server.install_signal_handlers = lambda: None
    server.startup = failing_startup  # type: ignore[method-assign]
    with pytest.raises(OSError, match="simulated bind failure"):
        await server.serve()
    out = capsys.readouterr().out
    assert "FIRST START" in out and password in out


@pytest.mark.asyncio
async def test_admin_api_errors_keep_their_own_detail(tmp_path):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        headers = {"X-CSRF-Token": changed.json()["csrf_token"]}
        assert (await client.post("/admin/api/accept-disclaimer", headers=headers)).status_code == 200
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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": password})
        changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                                    json={"current_password": password, "new_password": "a much stronger password"})
        headers = {"X-CSRF-Token": changed.json()["csrf_token"]}
        assert (await client.post("/admin/api/accept-disclaimer", headers=headers)).status_code == 200

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


@pytest.mark.asyncio
async def test_a_busy_transition_rejects_a_live_settings_change_before_persisting(tmp_path):
    """Finding C invariant: when a live transition is already running, a transition-bearing settings
    PUT is refused with 409 under _LIVE_CHANGE_LOCK BEFORE anything is persisted -- the desired
    settings must be unchanged, so no diverged saved-vs-active state can arise. 'Busy' is simulated
    deterministically by forcing _transition_running True rather than racing real timings."""
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
        assert (await client.post("/admin/api/accept-disclaimer", headers=headers)).status_code == 200

        before = app.state.admin_desired_settings
        assert before.enable_write_support is False
        with patch("app.admin.api._transition_running", return_value=True):
            busy = await client.put("/admin/api/settings", headers=headers, json={"enable_write_support": True})
        assert busy.status_code == 409
        # Nothing was persisted or applied: the desired-state object is the same, writes still off.
        assert app.state.admin_desired_settings is before
        assert app.state.admin_desired_settings.enable_write_support is False


@pytest.mark.asyncio
async def test_a_busy_transition_rejects_a_parameter_selection_change_before_persisting(tmp_path):
    """Finding C invariant for the /parameters selection-changed path: a busy transition refuses the
    change with 409 under _LIVE_CHANGE_LOCK before the exposed-names selection is persisted."""
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
        assert (await client.post("/admin/api/accept-disclaimer", headers=headers)).status_code == 200

        store = app.state.admin_store
        exposed_before = store.get("exposed_names")
        # A selection that differs from the current one, chosen from the catalog's numeric entries.
        from app.catalog.base import is_numeric

        numeric = [entry.name for entry in app.state.runtime.catalog.entries() if is_numeric(entry.value_type)]
        new_selection = numeric[:1]
        assert new_selection and new_selection != (exposed_before or [])
        with patch("app.admin.api._transition_running", return_value=True):
            busy = await client.put("/admin/api/parameters", headers=headers,
                                    json={"exposed_names": new_selection, "write_names": []})
        assert busy.status_code == 409
        # The exposed-names selection was not persisted.
        assert store.get("exposed_names") == exposed_before


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


def _fake_request(peer: str, forwarded_proto: str | None, scheme: str = "http", behind_proxy: bool = False):
    from types import SimpleNamespace

    from app.security.client_ip import ClientIpResolver

    resolver = ClientIpResolver([ip_network("127.0.0.0/8"), ip_network("fd00::/8")], "x-forwarded-for")
    headers = {"x-forwarded-proto": forwarded_proto} if forwarded_proto else {}
    state = SimpleNamespace(
        security=SimpleNamespace(client_ip=resolver),
        runtime=SimpleNamespace(settings=SimpleNamespace(behind_reverse_proxy=behind_proxy)),
    )
    return SimpleNamespace(
        client=SimpleNamespace(host=peer), headers=headers, url=SimpleNamespace(scheme=scheme),
        app=SimpleNamespace(state=state),
    )


@pytest.mark.parametrize(("peer", "proto", "expected"), [
    ("127.0.0.1", "https", "https"),     # trusted proxy: the browser scheme counts
    ("fd00::5", "https, http", "https"),  # IPv6 trusted proxy, first hop wins
    ("192.0.2.9", "https", "http"),       # untrusted peer cannot claim https
    ("2001:db8::1", "https", "http"),
    ("127.0.0.1", "gopher", "http"),      # unknown value ignored
    ("127.0.0.1", None, "http"),
])
def test_public_scheme_trusts_forwarded_proto_only_from_a_trusted_proxy(peer, proto, expected):
    from app.admin.api import _public_scheme, _secure_cookies

    request = _fake_request(peer, proto)
    assert _public_scheme(request) == expected
    assert _secure_cookies(request) is (expected == "https")


def test_secure_cookies_follow_behind_reverse_proxy_even_for_an_untrusted_peer():
    from app.admin.api import _secure_cookies

    assert _secure_cookies(_fake_request("192.0.2.9", None, behind_proxy=True)) is True


def test_stored_display_name_that_is_too_long_or_has_control_characters_still_loads():
    from app.config import DeviceEntry

    entry = DeviceEntry(device_id="main", host="10.0.0.1", display_name="x" * 100)
    assert entry.display_name == "x" * 64
    assert DeviceEntry(device_id="main", host="10.0.0.1", display_name="a\x00b\nc").display_name == "a b c"


def test_admin_input_for_a_display_name_is_still_rejected_strictly():
    from fastapi import HTTPException

    from app.admin.api import _display_name

    assert _display_name("") is None and _display_name("Garage") == "Garage"
    for bad in ("x" * 65, "a\x00b"):
        with pytest.raises(HTTPException) as excinfo:
            _display_name(bad)
        assert excinfo.value.status_code == 400


@pytest.mark.parametrize(("behavior", "status", "rollback"), [
    ("raise", 500, False),    # failure after the teardown began: the live graph is unknown
    ("hang", 504, False),     # still running: neither success nor rollback can be claimed
    ("build", 409, True),     # nothing torn down: the old list must be restored
])
def test_reconfigure_failures_are_reported_and_only_a_build_failure_rolls_back(behavior, status, rollback):
    import asyncio
    import threading
    from types import SimpleNamespace
    from unittest.mock import patch

    from fastapi import HTTPException

    from app.admin import api
    from app.errors import ReconfigurationBuildError

    async def reconfigure():
        if behavior == "raise":
            raise RuntimeError("boom")
        if behavior == "build":
            raise ReconfigurationBuildError("bad address")
        await asyncio.sleep(5)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    state = SimpleNamespace(reconfigure_devices=reconfigure, loop=loop, admin_store=None)
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    sentinel = Settings(_env_file=None, hmac_secret="s" * 48)
    try:
        with patch.object(api, "_RECONFIGURE_TIMEOUT_SECONDS", 0.05), pytest.raises(HTTPException) as excinfo:
            api._reconfigure_devices(request, sentinel, sentinel)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
    assert excinfo.value.status_code == status
    assert excinfo.value.rollback is rollback


def test_reconfigure_timeout_defers_rollback_when_the_running_coroutine_fails_rollback_capable():
    """A reconfiguration that outlives its HTTP wait (504) and then fails in the pre-teardown phase
    must still revert the saved device list, with no caller left to do it (finding 3)."""
    import asyncio
    import threading
    from types import SimpleNamespace
    from unittest.mock import patch

    from fastapi import HTTPException

    from app.admin import api
    from app.errors import ReconfigurationBuildError

    async def reconfigure():
        # Outlive the 0.05 s HTTP wait, then fail rollback-capable (old graph still live).
        await asyncio.sleep(0.2)
        raise ReconfigurationBuildError("bad address")

    reverted: list[tuple] = []
    done = threading.Event()

    def fake_revert(state, store, updated, previous):
        reverted.append((updated, previous))
        done.set()

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    state = SimpleNamespace(reconfigure_devices=reconfigure, loop=loop, admin_store=object())
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    updated = Settings(_env_file=None, hmac_secret="s" * 48)
    previous = Settings(_env_file=None, hmac_secret="s" * 48)
    try:
        with (
            patch.object(api, "_RECONFIGURE_TIMEOUT_SECONDS", 0.05),
            patch.object(api, "_revert_devices_on_state", fake_revert),
        ):
            with pytest.raises(HTTPException) as excinfo:
                api._reconfigure_devices(request, updated, previous)
            assert excinfo.value.status_code == 504
            # Still inside the patch so the deferred executor runs the fake: the coroutine fails
            # ~0.2 s from now, the done-callback reverts, then this unblocks.
            assert done.wait(2), "deferred rollback did not run after the timed-out reconfiguration failed"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
    assert reverted == [(updated, previous)]


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 50_001.0])
def test_dispatch_body_rejects_an_unbounded_max_power(value):
    from datetime import UTC, datetime

    from app.api.routers.dispatch import DispatchBody

    with pytest.raises(ValidationError):
        DispatchBody(mode="charge_from_grid", target_soc_percent=80, max_power_w=value,
                     valid_until=datetime(2030, 1, 1, tzinfo=UTC))


def test_enabling_the_metrics_endpoint_is_session_only_where_it_would_serve_without_a_token():
    from types import SimpleNamespace

    from app.admin.api import _changed_session_only
    from app.config import Settings

    def request(**settings):
        desired = Settings(_env_file=None, hmac_secret="s" * 48, enable_metrics_endpoint=False, **settings)
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(admin_desired_settings=desired)))

    on = {"enable_metrics_endpoint": True}
    assert _changed_session_only(request(), on) == set()  # token still required: not a privilege change
    assert _changed_session_only(request(metrics_require_token=False), on) == {"enable_metrics_endpoint"}
    assert _changed_session_only(request(metrics_trusted_sources=["10.0.0.0/8"]), on) == {"enable_metrics_endpoint"}
    assert _changed_session_only(request(), {**on, "metrics_require_token": False}) >= {
        "enable_metrics_endpoint", "metrics_require_token",
    }
    assert _changed_session_only(request(metrics_require_token=False), {"enable_metrics_endpoint": False}) == set()


# --- Mandatory usage-disclaimer HTTP enforcement -----------------------------------------------

_DISCLAIMER_PW = "a much stronger password"


def _api_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                             headers={"Origin": "http://testserver"})


async def _changed_not_accepted(client, password):
    """Log in and change the initial password WITHOUT accepting the disclaimer; return the CSRF."""
    csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
    login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                              json={"username": "admin", "password": password})
    changed = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": login.json()["csrf_token"]},
                                json={"current_password": password, "new_password": _DISCLAIMER_PW})
    assert changed.status_code == 200
    body = changed.json()
    assert body["must_change_password"] is False and body["disclaimer_required"] is True
    return changed.json()["csrf_token"]


@pytest.mark.asyncio
async def test_disclaimer_gate_blocks_normal_admin_api_before_acceptance(tmp_path):
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"))
    password = app.state.first_start_password
    async with _api_client(app) as client:
        csrf = await _changed_not_accepted(client, password)
        h = {"X-CSRF-Token": csrf}
        assert (await client.get("/admin/api/settings")).status_code == 403
        assert (await client.get("/admin/api/tokens")).status_code == 403
        assert (await client.put("/admin/api/settings", headers=h, json={"docs_public": True})).status_code == 403
        assert (await client.post("/admin/api/tokens", headers=h,
                                  json={"name": "x", "role": "read"})).status_code == 403


@pytest.mark.asyncio
async def test_accept_endpoint_needs_a_session_and_valid_csrf(tmp_path):
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"))
    password = app.state.first_start_password
    async with _api_client(app) as client:
        csrf = await _changed_not_accepted(client, password)
        # No CSRF token -> refused (mutation).
        assert (await client.post("/admin/api/accept-disclaimer")).status_code == 403
        # Wrong CSRF token -> refused.
        assert (await client.post("/admin/api/accept-disclaimer",
                                  headers={"X-CSRF-Token": "wrong"})).status_code == 403
        good = await client.post("/admin/api/accept-disclaimer", headers={"X-CSRF-Token": csrf})
        assert good.status_code == 200
        assert good.json()["accepted"] is True
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", good.json()["accepted_at"])
        # Idempotent: a second accept keeps the same timestamp and the state stays unlocked.
        again = await client.post("/admin/api/accept-disclaimer", headers={"X-CSRF-Token": csrf})
        assert again.status_code == 200 and again.json()["accepted_at"] == good.json()["accepted_at"]


@pytest.mark.asyncio
async def test_accept_endpoint_requires_the_authenticated_state(tmp_path):
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"))
    async with _api_client(app) as client:
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        # No session at all -> login required.
        assert (await client.post("/admin/api/accept-disclaimer",
                                  headers={"X-CSRF-Token": csrf})).status_code == 401


@pytest.mark.asyncio
async def test_session_endpoint_reflects_acceptance_and_the_modal_does_not_reappear(tmp_path):
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"))
    password = app.state.first_start_password
    async with _api_client(app) as client:
        csrf = await _changed_not_accepted(client, password)
        state = (await client.get("/admin/api/session")).json()
        assert state["disclaimer_required"] is True and state["disclaimer_accepted_at"] is None
        accepted = await client.post("/admin/api/accept-disclaimer", headers={"X-CSRF-Token": csrf})
        stamp = accepted.json()["accepted_at"]
        state = (await client.get("/admin/api/session")).json()
        assert state["disclaimer_required"] is False and state["disclaimer_accepted_at"] == stamp
        # A later log out + log in must not bring the modal back for this user.
        assert (await client.post("/admin/api/logout", headers={"X-CSRF-Token": csrf})).status_code == 200
        relogin_csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        await client.post("/admin/api/login", headers={"X-CSRF-Token": relogin_csrf},
                          json={"username": "admin", "password": _DISCLAIMER_PW})
        state = (await client.get("/admin/api/session")).json()
        assert state["disclaimer_required"] is False and state["disclaimer_accepted_at"] == stamp


@pytest.mark.asyncio
async def test_pat_cannot_bypass_the_disclaimer(tmp_path):
    app = create_app(Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db"))
    store = app.state.admin_store
    assert store.change_password(app.state.first_start_password, _DISCLAIMER_PW)  # disclaimer still pending
    write_pat = store.create_token("rw", "read/write", None)[1]
    bearer = {"Authorization": "Bearer " + write_pat}
    async with _api_client(app) as client:
        # A valid read/write PAT is refused for every normal admin call while unaccepted...
        assert (await client.get("/admin/api/settings", headers=bearer)).status_code == 403
        assert (await client.put("/admin/api/settings", headers=bearer,
                                 json={"metrics_rate_limit_requests": 120})).status_code == 403
        # ...and a PAT may not reach the acceptance endpoint at all: the flow endpoints are
        # session-only, so the PAT header is ignored and a session is demanded (401, never 200).
        assert (await client.post("/admin/api/accept-disclaimer", headers=bearer)).status_code in (401, 403)
        assert store.disclaimer_required() is True  # nothing was accepted via the PAT path
