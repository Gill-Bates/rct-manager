#!/usr/bin/env python3
#
# tests/test_admin_security.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Security and persistence regressions of the administration backend."""

import re
import stat
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.admin.store import AdminStore
from app.api.app_factory import create_app
from app.config import Settings

PASSWORD2 = "a much stronger password"


def _settings(tmp_path, **extra):
    return Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db", **extra)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _login(client, password, csrf=None, **headers):
    if csrf is None:
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
    return await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf, **headers},
                             json={"username": "admin", "password": password})


@asynccontextmanager
async def _ready(app, password):
    """Log in, change the initial password and yield a client with a usable session."""
    async with _client(app) as client:
        login = await _login(client, password)
        changed = await client.post("/admin/api/change-password",
                                    headers={"X-CSRF-Token": login.json()["csrf_token"]},
                                    json={"current_password": password, "new_password": PASSWORD2})
        assert changed.status_code == 200
        yield client, changed.json()["csrf_token"]


@pytest.fixture
def booted(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    password = app.state.first_start_password
    return settings, app, password


@pytest.mark.asyncio
async def test_forced_password_change_is_enforced_by_the_server(booted):
    _, app, password = booted
    async with _client(app) as client:
        login = await _login(client, password)
        csrf = login.json()["csrf_token"]
        assert login.json()["must_change_password"] is True
        assert (await client.get("/admin/api/settings")).status_code == 403
        assert (await client.get("/admin/api/tokens")).status_code == 403
        assert (await client.put("/admin/api/settings", headers={"X-CSRF-Token": csrf},
                                 json={"docs_public": True})).status_code == 403
        assert (await client.post("/admin/api/tokens", headers={"X-CSRF-Token": csrf},
                                  json={"name": "x", "role": "read"})).status_code == 403
        page = await client.get("/ui/dashboard")
        assert page.status_code == 303 and page.headers["location"] == "/change-password"


@pytest.mark.asyncio
async def test_sessions_rotate_and_old_ones_die(booted):
    _, app, password = booted
    async with _client(app) as client:
        first = await _login(client, password)
        old_cookie = client.cookies["rct_admin_session"]
        changed = await client.post("/admin/api/change-password",
                                    headers={"X-CSRF-Token": first.json()["csrf_token"]},
                                    json={"current_password": password, "new_password": PASSWORD2})
        assert changed.status_code == 200
        assert client.cookies["rct_admin_session"] != old_cookie
        assert (await client.get("/admin/api/settings")).status_code == 200
        async with _client(app) as stale:
            stale.cookies.set("rct_admin_session", old_cookie)
            assert (await stale.get("/admin/api/settings")).status_code == 401
        # the initial password stops working once it was changed
        async with _client(app) as other:
            assert (await _login(other, password)).status_code == 401
            assert (await _login(other, PASSWORD2)).status_code == 200
        csrf = changed.json()["csrf_token"]
        live_cookie = client.cookies["rct_admin_session"]
        assert (await client.post("/admin/api/logout", headers={"X-CSRF-Token": csrf})).status_code == 200
        async with _client(app) as stale:
            stale.cookies.set("rct_admin_session", live_cookie)
            assert (await stale.get("/admin/api/settings")).status_code == 401


@pytest.mark.asyncio
async def test_csrf_and_origin_are_enforced(booted):
    _, app, password = booted
    async with _ready(app, password) as (client, csrf):
        body = {"docs_public": True}
        assert (await client.put("/admin/api/settings", json=body)).status_code == 403
        assert (await client.put("/admin/api/settings", headers={"X-CSRF-Token": "wrong"}, json=body)).status_code == 403
        evil = {"X-CSRF-Token": csrf, "Origin": "http://evil.example"}
        assert (await client.put("/admin/api/settings", headers=evil, json=body)).status_code == 403
        referer = {"X-CSRF-Token": csrf, "Referer": "http://evil.example/page"}
        assert (await client.put("/admin/api/settings", headers=referer, json=body)).status_code == 403
        good = {"X-CSRF-Token": csrf, "Origin": "http://testserver"}
        assert (await client.put("/admin/api/settings", headers=good, json=body)).status_code == 200
        assert (await client.delete("/admin/api/tokens/nope", headers=evil)).status_code == 403


@pytest.mark.asyncio
async def test_login_and_password_change_guesses_are_throttled(booted):
    _, app, password = booted
    async with _client(app) as client:
        codes = [(await _login(client, "wrong-password")).status_code for _ in range(8)]
        assert codes[:5] == [401] * 5 and codes[-1] == 429
        assert (await _login(client, password)).status_code == 429  # even the right one is blocked
    fresh = create_app(_settings(booted[0].admin_db_path.parent))
    async with _client(fresh) as client:
        login = await _login(client, password)
        csrf = login.json()["csrf_token"]
        codes = []
        for _ in range(8):
            r = await client.post("/admin/api/change-password", headers={"X-CSRF-Token": csrf},
                                  json={"current_password": "wrong-password", "new_password": PASSWORD2})
            codes.append(r.status_code)
        assert codes[:5] == [401] * 5 and codes[-1] == 429


@pytest.mark.asyncio
async def test_pat_mutations_are_rate_limited(tmp_path):
    app = create_app(_settings(tmp_path, rate_limit_requests=6))
    password = app.state.first_start_password
    async with _ready(app, password) as (client, csrf):
        codes = []
        for _ in range(10):
            r = await client.post("/admin/api/tokens", headers={"X-CSRF-Token": csrf},
                                  json={"name": "t", "role": "read"})
            codes.append(r.status_code)
        assert 429 in codes


@pytest.mark.asyncio
async def test_token_expiry_revocation_and_restart(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    password = app.state.first_start_password
    async with _ready(app, password) as (client, csrf):
        h = {"X-CSRF-Token": csrf}
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        assert (await client.post("/admin/api/tokens", headers=h,
                                  json={"name": "p", "role": "read", "expires_at": past})).status_code == 400
        naive = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
        kept = await client.post("/admin/api/tokens", headers=h,
                                 json={"name": "naive", "role": "read", "expires_at": naive})
        assert kept.status_code == 201
        gone = await client.post("/admin/api/tokens", headers=h, json={"name": "gone", "role": "read"})
        assert (await client.delete("/admin/api/tokens/" + gone.json()["id"], headers=h)).status_code == 200
    store = AdminStore(settings.admin_db_path, "s" * 48)
    try:
        assert store.authenticate_token(kept.json()["token"]) is not None
        assert store.authenticate_token(gone.json()["token"]) is None
        restarted = create_app(settings)
        assert restarted.state.admin_store.authenticate_token(kept.json()["token"]) is not None
        assert restarted.state.admin_store.authenticate_token(gone.json()["token"]) is None
        ids = {t["id"] for t in store.list_tokens()}
        assert kept.json()["id"] in ids and gone.json()["id"] not in ids
    finally:
        store.close()


def test_expired_token_is_rejected(tmp_path):
    store = AdminStore(tmp_path / "rct.db", "x" * 48)
    store.initialize()
    record, token = store.create_token("short", "read", datetime.now(UTC) + timedelta(seconds=1))
    assert store.authenticate_token(token) is not None
    import time

    time.sleep(1.2)
    assert store.authenticate_token(token) is None
    assert record["id"]


def _rows(path, table):
    import sqlite3

    db = sqlite3.connect(path)
    try:
        return db.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        db.close()


def test_swapped_and_tampered_ciphertexts_fail_closed(tmp_path):
    import sqlite3

    path = tmp_path / "rct.db"
    store = AdminStore(path, "x" * 48)
    store.initialize()
    store.put("a", 1)
    store.put("b", 2)
    db = sqlite3.connect(path)
    ea = db.execute("SELECT encrypted FROM settings WHERE key='a'").fetchone()[0]
    eb = db.execute("SELECT encrypted FROM settings WHERE key='b'").fetchone()[0]
    db.execute("UPDATE settings SET encrypted=? WHERE key='a'", (eb,))
    db.execute("UPDATE settings SET encrypted=? WHERE key='b'", (ea,))
    db.commit()
    with pytest.raises(ValueError, match="integrity"):
        store.get("a")
    db.execute("UPDATE settings SET encrypted=? WHERE key='b'", (ea[:-4] + b"AAAA",))
    db.commit()
    db.close()
    with pytest.raises(ValueError, match="HMAC_SECRET"):
        store.get("b")
    with pytest.raises(ValueError, match="HMAC_SECRET"):
        AdminStore(path, "z" * 48).get("a")


def test_swapped_session_rows_never_authenticate(tmp_path):
    import sqlite3

    store = AdminStore(tmp_path / "rct.db", "x" * 48)
    store.initialize()
    (t1, _), (t2, _) = store.new_session(), store.new_session()
    assert store.session(t1) and store.session(t2)
    db = sqlite3.connect(store.path)
    rows = db.execute("SELECT digest, encrypted FROM sessions").fetchall()
    (d1, e1), (d2, e2) = rows
    db.execute("UPDATE sessions SET encrypted=? WHERE digest=?", (e2, d1))
    db.execute("UPDATE sessions SET encrypted=? WHERE digest=?", (e1, d2))
    db.commit()
    db.close()
    assert store.session(t1) is None and store.session(t2) is None


def test_expired_sessions_are_purged(tmp_path, monkeypatch):
    import time as time_module

    store = AdminStore(tmp_path / "rct.db", "x" * 48)
    store.initialize()
    store.new_session()
    real = time_module.time()
    monkeypatch.setattr("app.admin.store.time.time", lambda: real + 13 * 3600)
    store.new_session()
    assert len(_rows(store.path, "sessions")) == 1


def test_database_and_directory_permissions(tmp_path):
    path = tmp_path / "data" / "rct.db"
    path.parent.mkdir()
    path.touch()
    path.chmod(0o644)
    store = AdminStore(path, "x" * 48)
    store.initialize()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    nested = AdminStore(tmp_path / "new" / "deep" / "rct.db", "x" * 48)
    nested.initialize()
    assert stat.S_IMODE(nested.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(nested.path.stat().st_mode) == 0o600


def test_hmac_secret_bootstrap_permissions(tmp_path, monkeypatch, caplog):
    from app.__main__ import _ensure_hmac_secret

    monkeypatch.delenv("HMAC_SECRET", raising=False)
    env = tmp_path / "settings.env"
    env.write_text("LOG_LEVEL=info\n")
    env.chmod(0o644)
    _ensure_hmac_secret(env)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert re.search(r"^HMAC_SECRET=[A-Za-z0-9+/]{43}=$", env.read_text(), re.MULTILINE)
    env.chmod(0o640)
    with caplog.at_level("WARNING", logger="app"):
        _ensure_hmac_secret(env)
    # P3-3: an already-existing secret file with loose permissions is repaired to 0600, not just
    # warned about, using the same O_NOFOLLOW/FD-based check as creation.
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "permissions corrected" in caplog.text
    link = tmp_path / "link.env"
    link.symlink_to(tmp_path / "target.env")
    from app.errors import ConfigError

    with pytest.raises(ConfigError):
        _ensure_hmac_secret(link)


def test_existing_secret_file_behind_a_symlink_is_not_chmodded_through_it(tmp_path, caplog):
    """A symlink swapped in for the secret file must not have its target's permissions changed."""
    from app.__main__ import _ensure_hmac_secret

    target = tmp_path / "target.env"
    target.write_text("HMAC_SECRET=" + "x" * 44 + "\n")
    target.chmod(0o644)
    link = tmp_path / "settings.env"
    link.symlink_to(target)
    with caplog.at_level("WARNING", logger="app"):
        _ensure_hmac_secret(link)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644  # untouched: the open() refused to follow the link
    assert "could not be reopened to fix it" in caplog.text


@pytest.mark.asyncio
async def test_settings_report_live_versus_restart_required(booted):
    _, app, password = booted
    async with _ready(app, password) as (client, csrf):
        h = {"X-CSRF-Token": csrf}
        assert (await client.get("/admin/api/settings")).json()["restart_required"] == []
        live = await client.put("/admin/api/settings", headers=h, json={"docs_public": True})
        assert live.json()["restart_required"] == [] and "docs_public" in live.json()["live"]
        assert app.state.runtime.settings.docs_public is True
        slow = await client.put("/admin/api/settings", headers=h, json={"log_level": "DEBUG"})
        assert slow.json()["restart_required"] == ["log_level"]
        assert app.state.runtime.settings.log_level != "DEBUG"
        again = (await client.get("/admin/api/settings")).json()
        assert again["restart_required"] == ["log_level"] and again["settings"]["log_level"] == "DEBUG"
        bad = await client.put("/admin/api/settings", headers=h, json={"bind_port": 70000})
        assert bad.status_code == 400
        assert (await client.get("/admin/api/settings")).json()["settings"]["bind_port"] != 70000


@pytest.mark.asyncio
async def test_concurrent_autosaves_of_different_fields_are_kept(booted):
    _, app, password = booted
    async with _ready(app, password) as (client, csrf):
        h = {"X-CSRF-Token": csrf}
        import asyncio

        results = await asyncio.gather(*(
            client.put("/admin/api/settings", headers=h, json=body) for body in (
                {"docs_public": True}, {"log_level": "DEBUG"}, {"enable_metrics_endpoint": False},
                {"behind_reverse_proxy": False}, {"auth_required": True})))
        assert [r.status_code for r in results] == [200] * 5
        view = (await client.get("/admin/api/settings")).json()["settings"]
        assert view["docs_public"] is True and view["log_level"] == "DEBUG"
        assert view["enable_metrics_endpoint"] is False
    saved = app.state.admin_store.get("operator_settings")
    assert saved["docs_public"] is True and saved["log_level"] == "DEBUG"
    assert app.state.admin_desired_settings.log_level == "DEBUG"


@pytest.mark.asyncio
async def test_parameter_selection_limits_and_restart_reporting(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    password = app.state.first_start_password
    async with _ready(app, password) as (client, csrf):
        h = {"X-CSRF-Token": csrf}
        view = (await client.get("/admin/api/parameters")).json()
        assert view["restart_required"] == []
        numeric = [p["name"] for p in view["available"] if p["exportable"]]
        too_many = await client.put("/admin/api/parameters", headers=h,
                                    json={"exposed_names": numeric[:65], "write_names": []})
        assert too_many.status_code == 422
        exactly = await client.put("/admin/api/parameters", headers=h,
                                   json={"exposed_names": numeric[:64], "write_names": []})
        assert exactly.status_code == 200 and exactly.json()["restart_required"] == ["exposed_names"]
        dup = await client.put("/admin/api/parameters", headers=h,
                               json={"exposed_names": [numeric[0], numeric[0]], "write_names": []})
        assert dup.status_code == 400
        unknown = await client.put("/admin/api/parameters", headers=h,
                                   json={"exposed_names": ["nope"], "write_names": []})
        assert unknown.status_code == 400
        writable = [p["name"] for p in view["available"] if p["writable"]]
        denied = await client.put("/admin/api/parameters", headers=h,
                                  json={"exposed_names": [], "write_names": ["not_writable_metric"]})
        assert denied.status_code == 400
        if writable:
            ok = await client.put("/admin/api/parameters", headers=h,
                                  json={"exposed_names": [], "write_names": writable[:1]})
            assert ok.status_code == 200
            assert app.state.admin_store.get("write_names") == writable[:1]
        empty = await client.put("/admin/api/parameters", headers=h, json={"exposed_names": [], "write_names": []})
        assert empty.json()["exposed_names"] == []
    restarted = create_app(settings)
    assert restarted.state.runtime.exporter._exposed == []
    assert restarted.state.admin_store.get("exposed_names") == []
    assert restarted.state.admin_store.get("write_names") == []
    async with _client(restarted) as client:
        assert (await _login(client, PASSWORD2)).status_code == 200
        assert (await client.get("/admin/api/parameters")).json()["restart_required"] == []


def test_concurrent_store_writes_do_not_lose_updates(tmp_path):
    store = AdminStore(tmp_path / "rct.db", "x" * 48)
    store.initialize()
    from app.config import Settings

    current = Settings(_env_file=None, hmac_secret="x" * 48)
    errors: list[BaseException] = []

    def work(change):
        try:
            store.merge_operator_settings(current, change)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    changes = [{"docs_public": True}, {"log_level": "DEBUG"}, {"enable_metrics_endpoint": False}, {"bind_port": 9000}]
    threads = [threading.Thread(target=work, args=(c,)) for c in changes]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    saved = store.get("operator_settings")
    assert all(saved[k] == v for c in changes for k, v in c.items())


def test_legacy_admin_secret_is_used_with_deprecation_warning(tmp_path, monkeypatch, caplog):
    from app.__main__ import _ensure_hmac_secret
    from app.config import load_settings

    env = tmp_path / "settings.env"
    env.write_text("ADMIN_SECRET=" + "o" * 44 + "\n")
    env.chmod(0o600)
    _ensure_hmac_secret(env)
    assert env.read_text() == "ADMIN_SECRET=" + "o" * 44 + "\n"  # no new key next to the old one
    with caplog.at_level("WARNING", logger="app.config"):
        settings = load_settings(env)
    assert settings.hmac_secret.get_secret_value() == "o" * 44
    assert "ADMIN_SECRET is deprecated, rename it to HMAC_SECRET" in caplog.text
    assert "o" * 44 not in str(settings.effective())


def _wait_idle(store):
    store._touch_pool.submit(lambda: None).result(timeout=5)  # the pool has one worker: earlier writes are done


def test_last_used_is_recorded_throttled_and_never_for_invalid_tokens(tmp_path):
    from datetime import datetime

    store = AdminStore(tmp_path / "rct.db", "k" * 44)
    store.initialize()
    _record, token = store.create_token("probe", "read", None)
    assert store.list_tokens()[0]["last_used_at"] is None
    assert store.authenticate_token("pat_" + "A" * 46) is None
    _wait_idle(store)
    assert store.list_tokens()[0]["last_used_at"] is None
    assert store.authenticate_token(token) is not None
    _wait_idle(store)
    first = store.list_tokens()[0]["last_used_at"]
    assert datetime.fromisoformat(first).tzinfo is not None
    assert store.authenticate_token(token) is not None
    _wait_idle(store)
    assert store.list_tokens()[0]["last_used_at"] == first  # throttled: no second write within the interval
    fresh = AdminStore(tmp_path / "rct.db", "k" * 44)  # a restart keeps the throttle through the stored value
    assert fresh.authenticate_token(token) is not None
    _wait_idle(fresh)
    assert fresh.list_tokens()[0]["last_used_at"] == first


def test_record_without_last_used_field_lists_as_never(tmp_path):
    store = AdminStore(tmp_path / "rct.db", "k" * 44)
    store.initialize()
    record, _token = store.create_token("old", "read", None)
    with store.connect() as db:  # an old record never had the field
        db.execute("UPDATE pats SET encrypted=? WHERE id=?", (store._encode(record["id"], record), record["id"]))
    assert store.list_tokens()[0]["last_used_at"] is None


def test_last_used_write_failure_never_breaks_authentication(tmp_path, monkeypatch):
    store = AdminStore(tmp_path / "rct.db", "k" * 44)
    store.initialize()
    _record, token = store.create_token("probe", "read", None)
    monkeypatch.setattr(store, "_touch_pool", None)  # scheduling raises AttributeError internally
    assert store.authenticate_token(token) is not None


def test_wal_mode_permissions_close_and_parallel_writes(tmp_path):
    import sqlite3
    import threading

    path = tmp_path / "data" / "rct.db"
    store = AdminStore(path, "k" * 44)
    store.initialize()
    raw = sqlite3.connect(path)  # `with sqlite3.connect()` would not close it
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    raw.close()
    with store.connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        reader = sqlite3.connect(path)  # an open reader keeps the -wal/-shm files alive
        reader.execute("SELECT count(*) FROM settings").fetchone()
        db.execute("INSERT INTO settings(key, encrypted) VALUES ('x', x'00')")
    for suffix in ("-wal", "-shm"):
        extra = path.with_name(path.name + suffix)
        if extra.exists():
            assert stat.S_IMODE(extra.stat().st_mode) == 0o600
    reader.close()
    errors = []

    def writer(n):
        try:
            for i in range(25):
                store.put(f"k{n}-{i}", i)
                assert store.get(f"k{n}-{i}") == i
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    store.close()
    store.close()  # idempotent
    assert not path.with_name(path.name + "-wal").exists() or path.with_name(path.name + "-wal").stat().st_size == 0
    assert not path.with_name(path.name + "-shm").exists()
