#!/usr/bin/env python3
#
# tests/test_energy_verification_restore.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Safety properties of the guided hardware-verification assistant.

Covers the static-review findings:

* P2-1 - the hardware-verification preflight treats "connected" exactly as the energy-command path
  does (``DeviceState.OK`` only) and refuses to start on ``DEGRADED``, through the one shared
  ``_is_connected`` predicate.
* P2-3 - the running 15-minute session TTL lives on the monotonic axis, so a backward wall-clock
  jump cannot extend it (and the forward wall jump the clock contract warns about cannot expire it).
* restore guarantee - a probe the controller refuses is only reported as "returned to its previous
  state" when the dispatch record actually confirms a clean, restored state; an outstanding restore
  is reported as a still-pending hand-back instead.

These exercise the refusal/decision paths directly with light fakes; they do not drive a real
inverter, which the module's higher-level flow would require.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.admin import energy_verification as ev
from app.admin.energy_api import _is_connected
from app.dispatch.capabilities import CapabilityName
from app.dispatch.models import DispatchState
from app.errors import DeviceApiError
from app.gateway.base import DeviceState

DEVICE = "main"


class _Clock:
    """Separate wall and monotonic axes so a wall jump can be shown not to move the deadline."""

    def __init__(self) -> None:
        self._wall = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    def advance_monotonic(self, seconds: float) -> None:
        self._mono += seconds

    def jump_wall(self, seconds: float) -> None:
        self._wall = datetime.fromtimestamp(self._wall.timestamp() + seconds, tz=UTC)


# --- P2-1: one shared "connected for hardware actions" predicate -------------------------------


def test_is_connected_is_ok_only_not_degraded() -> None:
    # The shared predicate the energy-command path and the verification preflight both use.
    assert _is_connected(DeviceState.OK) is True
    assert _is_connected(DeviceState.DEGRADED) is False
    assert _is_connected(DeviceState.UNREACHABLE) is False


def _preflight_request(state: DeviceState):
    """A request whose preflight passes every check EXCEPT (optionally) the connection one."""
    gateway = SimpleNamespace(device_status=lambda _d: SimpleNamespace(state=state))
    settings = SimpleNamespace(enable_write_support=True)

    class _Energy:
        async def approved_write_names(self):
            return ["a", "b"]

        def required_write_names(self):
            return ["a", "b"]

    class _Dispatch:
        def device_limits(self, _d):
            return object()  # limits present

        async def status(self, _d):
            return SimpleNamespace(state=DispatchState.IDLE, restore_required=False)

    runtime = SimpleNamespace(
        gateway=gateway, settings=settings, energy=_Energy(), dispatch=_Dispatch()
    )
    state_ns = SimpleNamespace(runtime=runtime)
    return SimpleNamespace(app=SimpleNamespace(state=state_ns))


def test_preflight_refuses_degraded_but_allows_ok() -> None:
    async def scenario() -> None:
        degraded = await ev._preflight(_preflight_request(DeviceState.DEGRADED), DEVICE)
        assert "The inverter is not connected." in degraded

        ok = await ev._preflight(_preflight_request(DeviceState.OK), DEVICE)
        assert "The inverter is not connected." not in ok

    asyncio.run(scenario())


# --- P2-3: the running TTL is a monotonic deadline ---------------------------------------------


def _session(clock: _Clock) -> ev._Session:
    return ev._Session(
        clock.now(),
        clock.monotonic() + ev.SESSION_TTL.total_seconds(),
        "MODEL",
        "FW",
        "percent",
    )


