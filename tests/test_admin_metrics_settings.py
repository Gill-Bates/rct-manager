#!/usr/bin/env python3
#
# tests/test_admin_metrics_settings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Prometheus administration settings apply live, without a server restart."""

import httpx
import pytest

from app.api.app_factory import create_app
from app.config import Settings
from tests.api_helpers import admin_session_headers


@pytest.mark.asyncio
async def test_prometheus_settings_apply_live_and_persist(tmp_path):
    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")
    app = create_app(settings)
    assert app.state.admin_store.change_password(app.state.first_start_password, "a much stronger password")
    updates = {
        "metrics_require_token": False,
        "metrics_trusted_sources": ["192.0.2.10", "2001:db8::/32"],
        "metrics_rate_limit_requests": 300,
        "metrics_rate_limit_window_seconds": 120,
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        # Trust and authentication settings need the cookie session, a PAT is refused for them.
        csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
        login = await client.post("/admin/api/login", headers={"X-CSRF-Token": csrf},
                                  json={"username": "admin", "password": "a much stronger password"})
        headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        response = await client.put("/admin/api/settings", headers=headers, json=updates)
        assert response.status_code == 200
        result = response.json()
        assert result["settings"]["metrics_trusted_sources"] == ["192.0.2.10/32", "2001:db8::/32"]
        assert result["settings"]["metrics_require_token"] is False
        assert result["settings"]["metrics_rate_limit_requests"] == 300
        assert result["settings"]["metrics_rate_limit_window_seconds"] == 120
        # Applied to the running server immediately, no restart needed.
        assert result["restart_required"] == []
        assert set(updates).issubset(result["live"])
        assert app.state.runtime.settings.metrics_require_token is False
        assert [str(n) for n in app.state.runtime.settings.metrics_trusted_sources] == [
            "192.0.2.10/32", "2001:db8::/32",
        ]
        assert app.state.runtime.settings.metrics_rate_limit_requests == 300
        assert app.state.runtime.settings.metrics_rate_limit_window_seconds == 120

        # The token requirement is now off: an unauthenticated scrape succeeds immediately.
        assert (await client.get("/metrics")).status_code == 200

        # The scrape rate limit was reconfigured on the live counter, not only in the settings
        # snapshot: a limit tighter than the scrapes already recorded in this window rejects
        # immediately, which could only happen if the running counter picked up the new value.
        tight = await client.put("/admin/api/settings", headers=headers,
                                 json={"metrics_rate_limit_requests": 1, "metrics_rate_limit_window_seconds": 60})
        assert tight.json()["restart_required"] == []
        assert (await client.get("/metrics")).status_code == 429

        for invalid in (
            {"metrics_trusted_sources": ["not-a-network"]},
            {"metrics_rate_limit_requests": 0},
            {"metrics_rate_limit_requests": 10_001},
            {"metrics_rate_limit_window_seconds": 0},
            {"metrics_rate_limit_window_seconds": 3601},
        ):
            rejected = await client.put("/admin/api/settings", headers=headers, json=invalid)
            assert rejected.status_code == 400

        stored = (await client.get("/admin/api/settings", headers=headers)).json()
        assert stored["settings"]["metrics_trusted_sources"] == ["192.0.2.10/32", "2001:db8::/32"]
        assert stored["settings"]["metrics_rate_limit_requests"] == 1

        # Re-enabling the token requirement is live too: the next unauthenticated scrape is
        # rejected for missing auth (checked before the already-spent scrape limit).
        reenabled = await client.put("/admin/api/settings", headers=headers, json={"metrics_require_token": True})
        assert reenabled.json()["restart_required"] == []
        assert (await client.get("/metrics")).status_code == 401

    restarted = create_app(settings)
    assert restarted.state.runtime.settings.metrics_require_token is True
    assert [str(net) for net in restarted.state.runtime.settings.metrics_trusted_sources] == [
        "192.0.2.10/32", "2001:db8::/32",
    ]
    assert restarted.state.runtime.settings.metrics_rate_limit_requests == 1
    assert restarted.state.runtime.settings.metrics_rate_limit_window_seconds == 60


@pytest.mark.asyncio
async def test_device_heading_prefers_display_name_then_reported_name_then_id(tmp_path):
    from app.config import DeviceEntry

    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db",
                        devices=[DeviceEntry(device_id="main", host="192.0.2.10", port=8899)])
    app = create_app(settings)
    app.state.admin_store.change_password(app.state.first_start_password, "a much stronger password")
    token = app.state.admin_store.create_token("devices", "read/write", None)[1]
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        # Never connected, no admin-set name: falls back to the device_id.
        before = (await client.get("/admin/api/devices", headers=headers)).json()["devices"]
        assert before[0]["name"] == "main"

        # The gateway learned the device's own name (as startup would via read_inverter_name);
        # it wins over the device_id but not over an admin-set display_name.
        app.state.runtime.gateway.set_reported_name("main", "Garage Inverter")
        reported = (await client.get("/admin/api/devices", headers=headers)).json()["devices"]
        assert reported[0]["name"] == "Garage Inverter"
        assert reported[0]["serial_number"] is None  # not read yet: the card omits it

        app.state.runtime.gateway.set_reported_serial("main", "12345678")
        with_serial = (await client.get("/admin/api/devices", headers=headers)).json()["devices"]
        assert with_serial[0]["serial_number"] == "12345678"

    # An admin-set display_name wins over the device-reported name (checked at startup,
    # since "devices" is not a live setting: a saved display_name needs a restart to apply).
    named_settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "named.db",
                              devices=[DeviceEntry(device_id="main", host="192.0.2.10", port=8899,
                                                    display_name="Roof Inverter")])
    named_app = create_app(named_settings)
    named_app.state.admin_store.change_password(named_app.state.first_start_password, "a much stronger password")
    named_token = named_app.state.admin_store.create_token("devices", "read/write", None)[1]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=named_app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        named_app.state.runtime.gateway.set_reported_name("main", "Garage Inverter")
        named = (await client.get("/admin/api/devices",
                                   headers={"Authorization": f"Bearer {named_token}"})).json()["devices"]
        assert named[0]["name"] == "Roof Inverter"


