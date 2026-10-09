#!/usr/bin/env python3
#
# app/admin/energy_verification.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Guided hardware verification for the Basic Energy page.

The assistant produces the same attestation as the Expert form, but every value comes from the
inverter or from a short, explicitly confirmed test, never from a guess:

* model and firmware are read live from the inverter;
* the SoC-target unit is derived from the live register value (and left open when it is ambiguous);
* both sign conventions come from a live reading plus the operator's statement of what the battery
  or the grid is physically doing right now (the server re-reads the value itself);
* the write path is proven by one bounded hold test: the inverter is told to stay at 0 W, every
  control register is read back, the battery power must actually follow, and the previous state
  must be restored and read back again.

What it cannot prove is a non-zero setpoint direction: the hold writes 0 W by design. That limit
is shown to the operator. Nothing is stored until ``commit``; the backend gate is untouched.
"""

import asyncio
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from app.admin.dispatch_api import (
    _ADMIN_ACTOR,
    _conflict,
    _device_or_404,
    _dispatch_or_503,
    _require_admin_write,
)
from app.admin.energy_api import (
    AdminEnergyDeviceStatus,
    HardwareVerificationBody,
    _admin_status,
    _energy_or_503,
    verification_records,
)
from app.catalog.registry import RegistryCatalog
from app.dispatch.capabilities import CapabilityName
from app.dispatch.controller import CapabilityConflict
from app.dispatch.models import DispatchCommand, DispatchMode, DispatchState
from app.errors import DeviceApiError
from app.gateway.base import DeviceState
from app.gateway.rct_dispatch import RctDispatchGateway
from app.protocol.values import DEFAULT_WIDTHS

router = APIRouter(prefix="/admin/api/energy/devices/{device_id}/verification-assistant", include_in_schema=False)
log = logging.getLogger(__name__)

# Vendor-documented "external control" value of the strategy register. It is only a candidate: it
# is stored solely when the hold test shows the battery really follows the 0 W setpoint.
EXTERNAL_STRATEGY_CODE_CANDIDATE = 2
SESSION_TTL = timedelta(minutes=15)
MIN_DIRECTION_W = 100.0  # below this a battery/grid reading is too close to noise to tell a direction
MIN_BASELINE_W = 150.0  # the battery must be visibly moving, or "it went to 0 W" proves nothing
SETTLED_W = 50.0
PROBE_TIMEOUT_S = 30.0
PROBE_POLL_S = 2.0
PROBE_VALID_S = 60.0

_RESTORE_REGISTERS = RctDispatchGateway.REQUIRED_WRITES  # exactly what a hold writes or restores


@dataclass
class _Session:
    started: datetime
    model: str
    firmware: str
    soc_target_unit: Literal["ratio", "percent"] | None
    battery_discharge_positive: bool | None = None
    grid_import_positive: bool | None = None
    battery_evidence: str | None = None
    grid_evidence: str | None = None
    code: int | None = None
    enum_byte_width: int | None = None
    bool_byte_width: int | None = None
    test_evidence: str | None = None
    messages: dict[str, str] = field(default_factory=dict)  # step id -> failure text
    busy: asyncio.Lock = field(default_factory=asyncio.Lock)


class StepView(BaseModel):
    id: Literal["battery", "grid", "control_test"]
    status: Literal["pending", "done", "failed"]
    message: str | None


class ReadingsView(BaseModel):
    battery_power_w: float | None
    grid_power_w: float | None
    solar_power_w: float | None
    household_load_w: float | None
    soc_percent: float | None


class AssistantState(BaseModel):
    device_id: str
    started: bool
    blockers: list[str]
    model: str | None
    firmware: str | None
    steps: list[StepView]
    readings: ReadingsView | None
    can_commit: bool
    expires_at: datetime | None


class DirectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["battery", "grid"]
    answer: Literal["discharging", "charging", "importing", "exporting"]


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: StrictBool


class _Blocked(Exception):
    """A plain-language reason why a step cannot run or prove its claim."""


def _sessions(request: Request) -> dict[str, _Session]:
    state = request.app.state
    if not hasattr(state, "energy_verification_sessions"):
        state.energy_verification_sessions = {}
    return state.energy_verification_sessions


def _live_session(request: Request, device_id: str) -> _Session:
    session = _sessions(request).get(device_id)
    now = request.app.state.runtime.clock.now()
    if session is None or now - session.started > SESSION_TTL:
        _sessions(request).pop(device_id, None)
        raise HTTPException(409, "The verification assistant is not running or has expired. Start it again.")
    return session


async def _read(request: Request, device_id: str, name: str):
    try:
        return await request.app.state.runtime.gateway.read_system(device_id, name)
    except DeviceApiError as exc:
        raise _Blocked(f"The inverter did not answer a read request ({exc.code}).") from exc


async def _number(request: Request, device_id: str, name: str) -> float:
    value = (await _read(request, device_id, name)).value
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _Blocked("The inverter returned an unexpected value.")
    return float(value)


async def _text(request: Request, device_id: str, name: str, limit: int) -> str:
    value = (await _read(request, device_id, name)).value
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > limit or not text.isascii() or not text.isprintable():
        raise _Blocked("The inverter did not report a usable model or firmware identification.")
    return text


async def _preflight(request: Request, device_id: str) -> list[str]:
    runtime = request.app.state.runtime
    blockers: list[str] = []
    state = runtime.gateway.device_status(device_id).state
    if state not in (DeviceState.OK, DeviceState.DEGRADED):
        blockers.append("The inverter is not connected.")
    if not runtime.settings.enable_write_support:
        blockers.append('Write access is switched off. Turn on "Write access" on the Inverters page.')
    energy = _energy_or_503(request)
    dispatch = _dispatch_or_503(request)
    approved = set(await energy.approved_write_names())
    if any(name not in approved for name in energy.required_write_names()):
        blockers.append("The battery power control parameters are not approved yet (Inverters page).")
    if dispatch.device_limits(device_id) is None:
        blockers.append("The power limits are not set yet.")
    status = await dispatch.status(device_id)
    if status.state is not DispatchState.IDLE or status.restore_required:
        blockers.append("A battery operation is running or being restored. Wait until it has finished.")
    return blockers


async def _readings(request: Request, device_id: str) -> ReadingsView:
    async def one(name: str) -> float | None:
        try:
            return await _number(request, device_id, name)
        except _Blocked:
            return None

    solar_a, solar_b = await one("solar_a_power"), await one("solar_b_power")
    soc = await one("battery_soc")
    return ReadingsView(
        battery_power_w=await one("battery_power"),
        grid_power_w=await one("grid_power"),
        solar_power_w=None if solar_a is None or solar_b is None else solar_a + solar_b,
        household_load_w=await one("household_load_power"),
        soc_percent=None if soc is None else round(soc * 100, 1),
    )


def _view(device_id: str, session: _Session | None, blockers: list[str], readings, now: datetime | None) -> AssistantState:
    def step(step_id, done: bool) -> StepView:
        message = None if session is None else session.messages.get(step_id)
        status = "done" if done else ("failed" if message else "pending")
        return StepView(id=step_id, status=status, message=message)

    steps = [
        step("battery", session is not None and session.battery_discharge_positive is not None),
        step("grid", session is not None and session.grid_import_positive is not None),
        step("control_test", session is not None and session.code is not None),
    ]
    can_commit = session is not None and not blockers and all(s.status == "done" for s in steps) and (
        session.soc_target_unit is not None
    )
    return AssistantState(
        device_id=device_id,
        started=session is not None,
        blockers=blockers,
        model=None if session is None else session.model,
        firmware=None if session is None else session.firmware,
        steps=steps,
        readings=readings,
        can_commit=bool(can_commit),
        expires_at=None if session is None or now is None else session.started + SESSION_TTL,
    )


async def _state(request: Request, device_id: str, session: _Session | None, *, readings: bool = True):
    blockers = await _preflight(request, device_id)
    if session is not None and session.soc_target_unit is None:
        blockers = [
            *blockers,
            (
                "The battery target setting currently sits at 0 % or 100 %, so its unit cannot be determined. "
                "Change the battery target in the inverter app to a value between 5 % and 95 %, then start again."
            ),
        ]
    live = await _readings(request, device_id) if readings and not blockers else None
    return _view(device_id, session, blockers, live, request.app.state.runtime.clock.now())


@router.post("/start")
async def start(
    request: Request,
    device_id: str,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AssistantState:
    """Read-only: identify the inverter and check the preconditions. Writes nothing."""
    del admin
    _device_or_404(request, device_id)
    blockers = await _preflight(request, device_id)
    if blockers:
        _sessions(request).pop(device_id, None)
        return _view(device_id, None, blockers, None, None)
    try:
        model = await _text(request, device_id, "android_description", 128)
        firmware = await _text(request, device_id, "svnversion", 64)
        unit = RctDispatchGateway.soc_target_unit(
            await _number(request, device_id, RctDispatchGateway.SOC_TARGET_REGISTER)
        )
    except _Blocked as exc:
        return _view(device_id, None, [str(exc)], None, None)
    session = _Session(request.app.state.runtime.clock.now(), model, firmware, unit)
    _sessions(request)[device_id] = session
    log.warning("Energy verification assistant started: device=%s model=%s firmware=%s admin=%s",
                device_id, model, firmware, _ADMIN_ACTOR)  # fmt: skip
    return await _state(request, device_id, session)


@router.delete("")
async def cancel(
    request: Request,
    device_id: str,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AssistantState:
    del admin
    _device_or_404(request, device_id)
    session = _sessions(request).get(device_id)
    if session is not None and session.busy.locked():
        raise HTTPException(409, "The control test is running. It cannot be cancelled until it has finished.")
    _sessions(request).pop(device_id, None)
    return _view(device_id, None, [], None, None)


@router.post("/direction")
async def direction(
    request: Request,
    device_id: str,
    body: DirectionBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AssistantState:
    """Derive one sign convention from a live reading and the operator's physical observation."""
    del admin
    _device_or_404(request, device_id)
    session = _live_session(request, device_id)
    allowed = {"battery": ("discharging", "charging"), "grid": ("importing", "exporting")}[body.kind]
    if body.answer not in allowed:
        raise HTTPException(422, "The answer does not belong to this question.")
    register = "battery_power" if body.kind == "battery" else "grid_power"
    try:
        raw = await _number(request, device_id, register)
    except _Blocked as exc:
        session.messages[body.kind] = str(exc)
        return await _state(request, device_id, session)
    if abs(raw) < MIN_DIRECTION_W:
        session.messages[body.kind] = (
            f"The inverter currently reports only {raw:.0f} W, which is too little to tell the direction. "
            "Try again when more power is flowing."
        )
        return await _state(request, device_id, session)
    positive_means_first = raw > 0  # "first" answer = discharging / importing
    first = body.answer in ("discharging", "importing")
    flag = positive_means_first == first
    evidence = f"inverter reported {raw:.0f} W while the operator saw it {body.answer}"
    if body.kind == "battery":
        session.battery_discharge_positive, session.battery_evidence = flag, evidence
    else:
        session.grid_import_positive, session.grid_evidence = flag, evidence
    session.messages.pop(body.kind, None)
    return await _state(request, device_id, session)


