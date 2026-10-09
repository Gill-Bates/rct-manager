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
* both sign conventions are first derived from the inverter's own power balance (house load = solar +
  grid import + battery discharge) with a unique, well-separated fit of the sign hypotheses; only
  what that cannot prove is taken from a live reading plus the operator's statement of what the
  battery or the grid is physically doing right now (the server re-reads the value itself). A sign
  that is neither proven nor confirmed is never stored, and an "idle" statement proves nothing;
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
# Power balance: the readings are taken one after another and the inverter has conversion losses,
# so a fit counts only inside this tolerance and only when the opposite sign is clearly worse.
BALANCE_TOLERANCE_W = 120.0
BALANCE_TOLERANCE_RATIO = 0.08
BALANCE_NOISE_W = 20.0  # a solar sum slightly below zero at night is meter noise, not a convention
BALANCE_SAMPLES = 2
BALANCE_GAP_S = 1.0

_RESTORE_REGISTERS = RctDispatchGateway.REQUIRED_WRITES  # exactly what a hold writes or restores


@dataclass(frozen=True, slots=True)
class BalanceSample:
    """One simultaneous set of readings, all as the registers report them (conventions unproven)."""

    battery_w: float
    grid_w: float
    solar_w: float
    load_w: float


@dataclass(frozen=True, slots=True)
class BalanceProof:
    battery_discharge_positive: bool | None = None
    grid_import_positive: bool | None = None


def _prove_sample(sample: BalanceSample) -> tuple[bool | None, bool | None]:
    """Which sign hypotheses the balance load = solar + grid_import + battery_discharge supports.

    Solar generation and house load are physical magnitudes that cannot be negative, so they anchor
    the balance without a convention under test. A flag is proven only when its best hypothesis fits
    within the tolerance and the opposite one misses by more than twice that, otherwise it stays open.
    """
    values = (sample.battery_w, sample.grid_w, sample.solar_w, sample.load_w)
    if not all(math.isfinite(v) for v in values) or sample.solar_w < -BALANCE_NOISE_W or sample.load_w < 0:
        return None, None
    solar = max(sample.solar_w, 0.0)
    tolerance = max(
        BALANCE_TOLERANCE_W,
        BALANCE_TOLERANCE_RATIO * max(sample.load_w, solar, abs(sample.grid_w), abs(sample.battery_w)),
    )
    residual = {
        (battery, grid): abs(
            sample.load_w - solar
            - (sample.grid_w if grid else -sample.grid_w)
            - (sample.battery_w if battery else -sample.battery_w)
        )
        for battery in (True, False)
        for grid in (True, False)
    }

    def decide(index: int, magnitude: float) -> bool | None:
        if magnitude < MIN_DIRECTION_W:
            return None  # an idle quantity cannot tell its own sign
        best = {value: min(r for key, r in residual.items() if key[index] is value) for value in (True, False)}
        if best[True] == best[False]:
            return None
        winner = best[True] < best[False]
        return winner if best[winner] <= tolerance and best[not winner] > 2 * tolerance else None

    battery = decide(0, abs(sample.battery_w))
    grid = decide(1, abs(sample.grid_w))
    if battery is not None and grid is not None and residual[(battery, grid)] > tolerance:
        return None, None  # the two proofs do not describe one consistent system
    return battery, grid


def prove_signs_from_balance(samples: list[BalanceSample]) -> BalanceProof:
    """A flag is proven only when every sample proves the same value; inconsistent data proves nothing."""
    if not samples:
        return BalanceProof()
    results = [_prove_sample(sample) for sample in samples]

    def agreed(index: int) -> bool | None:
        values = {result[index] for result in results}
        return values.pop() if len(values) == 1 else None

    return BalanceProof(agreed(0), agreed(1))


@dataclass(frozen=True, slots=True)
class _Established:
    """How a sign flag was established, kept for the operator's summary and the audit note."""

    method: Literal["balance", "operator", "both"]
    direction: Literal["charging", "discharging", "importing", "exporting"]
    power_w: float


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
    battery_proof: _Established | None = None
    grid_proof: _Established | None = None
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
    method: Literal["balance", "operator", "both"] | None = None  # how a done sign step was established
    direction: Literal["charging", "discharging", "importing", "exporting"] | None = None
    power_w: float | None = None


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
    min_direction_w: float = MIN_DIRECTION_W


class DirectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["battery", "grid"]
    answer: Literal["discharging", "charging", "importing", "exporting", "idle"]


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
    def step(step_id, done: bool, proof: _Established | None = None) -> StepView:
        message = None if session is None else session.messages.get(step_id)
        status = "done" if done else ("failed" if message else "pending")
        if not done or proof is None:
            return StepView(id=step_id, status=status, message=message)
        return StepView(
            id=step_id, status=status, message=message,
            method=proof.method, direction=proof.direction, power_w=round(proof.power_w, 1),
        )

    steps = [
        step("battery", session is not None and session.battery_discharge_positive is not None,
             None if session is None else session.battery_proof),
        step("grid", session is not None and session.grid_import_positive is not None,
             None if session is None else session.grid_proof),
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


async def _balance_proof(request: Request, device_id: str) -> tuple[BalanceProof, BalanceSample | None]:
    """Sample the balance a moment apart; any missing reading means there is nothing to prove with."""
    samples: list[BalanceSample] = []
    for index in range(BALANCE_SAMPLES):
        if index:
            await asyncio.sleep(BALANCE_GAP_S)
        live = await _readings(request, device_id)
        figures = (live.battery_power_w, live.grid_power_w, live.solar_power_w, live.household_load_w)
        if any(value is None for value in figures):
            return BalanceProof(), None
        samples.append(BalanceSample(*figures))  # type: ignore[arg-type]  # None excluded above
    return prove_signs_from_balance(samples), samples[-1]


def _establish(
    session: _Session, kind: str, flag: bool, raw: float, method: Literal["balance", "operator", "both"]
) -> None:
    """Store one proven flag with how it was established; ``raw`` is the register value as read."""
    first = (raw > 0) == flag  # discharging / importing
    how = {"balance": "power balance", "operator": "operator saw it", "both": "power balance, operator agreed"}[method]
    evidence = f"{how} ({raw:+.0f} W)"
    if kind == "battery":
        proof = _Established(method, "discharging" if first else "charging", abs(raw))
        session.battery_discharge_positive, session.battery_evidence, session.battery_proof = flag, evidence, proof
    else:
        proof = _Established(method, "importing" if first else "exporting", abs(raw))
        session.grid_import_positive, session.grid_evidence, session.grid_proof = flag, evidence, proof
    session.messages.pop(kind, None)


@router.post("/auto-check")
async def auto_check(
    request: Request,
    device_id: str,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AssistantState:
    """Prove the sign conventions from the power balance where the data allows; ask nobody."""
    del admin
    _device_or_404(request, device_id)
    session = _live_session(request, device_id)
    proof, sample = await _balance_proof(request, device_id)
    if sample is not None:
        if session.battery_discharge_positive is None and proof.battery_discharge_positive is not None:
            _establish(session, "battery", proof.battery_discharge_positive, sample.battery_w, "balance")
        if session.grid_import_positive is None and proof.grid_import_positive is not None:
            _establish(session, "grid", proof.grid_import_positive, sample.grid_w, "balance")
    return await _state(request, device_id, session)


@router.post("/direction")
async def direction(
    request: Request,
    device_id: str,
    body: DirectionBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AssistantState:
    """Derive one sign convention from a live reading and the operator's physical observation.

    Used only where the power balance cannot prove it. An "idle" statement proves no direction: it is
    checked against the live reading and otherwise changes nothing.
    """
    del admin
    _device_or_404(request, device_id)
    session = _live_session(request, device_id)
    allowed = {"battery": ("discharging", "charging", "idle"), "grid": ("importing", "exporting", "idle")}[body.kind]
    if body.answer not in allowed:
        raise HTTPException(422, "The answer does not belong to this question.")
    register = "battery_power" if body.kind == "battery" else "grid_power"
    try:
        raw = await _number(request, device_id, register)
    except _Blocked as exc:
        session.messages[body.kind] = str(exc)
        return await _state(request, device_id, session)
    if body.answer == "idle":
        if abs(raw) >= MIN_DIRECTION_W:
            session.messages[body.kind] = (
                f"The inverter reports about {abs(raw):.0f} W, so the {body.kind} is not idle right now. "
                "Choose what it is doing, or wait until the display shows it idle."
            )
        else:
            session.messages.pop(body.kind, None)  # still unproven: the assistant keeps waiting
        return await _state(request, device_id, session)
    if abs(raw) < MIN_DIRECTION_W:
        session.messages[body.kind] = (
            f"The inverter currently reports only {raw:.0f} W, which is too little to tell the direction. "
            "Try again when more power is flowing."
        )
        return await _state(request, device_id, session)
    first = body.answer in ("discharging", "importing")
    flag = (raw > 0) == first
    proof, _ = await _balance_proof(request, device_id)
    proven = proof.battery_discharge_positive if body.kind == "battery" else proof.grid_import_positive
    if proven is not None and proven != flag:
        session.messages[body.kind] = (
            "The inverter's own power balance points the other way, so your answer was not used and nothing "
            "was saved. Look at the display again; if the readings stay inconsistent, check the inverter in its own app."
        )
        return await _state(request, device_id, session)
    _establish(session, body.kind, flag, raw, "operator" if proven is None else "both")
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
    evidence = f"control test: battery {baseline:.0f} -> {last:.0f} W under a 0 W hold, read back, restored"
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
        f"Guided: battery sign by {session.battery_evidence}; grid sign by {session.grid_evidence}; "
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
