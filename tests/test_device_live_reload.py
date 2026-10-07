#!/usr/bin/env python3
#
# tests/test_device_live_reload.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Device/inverter list live-reload: PUT /admin/api/settings with a changed devices list applies
to the running gateway/transport/scheduling graph immediately, without an application restart, and
without leaking connections or background tasks across repeated add/remove cycles."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest

from app.admin.store import AdminStore
from app.config import DeviceEntry
from app.errors import UnknownDevice
from app.gateway.base import DeviceState
from app.security.tokens import TokenStore
from app.transport.endpoint import EndpointState
from tests.api_helpers import (
    HOST,
    PORT,
    WRITE_TOKEN,
    admin_session_headers,
    make_settings,
    running_app,
)

NEW_HOST = "192.0.2.78"
NEW_PORT = 48900
HEADERS = {"Authorization": f"Bearer {WRITE_TOKEN}"}


@contextlib.asynccontextmanager
async def _prepare_real_store(settings) -> AsyncIterator[AdminStore]:
    """Change the password and mint a write token on the *real* admin store (``settings.admin_db_path``),
    the one ``/admin/api/settings`` actually authenticates bearer tokens against (via ``_store()``);
    ``running_app()`` builds its own, separate store for session/token handling in its own temporary
    directory, which the admin API's bearer-token path never looks at.

    Mirrors ``running_app()``'s pattern: the store is closed in a ``finally`` block so no
    background write (the PAT last-used thread pool) can outlive the test.
    """
    store = AdminStore(settings.admin_db_path, "s" * 48)
    try:
        assert store.change_password(store.initialize(), "a much stronger password")
        with patch("app.admin.store.generate_pat", side_effect=[WRITE_TOKEN]):
            store.create_token("write test token", "read/write", None)
        yield store
    finally:
        store.close()


async def _settled_state(h, device_id: str, timeout: float = 5.0) -> DeviceState:
    """Poll until the device leaves STARTING; raises UnknownDevice itself if the id is unknown."""
    async with asyncio.timeout(timeout):
        while True:
            state = h.runtime.gateway.device_status(device_id).state
            if state is not DeviceState.STARTING:
                return state
            await asyncio.sleep(0.02)


async def test_adding_a_device_takes_effect_without_a_restart(tmp_path):
    settings = make_settings(
        hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db",
        devices=[DeviceEntry(device_id="main", host=HOST, port=PORT)],
    )
    async with _prepare_real_store(settings) as store, running_app(settings) as h:
        HEADERS = await admin_session_headers(h.client, "a much stronger password")
        h.app.state.security.tokens = TokenStore(auth_required=settings.auth_required, admin_store=store)
        current = (await h.client.get("/admin/api/settings", headers=HEADERS)).json()["settings"]["devices"]

        response = await h.client.put(
            "/admin/api/settings", headers=HEADERS,
            json={"devices": [*current, {"host": NEW_HOST, "port": NEW_PORT}]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["restart_required"] == []
        assert "devices" in body["live"]
        devices = body["settings"]["devices"]
        assert len(devices) == 2
        new_id = devices[-1]["device_id"]
        assert new_id in h.runtime.devices

        # Reachable through runtime.gateway within this same test process: no application restart.
        state = await _settled_state(h, new_id)
        assert state in (DeviceState.OK, DeviceState.DEGRADED)


async def test_removing_a_device_takes_effect_without_a_restart_and_closes_its_connection(tmp_path):
    settings = make_settings(
        hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db",
        devices=[
            DeviceEntry(device_id="main", host=HOST, port=PORT),
            DeviceEntry(device_id="extra", host=NEW_HOST, port=NEW_PORT),
        ],
    )
    async with _prepare_real_store(settings) as store, running_app(settings) as h:
        HEADERS = await admin_session_headers(h.client, "a much stronger password")
        h.app.state.security.tokens = TokenStore(auth_required=settings.auth_required, admin_store=store)
        removed_endpoint = h.runtime.gateway._devices["extra"].endpoint
        current = (await h.client.get("/admin/api/settings", headers=HEADERS)).json()["settings"]["devices"]
        kept = [d for d in current if d["device_id"] != "extra"]

        response = await h.client.put("/admin/api/settings", headers=HEADERS, json={"devices": kept})
        assert response.status_code == 200, response.text
        assert response.json()["restart_required"] == []

        assert removed_endpoint.state is EndpointState.DISCONNECTED
        assert "extra" not in h.runtime.devices
        with pytest.raises(UnknownDevice):
            h.runtime.gateway.device_status("extra")


async def test_repeated_add_remove_cycle_does_not_leak_tasks_or_connections(tmp_path):
    """Two full add-then-remove cycles must leave the task count and open-connection count exactly
    where a single steady-state device leaves them — the concrete leak check the feature requires."""
    settings = make_settings(
        hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db",
        devices=[DeviceEntry(device_id="main", host=HOST, port=PORT)],
    )
    async with _prepare_real_store(settings) as store, running_app(settings) as h:
        HEADERS = await admin_session_headers(h.client, "a much stronger password")
        h.app.state.security.tokens = TokenStore(auth_required=settings.auth_required, admin_store=store)
        baseline_tasks = len(h.runtime.tasks)
        baseline_open = h.net.open_now

        async def cycle() -> None:
            current = (await h.client.get("/admin/api/settings", headers=HEADERS)).json()["settings"]["devices"]
            added = await h.client.put(
                "/admin/api/settings", headers=HEADERS,
                json={"devices": [*current, {"host": NEW_HOST, "port": NEW_PORT}]},
            )
            assert added.status_code == 200, added.text
            devices = added.json()["settings"]["devices"]
            new_id = devices[-1]["device_id"]
            await _settled_state(h, new_id)

            kept = [d for d in devices if d["device_id"] != new_id]
            removed = await h.client.put("/admin/api/settings", headers=HEADERS, json={"devices": kept})
            assert removed.status_code == 200, removed.text

        await cycle()
        assert len(h.runtime.tasks) == baseline_tasks, "task count grew after one add/remove cycle"
        assert h.net.open_now == baseline_open, "open connection count grew after one add/remove cycle"

        await cycle()
        assert len(h.runtime.tasks) == baseline_tasks, "task count grew after a second add/remove cycle"
        assert h.net.open_now == baseline_open, "open connection count grew after a second add/remove cycle"