async def _restore_matches(request: Request, device_id: str, before: dict[str, float | bool | int]) -> bool:
    for name, expected in before.items():
        value = (await _read(request, device_id, name)).value
        if isinstance(expected, bool) or isinstance(value, bool):
            if value is not expected:
                return False
        elif not isinstance(value, int | float) or abs(float(value) - float(expected)) > 1e-3:
            return False
    return True


async def _run_hold_test(request: Request, device_id: str) -> tuple[int, str]:
    """The bounded hold. Returns (strategy code, evidence text) or raises ``_Blocked``."""
    runtime = request.app.state.runtime
    dispatch = _dispatch_or_503(request)
    baseline = await _number(request, device_id, "battery_power")
    if abs(baseline) < MIN_BASELINE_W:
        raise _Blocked(
            f"The battery is almost idle ({baseline:.0f} W), so the test could not show a difference. "
            f"Run it while the battery is charging or discharging with at least {MIN_BASELINE_W:.0f} W."
        )
    write_record = next(r for r in dispatch.capabilities(device_id) if r.name is CapabilityName.WRITE_PATH)
    code = write_record.soc_strategy_external_code
    code_set_here = code is None
    if code is None:
        code = EXTERNAL_STRATEGY_CODE_CANDIDATE
    before: dict[str, float | bool | int] = {}
    for name in _RESTORE_REGISTERS:
        value = (await _read(request, device_id, name)).value
        if not isinstance(value, bool | int | float):
            raise _Blocked("The inverter returned an unexpected value.")
        before[name] = value
    if code_set_here:
        try:
            await dispatch.set_capabilities(device_id, [replace(write_record, soc_strategy_external_code=code)])
        except CapabilityConflict as exc:
            raise _Blocked("A battery operation is running.") from exc
    started = False
    settled = 0
    last = baseline
    reason = "The battery did not follow the 0 W hold within the time limit."
    try:
        now = runtime.clock.now()
        command = DispatchCommand(DispatchMode.HOLD, None, 0.0, now + timedelta(seconds=PROBE_VALID_S))
        try:
            await dispatch.submit_verification_probe(device_id, command)
        except DeviceApiError as exc:
            raise _Blocked(f"The inverter refused the test ({exc.code}). It was returned to its previous state.") from exc
        started = True
        waited = 0.0
        while waited <= PROBE_TIMEOUT_S:
            strategy = (await _read(request, device_id, "power_mng_soc_strategy")).value
            setpoint = (await _read(request, device_id, "power_mng_battery_power_extern")).value
            grid_charge = (await _read(request, device_id, "power_mng_use_grid_power_enable")).value
            last = await _number(request, device_id, "battery_power")
            registers_ok = (
                strategy == code
                and isinstance(setpoint, int | float) and not isinstance(setpoint, bool) and abs(setpoint) < 1e-3
                and grid_charge is False
            )
            settled = settled + 1 if registers_ok and abs(last) <= SETTLED_W else 0
            if settled >= 2:
                break
            await asyncio.sleep(PROBE_POLL_S)
            waited += PROBE_POLL_S
        else:
            raise _Blocked(reason)
    except DeviceApiError as exc:
        raise _Blocked(f"The inverter stopped answering during the test ({exc.code}).") from exc
    finally:
        restored = False
        if started:
            try:
                await dispatch.cancel(device_id)
                restored = await _restore_matches(request, device_id, before)
            except Exception:  # the restore outcome is reported below either way
                log.exception("Verification test restore failed: device=%s", device_id)
        if started and not restored:
            log.error("Verification test did not confirm the restore: device=%s", device_id)
            # Raised from the finally block on purpose: it replaces a success with a failure.
            raise _Blocked(
                "The inverter could not be confirmed back in its previous state. Open the inverter app and check "
                "that automatic operation is active. The gateway keeps retrying the hand-back."
            )
        if code_set_here and not settled >= 2:
            try:
                await dispatch.set_capabilities(device_id, [write_record])
            except Exception:  # noqa: BLE001 - an unverified candidate left behind is harmless
                log.warning("Could not reset the candidate strategy code: device=%s", device_id)
    evidence = (
        f"control test: with the strategy set, battery moved from {baseline:.0f} W to {last:.0f} W under a 0 W hold; "
        "registers read back; previous state restored and read back"
    )
    return code, evidence


