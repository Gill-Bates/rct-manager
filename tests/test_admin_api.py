#!/usr/bin/env python3
#
# tests/test_admin_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Admin bootstrap, browser session, PAT and encrypted persistence behavior."""

import re
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
    banner = first_start_banner(app.state.runtime.settings, tmp_path / "initial-admin-password")
    assert app.state.first_start_password and "token" not in banner.lower()
    assert app.state.first_start_password not in banner  # the password itself must never reach stdout
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


def test_first_start_banner_names_the_password_file_and_never_the_password(tmp_path):
    from app.api.server import first_start_banner
    from app.config import Settings

    password_file = tmp_path / "initial-admin-password"
    text = first_start_banner(Settings(_env_file=None, bind_port=8123), password_file)
    assert "XnQ3!4wk" not in text  # the password itself must never be printed to stdout
    assert str(password_file) in text and "http://127.0.0.1:8123/" in text
    assert text.startswith("\n=") and text.endswith("=\n")
    assert re.search(r"Password file:\s+(\S+)", text).group(1) == str(password_file)


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
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(reconfigure_devices=reconfigure, loop=loop)))
    try:
        with patch.object(api, "_RECONFIGURE_TIMEOUT_SECONDS", 0.05), pytest.raises(HTTPException) as excinfo:
            api._reconfigure_devices(request)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
    assert excinfo.value.status_code == status
    assert excinfo.value.rollback is rollback


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