def _live_request(clock: _Clock, sessions: dict[str, ev._Session]):
    runtime = SimpleNamespace(clock=clock)
    state = SimpleNamespace(runtime=runtime, energy_verification_sessions=sessions)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_backward_wall_jump_does_not_extend_the_session() -> None:
    clock = _Clock()
    session = _session(clock)
    request = _live_request(clock, {DEVICE: session})

    # A large backward system-time correction: a wall-clock TTL would read the session as far from
    # expiry (or even negative age) and keep it alive; the monotonic deadline is untouched.
    clock.jump_wall(-3600)
    assert ev._live_session(request, DEVICE) is session

    # Monotonic time reaching the deadline expires it, regardless of the wall jump above.
    clock.advance_monotonic(ev.SESSION_TTL.total_seconds())
    with pytest.raises(HTTPException) as caught:
        ev._live_session(request, DEVICE)
    assert caught.value.status_code == 409


def test_forward_wall_jump_does_not_expire_the_session_early() -> None:
    clock = _Clock()
    session = _session(clock)
    request = _live_request(clock, {DEVICE: session})

    # A forward wall jump beyond the TTL would expire a wall-clock session prematurely; monotonic
    # time has not advanced, so the session is still live.
    clock.jump_wall(ev.SESSION_TTL.total_seconds() + 60)
    assert ev._live_session(request, DEVICE) is session


# --- restore guarantee: the "returned to its previous state" claim must be true -----------------


class _Gateway:
    def __init__(self, baseline: float = 500.0) -> None:
        self._baseline = baseline

    def device_status(self, _d):
        return SimpleNamespace(state=DeviceState.OK)

    async def read_system(self, _device_id, name):
        # Only battery_power is read before the probe submit in _run_hold_test.
        return SimpleNamespace(value=self._baseline)


class _WriteRecord:
    name = CapabilityName.WRITE_PATH
    soc_strategy_external_code = 2  # already present, so the test writes no candidate code


class _Dispatch:
    """A dispatch port whose probe always refuses; ``restore_required`` is configurable."""

    def __init__(self, *, restore_required: bool, state: DispatchState) -> None:
        self._restore_required = restore_required
        self._state = state

    def capabilities(self, _d):
        return [_WriteRecord()]

    async def submit_verification_probe(self, _device_id, _command):
        raise DeviceApiError("device_timeout")

    async def status(self, _d):
        return SimpleNamespace(
            restore_required=self._restore_required, state=self._state
        )


def _hold_request(clock: _Clock, dispatch: _Dispatch):
    runtime = SimpleNamespace(clock=clock, gateway=_Gateway(), dispatch=dispatch)
    state = SimpleNamespace(runtime=runtime)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_refused_probe_reports_restored_only_when_confirmed_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep the test independent of which registers a hold restores: the probe is refused before any
    # restore register is read, so an empty restore set is sufficient and realistic here.
    monkeypatch.setattr(ev, "_RESTORE_REGISTERS", ())
    clock = _Clock()
    dispatch = _Dispatch(restore_required=False, state=DispatchState.IDLE)
    request = _hold_request(clock, dispatch)

    async def scenario() -> None:
        monkeypatch.setattr(ev, "_dispatch_or_503", lambda _r: dispatch)
        with pytest.raises(ev._Blocked) as caught:
            await ev._run_hold_test(request, DEVICE, prove_grid=False)
        assert "returned to its previous state" in str(caught.value)

    asyncio.run(scenario())


def test_refused_probe_reports_pending_handback_when_restore_still_outstanding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ev, "_RESTORE_REGISTERS", ())
    clock = _Clock()
    dispatch = _Dispatch(
        restore_required=True, state=DispatchState.FAULT_RESTORE_PENDING
    )
    request = _hold_request(clock, dispatch)

    async def scenario() -> None:
        monkeypatch.setattr(ev, "_dispatch_or_503", lambda _r: dispatch)
        with pytest.raises(ev._Blocked) as caught:
            await ev._run_hold_test(request, DEVICE, prove_grid=False)
        message = str(caught.value)
        # Never claim a completed hand-back while the restore is still pending.
        assert "returned to its previous state" not in message
        assert "keeps retrying the hand-back" in message

    asyncio.run(scenario())
