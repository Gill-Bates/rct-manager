#!/usr/bin/env python3
#
# tests/test_startup_gate.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Device background jobs wait for the first admin password change."""

import logging

import pytest

from app.gateway.base import DeviceState
from tests.api_helpers import make_settings, running_app, wait_settled

NEW_PASSWORD = "a much stronger password"


def _settings(tmp_path):
    return make_settings(hmac_secret="s" * 48, admin_db_path=tmp_path / "rct.db")


def _states(app):
    runtime = app.state.runtime
    return {runtime.gateway.device_status(d).state for d in runtime.devices}


@pytest.mark.asyncio
async def test_jobs_wait_for_password_change_then_start(tmp_path, caplog):
    settings = _settings(tmp_path)
    caplog.set_level(logging.WARNING)
    async with running_app(settings, settle=False, authorize=False) as harness:
        password = harness.app.state.first_start_password
        # The lifespan gate (app_factory._await_password_change) decides whether to start the
        # heartbeat/periodic/refresh tasks synchronously, before the context manager above ever
        # yields: the warning log line and the STARTING state are both already settled by this
        # point, not something a fixed sleep could prove any more reliably (minor note).
        assert _states(harness.app) == {DeviceState.STARTING}  # no heartbeat while the bootstrap password stands
        assert "Initial admin password not changed yet" in caplog.text
        assert (await harness.client.get("/login")).status_code == 200  # the admin UI stays reachable
        assert harness.app.state.admin_store.change_password(password, NEW_PASSWORD)
        await wait_settled(harness.app, timeout=8)
        assert DeviceState.STARTING not in _states(harness.app)


@pytest.mark.asyncio
async def test_later_start_with_changed_password_runs_jobs_immediately(tmp_path, caplog):
    settings = _settings(tmp_path)
    async with running_app(settings, settle=False, authorize=False) as first:
        password = first.app.state.first_start_password
        assert first.app.state.admin_store.change_password(password, NEW_PASSWORD)
    caplog.set_level(logging.WARNING)
    caplog.clear()
    async with running_app(settings, authorize=False) as second:  # settle=True: heartbeat ran at once
        assert DeviceState.STARTING not in _states(second.app)
    assert "Initial admin password not changed yet" not in caplog.text