@pytest.mark.asyncio
async def test_stored_address_display_name_is_repaired_once_and_a_chosen_name_survives(tmp_path):
    from app.config import DeviceEntry

    settings = Settings(_env_file=None, hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db",
                        devices=[DeviceEntry(device_id="main", host="10.40.0.188", port=8899,
                                             display_name="10.40.0.188:8899")])
    app = create_app(settings)
    app.state.admin_store.change_password(app.state.first_start_password, "a much stronger password")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Origin": "http://testserver"}) as client:
        headers = await admin_session_headers(client, "a much stronger password")  # devices are session-only
        app.state.runtime.gateway.set_reported_name("main", "Garage Inverter")
        listed = (await client.get("/admin/api/devices", headers=headers)).json()["devices"]
        assert listed[0]["name"] == "Garage Inverter"  # the address-shaped name no longer wins
        stored = (await client.get("/admin/api/settings", headers=headers)).json()["settings"]["devices"]
        assert stored[0]["display_name"] is None
        assert app.state.admin_store.get("operator_settings")["devices"][0]["display_name"] is None

        # The repair ran once; a name the operator sets afterwards is kept.
        saved = await client.put("/admin/api/settings", headers=headers,
                                 json={"devices": [{**stored[0], "display_name": "10.40.0.188:8899"}]})
        assert saved.status_code == 200
        assert saved.json()["settings"]["devices"][0]["display_name"] == "10.40.0.188:8899"
        again = (await client.get("/admin/api/settings", headers=headers)).json()["settings"]["devices"]
        assert again[0]["display_name"] == "10.40.0.188:8899"

    restarted = create_app(settings)
    assert restarted.state.admin_desired_settings.devices[0].display_name == "10.40.0.188:8899"


@pytest.mark.asyncio
async def test_slaves_share_the_master_endpoint_and_ipv6_hosts_lose_their_brackets(tmp_path):
    from tests.test_admin_ui import _logged_in

    async with _logged_in(tmp_path) as client:
        csrf = {"X-CSRF-Token": (await client.get("/admin/api/session")).json()["csrf_token"]}

        async def put(devices):
            return await client.put("/admin/api/settings", headers=csrf, json={"devices": devices})

        shared = await put([{"host": "192.0.2.10", "port": 8899},
                            {"host": "192.0.2.10", "port": 8899, "network_id": 1},
                            {"host": "192.0.2.10", "port": 8899, "network_id": 2}])
        assert shared.status_code == 200, shared.text
        saved = shared.json()["settings"]["devices"]
        assert [device["network_id"] for device in saved] == [None, 1, 2]
        assert (await put(saved)).status_code == 200  # re-saving a valid topology stays valid

        # The full key is host, port and network id: only the exact triple is a duplicate.
        assert (await put([*saved, {"host": "192.0.2.10", "port": 8899, "network_id": 2}])).status_code == 400
        assert (await put([{"host": "192.0.2.10", "port": 8899, "network_id": "x"}])).status_code == 400

        bracketed = await put([{"host": "[2001:db8::1]", "port": 8899}])
        assert bracketed.status_code == 200, bracketed.text
        assert bracketed.json()["settings"]["devices"][0]["host"] == "2001:db8::1"  # transport needs the bare host


@pytest.mark.asyncio
async def test_devices_need_only_host_and_port_and_get_stable_ids(tmp_path):
    from tests.test_admin_ui import _logged_in

    async with _logged_in(tmp_path) as client:
        csrf = {"X-CSRF-Token": (await client.get("/admin/api/session")).json()["csrf_token"]}

        async def put(devices):
            return await client.put("/admin/api/settings", headers=csrf, json={"devices": devices})

        first = await put([{"host": "192.0.2.10", "port": 8899}])
        assert first.status_code == 200, first.text
        saved = first.json()["settings"]["devices"]
        assert saved[0]["device_id"] == "main" and saved[0]["display_name"] is None
        second = await put([*saved, {"host": "inverter.example", "port": 8899}])
        devices = second.json()["settings"]["devices"]
        assert [d["device_id"] for d in devices] == ["main", "inverter-2"]
        edited = await put([{**devices[0], "host": "192.0.2.11"}, devices[1]])  # editing the host keeps the id
        assert edited.json()["settings"]["devices"][0]["device_id"] == "main"
        for bad in ([{"host": "", "port": 1}], [{"host": "http://x", "port": 8899}], [{"host": "a b", "port": 8899}],
                    [{"host": "192.0.2.10", "port": 0}], [{"host": "192.0.2.10", "port": 70000}],
                    [{"host": "192.0.2.10", "port": 8899}, {"host": "192.0.2.10", "port": 8899}]):
            assert (await put(bad)).status_code == 400, bad
        legacy = await put([{"device_id": "old-one", "display_name": "Roof", "host": "192.0.2.20", "port": 8899}])
        assert legacy.json()["settings"]["devices"][0]["device_id"] == "old-one"
        assert legacy.json()["settings"]["devices"][0]["display_name"] == "Roof"
