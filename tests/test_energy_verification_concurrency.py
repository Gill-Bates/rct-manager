#!/usr/bin/env python3
#
# tests/test_energy_verification_concurrency.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""F1: the per-device verification session's busy lock is the operation mutex.

A control test holds ``session.busy`` for the whole 0 W hardware hold. ``commit`` and ``start`` must
treat that lock as a mutex, not a point-in-time flag, so overlapping admin requests cannot orphan a
running verification, replace an in-flight session, or store capability-lifting evidence while a
hardware test is active. These tests exercise the refusal paths directly with light fakes; they do
not drive a real inverter, which the module's higher-level flow would require.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.admin import energy_verification as ev

DEVICE = "main"


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return 1000.0


async def _fail_if_called(*args, **kwargs):  # pragma: no cover - asserted never reached
    raise AssertionError(
        "set_capabilities must not run while the control test holds the mutex"
    )


def _request(sessions: dict[str, ev._Session]):
    """A minimal Request-like object: just the app.state the refusal paths read before any I/O."""
    dispatch = SimpleNamespace(set_capabilities=_fail_if_called)
    runtime = SimpleNamespace(
        devices={DEVICE: object()}, clock=_Clock(), dispatch=dispatch
    )
    state = SimpleNamespace(runtime=runtime, energy_verification_sessions=sessions)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _ready_session() -> ev._Session:
    clock = _Clock()
    session = ev._Session(
        clock.now(),
        clock.monotonic() + ev.SESSION_TTL.total_seconds(),
        "MODEL",
        "FW",
        "percent",
    )
    # A completed-looking session so can_commit would otherwise pass; the point under test is the lock.
    session.battery_discharge_positive = True
    session.grid_import_positive = True
    session.code = 2
    session.enum_byte_width = 1
    session.bool_byte_width = 1
    return session


def test_commit_refuses_while_a_control_test_holds_the_lock() -> None:
    session = _ready_session()
    sessions = {DEVICE: session}
    request = _request(sessions)

    async def scenario() -> None:
        async with session.busy:  # stand in for an in-flight control test
            with pytest.raises(HTTPException) as caught:
                await ev.commit(
                    request, DEVICE, ev.ConfirmBody(confirm=True), admin=None
                )
            assert caught.value.status_code == 409
            assert caught.value.detail == "The control test is running."
        # The session was neither stored nor removed: the in-flight operation keeps its session.
        assert sessions[DEVICE] is session

    asyncio.run(scenario())


def test_start_refuses_to_replace_a_busy_session() -> None:
    session = _ready_session()
    sessions = {DEVICE: session}
    request = _request(sessions)

    async def scenario() -> None:
        async with session.busy:
            with pytest.raises(HTTPException) as caught:
                await ev.start(request, DEVICE, admin=None)
            assert caught.value.status_code == 409
            assert caught.value.detail == "The control test is running."
        # start() neither dropped nor replaced the busy session.
        assert sessions[DEVICE] is session

    asyncio.run(scenario())


def test_commit_runs_its_whole_body_under_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no control test active, commit must still hold the mutex across its awaited reads and the
    # store, so a control test that starts mid-commit blocks instead of racing in. _state() is the
    # first awaited call inside the critical section; observing the lock held there proves the body
    # runs under it rather than past a one-shot check.
    session = _ready_session()
    request = _request({DEVICE: session})
    observed: dict[str, bool] = {}

    async def fake_state(req, device_id, sess, *, readings=True):
        observed["locked_during_body"] = sess.busy.locked()
        raise HTTPException(
            409, "stop after observing the lock"
        )  # short-circuit the rest of commit

    monkeypatch.setattr(ev, "_state", fake_state)

    async def scenario() -> None:
        with pytest.raises(HTTPException):
            await ev.commit(request, DEVICE, ev.ConfirmBody(confirm=True), admin=None)
        assert observed["locked_during_body"] is True
        assert session.busy.locked() is False  # released on exit

    asyncio.run(scenario())