@router.post("/control-test")
async def control_test(
    request: Request,
    device_id: str,
    body: ConfirmBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AssistantState:
    """The one active step: a short 0 W hold with read-back and restore. Needs explicit consent."""
    del admin
    _device_or_404(request, device_id)
    if not body.confirm:
        raise HTTPException(400, "The control test needs the operator's explicit confirmation.")
    runtime = request.app.state.runtime
    runtime.ensure_accepting()
    session = _live_session(request, device_id)
    if session.battery_discharge_positive is None:
        raise HTTPException(409, "Confirm the battery direction first.")
    if session.busy.locked():
        raise HTTPException(409, "The control test is already running.")
    async with session.busy:
        blockers = await _preflight(request, device_id)
        if blockers:
            session.messages["control_test"] = blockers[0]
            return await _state(request, device_id, session)
        log.warning("Energy verification control test started: device=%s admin=%s", device_id, _ADMIN_ACTOR)
        try:
            code, evidence = await _run_hold_test(request, device_id)
        except _Blocked as exc:
            session.code = None
            session.messages["control_test"] = str(exc)
            log.warning("Energy verification control test failed: device=%s reason=%s", device_id, exc)
            return await _state(request, device_id, session)
        catalog = RegistryCatalog.from_file(runtime.settings.object_registry_path)
        enum_entry = catalog.object_entry("power_mng_soc_strategy")
        bool_entry = catalog.object_entry("power_mng_use_grid_power_enable")
        session.code = code
        session.enum_byte_width = enum_entry.byte_width or DEFAULT_WIDTHS[enum_entry.data_type]
        session.bool_byte_width = bool_entry.byte_width or DEFAULT_WIDTHS[bool_entry.data_type]
        session.test_evidence = evidence
        session.messages.pop("control_test", None)
        log.warning("Energy verification control test passed: device=%s admin=%s", device_id, _ADMIN_ACTOR)
        return await _state(request, device_id, session)


@router.post("/commit")
async def commit(
    request: Request,
    device_id: str,
    body: ConfirmBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AdminEnergyDeviceStatus:
    """Store the collected evidence as the same record the Expert form writes."""
    del admin
    _device_or_404(request, device_id)
    if not body.confirm:
        raise HTTPException(400, "The operator must confirm before the verification is stored.")
    session = _live_session(request, device_id)
    state = await _state(request, device_id, session, readings=False)
    if not state.can_commit or session.busy.locked():
        raise HTTPException(409, "The verification is incomplete: " + "; ".join(state.blockers or ["a step is open"]))
    try:  # the inverter must still be the one that was tested
        model = await _text(request, device_id, "android_description", 128)
        firmware = await _text(request, device_id, "svnversion", 64)
    except _Blocked as exc:
        raise HTTPException(409, str(exc)) from exc
    if (model, firmware) != (session.model, session.firmware):
        _sessions(request).pop(device_id, None)
        raise HTTPException(409, "The inverter identification changed during the verification. Start again.")
    assert session.code is not None and session.soc_target_unit is not None  # guaranteed by can_commit
    note = (
        f"Guided: battery sign - {session.battery_evidence}; grid sign - {session.grid_evidence}; "
        f"{session.test_evidence}"
    )[:200]
    record_body = HardwareVerificationBody(
        verified_device_model=model,
        verified_firmware=firmware,
        note=note,
        soc_strategy_external_code=session.code,
        enum_byte_width=session.enum_byte_width,
        bool_byte_width=session.bool_byte_width,
        write_frame_layout_verified=True,
        apply_sequence_verified=True,
        battery_discharge_positive=session.battery_discharge_positive,
        grid_import_positive=session.grid_import_positive,
        soc_target_unit=session.soc_target_unit,
    )
    dispatch = _dispatch_or_503(request)
    try:
        records = verification_records(dispatch, device_id, record_body, request.app.state.runtime.clock.now())
        await dispatch.set_capabilities(device_id, records)
    except CapabilityConflict as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _sessions(request).pop(device_id, None)
    log.warning(
        "Energy hardware verification saved by the guided assistant: device=%s model=%s firmware=%s admin=%s",
        device_id, model, firmware, _ADMIN_ACTOR,
    )  # fmt: skip
    return await _admin_status(request, device_id)
