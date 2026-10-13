#!/usr/bin/env python3
#
# app/admin/energy_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Admin API for the Energy Manager (design 2.8).

A session-cookie GUI holds no bearer token and therefore cannot call ``/api/v1``, so the same
service is exposed here behind the admin session/CSRF guard, excluded from the OpenAPI schema. This
is also the only surface where the raw gate detail (``CapabilityName`` values, the reject detail) and
the per-device SoC-target derivation policy may appear: the public contract carries business
semantics only.

No business logic lives here. Every decision is taken by ``EnergyManager`` or by the dispatch port;
this module only authenticates, validates the body and projects the result. ``app.admin.api`` is
imported read-only through ``app.admin.dispatch_api``, which owns the shared admin helpers.
"""

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.admin.dispatch_api import (
    _ADMIN_ACTOR,
    _conflict,
    _device_or_404,
    _dispatch_or_503,
    _require_admin_read,
    _require_admin_write,
)
from app.api.routers.energy import EnergyCommandBody, EnergyStatusResponse
from app.dispatch.capabilities import CapabilityName, CapabilityRecord, CapabilityStatus
from app.dispatch.controller import CapabilityConflict
from app.dispatch.soc_policy import NOTE_MAX_LENGTH, SocTargetMode, SocTargetPolicy
from app.energy.base import EnergyAdminPort
from app.energy.models import ADMIN_ACTOR, EnergyAction, EnergyCommand, EnergyMode
from app.errors import DeviceApiError
from app.gateway.base import DeviceState
from app.optimizer.models import SolarForecast
from app.spotmarket.base import SpotMarketError
from app.spotmarket.models import (
    ENERGY_CHARTS_CURRENCY,
    FeeRegion,
    PriceUnit,
    SpotMarketConfig,
    SpotMarketProvider,
    SpotPriceSnapshot,
)

router = APIRouter(prefix="/admin/api/energy", include_in_schema=False)
log = logging.getLogger(__name__)


def _require_admin_sensitive_read(request: Request) -> dict | None:
    # The energy device listing exposes the write allowlist (approved/required_write_names), the
    # raw CapabilityName values, reject_detail and verification evidence. That is sensitive like
    # the settings read, so it needs read/write and is denied to a read-only PAT (SEC-04).
    from app.admin.api import require_admin

    return require_admin(request, sensitive=True)


def _energy_or_503(request: Request) -> EnergyAdminPort:
    """The manager. Present whenever an admin store exists, so it can answer a command with a
    proper refusal instead of a missing attribute — the refusal codes come from the manager.
    """
    runtime = request.app.state.runtime
    if runtime.energy is None:
        raise HTTPException(503, "The Energy Manager is not configured")
    return runtime.energy


class ModeBody(BaseModel):
    """The mode exists on the admin surface only: the public API cannot switch a device on."""

    model_config = ConfigDict(extra="forbid")

    mode: EnergyMode  # an unknown value is a 422, never a guess


class GateView(BaseModel):
    """One ``GateDecision``, field for field. ``unverified`` carries the raw capability names: this
    is the admin surface, which is exactly where they are allowed to appear.
    """

    action: EnergyAction
    allowed: bool
    engineering_mode: bool  # "active": released THROUGH the per-device switch
    unverified: list[str]
    reject_detail: str | None


class SocTargetPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: SocTargetMode
    below_margin_percent: float = Field(5.0, ge=0, le=50, allow_inf_nan=False)
    note: str | None = Field(None, max_length=NOTE_MAX_LENGTH)


class SocTargetPolicyView(BaseModel):
    mode: SocTargetMode
    below_margin_percent: float
    note: str | None

    @classmethod
    def from_domain(cls, policy: SocTargetPolicy) -> "SocTargetPolicyView":
        return cls(
            mode=policy.mode, below_margin_percent=policy.below_margin_percent, note=policy.note
        )


class LimitsView(BaseModel):
    max_charge_power_w: float
    max_discharge_power_w: float
    engineering_mode: bool


class CapabilityView(BaseModel):
    """One capability row for the Advanced section: status and the evidence the form prefills."""

    name: str
    status: CapabilityStatus
    verified_device_model: str | None
    verified_firmware: str | None
    soc_strategy_external_code: int | None
    enum_byte_width: int | None
    bool_byte_width: int | None
    battery_discharge_positive: bool
    grid_import_positive: bool
    write_frame_layout_verified: bool
    apply_sequence_verified: bool
    note: str | None


class HardwareVerificationBody(BaseModel):
    """Everything one hardware verification attests, so it is applied or refused as a whole."""

    model_config = ConfigDict(extra="forbid")

    verified_device_model: str = Field(min_length=1, max_length=128)
    verified_firmware: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=NOTE_MAX_LENGTH)
    soc_strategy_external_code: int = Field(ge=0, le=255)
    enum_byte_width: int = Field(ge=1, le=4)
    bool_byte_width: int = Field(ge=1, le=4)
    write_frame_layout_verified: StrictBool
    apply_sequence_verified: StrictBool
    battery_discharge_positive: StrictBool
    grid_import_positive: StrictBool
    soc_target_unit: Literal["ratio", "percent"]


_VERIFIED_CAPABILITIES = (
    CapabilityName.WRITE_PATH,
    CapabilityName.BATTERY_POWER_SIGN,
    CapabilityName.GRID_POWER_SIGN,
)


class AdminEnergyDeviceStatus(EnergyStatusResponse):
    """The public status plus the admin-only blocks the GUI needs (design 2.8)."""

    device_name: str
    mode_recorded: bool
    write_support_enabled: bool  # the global write switch; the checklist's write access needs it
    host: str
    connected: bool  # the inverter answers (state ok or degraded)
    connection_state: str
    gates: list[GateView]
    soc_target_policy: SocTargetPolicyView
    limits: LimitsView | None
    capabilities: list[CapabilityView]
    added_write_names: list[str]  # what mode switches contributed, display only
    approved_write_names: list[str]  # the live write allowlist, state-independent (Setup checklist)
    required_write_names: list[str]  # what the Setup checklist needs approved (single source: backend)
    mode_changed_at: datetime | None
    mode_changed_by: str | None
    restore_attempts: int = 0  # failed automatic restore attempts since the last clean restore
    next_restore_at: datetime | None = None


class HardwareStrategyView(BaseModel):
    device_id: str
    device_name: str
    code: int | None
    label: str | None
    stale: bool = False


@router.get("/hardware-strategies")
async def hardware_strategies(
    request: Request, admin: Annotated[dict | None, Depends(_require_admin_read)]
) -> list[HardwareStrategyView]:
    del admin
    runtime = request.app.state.runtime
    result = []
    for device_id, device in sorted(runtime.devices.items()):
        code = None
        label = None
        stale = False
        if runtime.catalog.exists("power_mng_soc_strategy"):
            try:
                # The gateway serves fresh cache entries and coalesces concurrent misses.
                reading = await runtime.gateway.read_metric(device_id, "power_mng_soc_strategy", fresh=False)
                if isinstance(reading.value, int) and not isinstance(reading.value, bool):
                    code = reading.value
                    label = reading.enum_label
                    stale = reading.stale
            except DeviceApiError:
                pass
        result.append(HardwareStrategyView(
            device_id=device_id,
            device_name=device.display_name or runtime.gateway.reported_name(device_id) or device_id,
            code=code,
            label=label,
            stale=stale,
        ))
    return result


async def _admin_status(request: Request, device_id: str) -> AdminEnergyDeviceStatus:
    energy = _energy_or_503(request)
    dispatch = _dispatch_or_503(request)
    public = EnergyStatusResponse.from_domain(await energy.status(device_id))
    record = energy.mode_record(device_id)
    runtime = request.app.state.runtime
    limits = dispatch.device_limits(device_id)
    restore_attempts, next_restore_at = await dispatch.restore_retry_info(device_id)
    device = runtime.devices.get(device_id)
    if device is None:  # removed by a reconfiguration while this request was awaiting
        raise HTTPException(404, "Unknown device")
    state = runtime.gateway.device_status(device_id).state
    return AdminEnergyDeviceStatus(
        **public.model_dump(),
        mode_recorded=energy.mode_recorded(device_id),
        write_support_enabled=runtime.settings.enable_write_support,
        device_name=device.display_name or runtime.gateway.reported_name(device_id) or device_id,
        host=device.host,
        connected=state in (DeviceState.OK, DeviceState.DEGRADED),
        connection_state=state.value,
        gates=[
            GateView(
                action=action,
                allowed=decision.allowed,
                engineering_mode=decision.engineering_mode,
                unverified=[name.value for name in decision.unverified],
                reject_detail=decision.reject_detail,
            )
            for action, decision in energy.gate_decisions(device_id)
        ],
        soc_target_policy=SocTargetPolicyView.from_domain(dispatch.soc_target_policy(device_id)),
        limits=None
        if limits is None
        else LimitsView(
            max_charge_power_w=limits.max_charge_power_w,
            max_discharge_power_w=limits.max_discharge_power_w,
            engineering_mode=limits.engineering_mode,
        ),
        capabilities=[
            CapabilityView(
                name=record.name.value,
                status=record.status,
                verified_device_model=record.verified_device_model,
                verified_firmware=record.verified_firmware,
                soc_strategy_external_code=record.soc_strategy_external_code,
                enum_byte_width=record.enum_byte_width,
                bool_byte_width=record.bool_byte_width,
                battery_discharge_positive=record.battery_discharge_positive,
                grid_import_positive=record.grid_import_positive,
                write_frame_layout_verified=record.write_frame_layout_verified,
                apply_sequence_verified=record.apply_sequence_verified,
                note=record.note,
            )
            for record in dispatch.capabilities(device_id)
        ],
        added_write_names=list(record.added_write_names),
        approved_write_names=list(await energy.approved_write_names()),
        required_write_names=list(energy.required_write_names()),
        mode_changed_at=record.changed_at,
        mode_changed_by=record.changed_by,
        restore_attempts=restore_attempts,
        next_restore_at=next_restore_at,
    )


@router.get("/devices")
async def list_devices(
    request: Request, admin: Annotated[dict | None, Depends(_require_admin_sensitive_read)]
) -> list[AdminEnergyDeviceStatus]:
    """One entry per configured device, readings included, so the GUI needs one poll per cycle."""
    del admin
    result = []
    for device_id in sorted(request.app.state.runtime.devices):  # sorted() copies: safe across the awaits
        try:
            result.append(await _admin_status(request, device_id))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            # removed by a concurrent reconfiguration: leave it out of the listing
    return result


async def _status_after_action(request: Request, device_id: str) -> AdminEnergyDeviceStatus:
    """The action already happened: a failing status projection must not turn it into a 500.

    The caller could retry a hardware action that did run, so the success is reported anyway.
    """
    try:
        return await _admin_status(request, device_id)
    except Exception:
        log.exception("Status projection failed after an executed energy action (device=%s)", device_id)
        return JSONResponse(  # type: ignore[return-value]
            {"executed": True, "status_available": False, "device_id": device_id}, status_code=202
        )


@router.post("/devices/{device_id}/command")
async def post_command(
    request: Request,
    device_id: str,
    body: EnergyCommandBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AdminEnergyDeviceStatus:
    del admin
    _device_or_404(request, device_id)
    runtime = request.app.state.runtime
    runtime.ensure_accepting()
    energy = _energy_or_503(request)
    await energy.command(
        device_id,
        EnergyCommand(body.action, body.target_soc_percent, body.max_power_w),
        actor=ADMIN_ACTOR,
    )
    return await _status_after_action(request, device_id)


@router.put("/devices/{device_id}/mode")
async def put_mode(
    request: Request,
    device_id: str,
    body: ModeBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AdminEnergyDeviceStatus:
    del admin
    _device_or_404(request, device_id)
    runtime = request.app.state.runtime
    runtime.ensure_accepting()
    energy = _energy_or_503(request)
    await energy.set_mode(device_id, body.mode, actor=ADMIN_ACTOR)
    return await _status_after_action(request, device_id)


def verification_records(
    dispatch, device_id: str, body: HardwareVerificationBody, now: datetime
) -> list[CapabilityRecord]:
    """The three records one hardware verification writes; shared by the Expert form and the
    guided assistant so both produce exactly the same attestation.
    """
    # A sign record's note is evidence this endpoint has no input field for; carry it over instead
    # of dropping it when the records are rewritten. body.note documents the write path only.
    # Carried only when it cannot be mis-attributed: either it was never stamped under a specific
    # model/firmware (a bare note, nothing to contradict), or it was stamped under the SAME
    # model/firmware this verification attests again. A note recorded under a different hardware
    # identity (e.g. firmware 1.0) must not be re-stamped as if observed under this one (firmware
    # 2.0) — it is dropped instead of silently following the new attestation.
    kept_notes = {
        record.name: record.note
        for record in dispatch.capabilities(device_id)
        if (record.verified_device_model is None and record.verified_firmware is None)
        or (record.verified_device_model, record.verified_firmware)
        == (body.verified_device_model, body.verified_firmware)
    }
    stamp = {
        "status": CapabilityStatus.VERIFIED,
        "verified_device_model": body.verified_device_model,
        "verified_firmware": body.verified_firmware,
        "verified_at": now,
        "verified_by": _ADMIN_ACTOR,
    }
    return [
        CapabilityRecord(
            device_id=device_id,
            name=CapabilityName.WRITE_PATH,
            soc_strategy_external_code=body.soc_strategy_external_code,
            enum_byte_width=body.enum_byte_width,
            bool_byte_width=body.bool_byte_width,
            write_frame_layout_verified=body.write_frame_layout_verified,
            apply_sequence_verified=body.apply_sequence_verified,
            soc_target_unit=body.soc_target_unit,
            note=body.note,
            **stamp,
        ),
        CapabilityRecord(
            device_id=device_id,
            name=CapabilityName.BATTERY_POWER_SIGN,
            battery_discharge_positive=body.battery_discharge_positive,
            note=kept_notes.get(CapabilityName.BATTERY_POWER_SIGN),
            **stamp,
        ),
        CapabilityRecord(
            device_id=device_id,
            name=CapabilityName.GRID_POWER_SIGN,
            grid_import_positive=body.grid_import_positive,
            note=kept_notes.get(CapabilityName.GRID_POWER_SIGN),
            **stamp,
        ),
    ]


@router.put("/devices/{device_id}/hardware-verification")
async def put_hardware_verification(
    request: Request,
    device_id: str,
    body: HardwareVerificationBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AdminEnergyDeviceStatus:
    """Verify the write path and both sign conventions in one transaction (all or none)."""
    del admin
    _device_or_404(request, device_id)
    dispatch = _dispatch_or_503(request)
    missing = [
        flag for flag in ("write_frame_layout_verified", "apply_sequence_verified") if not getattr(body, flag)
    ]
    if not body.note.strip():
        missing.append("note")
    if missing:
        raise HTTPException(400, f"missing evidence for write_path_convention: {', '.join(missing)}")
    now = request.app.state.runtime.clock.now()
    try:
        records = verification_records(dispatch, device_id, body, now)
        await dispatch.set_capabilities(device_id, records)
    except CapabilityConflict as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log.warning(
        "Energy hardware verification saved: device=%s model=%s firmware=%s admin=%s",
        device_id, body.verified_device_model, body.verified_firmware, _ADMIN_ACTOR,
    )  # fmt: skip
    return await _admin_status(request, device_id)


@router.delete("/devices/{device_id}/hardware-verification")
async def delete_hardware_verification(
    request: Request,
    device_id: str,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> AdminEnergyDeviceStatus:
    """Revoke the verification: only the status changes, the recorded evidence stays."""
    del admin
    _device_or_404(request, device_id)
    dispatch = _dispatch_or_503(request)
    current = {record.name: record for record in dispatch.capabilities(device_id)}
    revoked = [
        replace(current[name], status=CapabilityStatus.UNVERIFIED, verified_at=None, verified_by=None)
        for name in _VERIFIED_CAPABILITIES
    ]
    try:
        await dispatch.set_capabilities(device_id, revoked)
    except CapabilityConflict as exc:
        raise _conflict(exc) from exc
    log.warning("Energy hardware verification revoked: device=%s admin=%s", device_id, _ADMIN_ACTOR)
    return await _admin_status(request, device_id)


@router.get("/devices/{device_id}/soc-target-policy")
def get_soc_target_policy(
    request: Request, device_id: str, admin: Annotated[dict | None, Depends(_require_admin_read)]
) -> SocTargetPolicyView:
    del admin
    _device_or_404(request, device_id)
    return SocTargetPolicyView.from_domain(_dispatch_or_503(request).soc_target_policy(device_id))


@router.put("/devices/{device_id}/soc-target-policy")
async def put_soc_target_policy(
    request: Request,
    device_id: str,
    body: SocTargetPolicyBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> SocTargetPolicyView:
    del admin
    _device_or_404(request, device_id)
    dispatch = _dispatch_or_503(request)
    try:
        policy = SocTargetPolicy(
            device_id=device_id,
            mode=body.mode,
            below_margin_percent=body.below_margin_percent,
            note=body.note,
        )
        await dispatch.set_soc_target_policy(device_id, policy)
    except CapabilityConflict as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log.warning(
        "Energy SoC target policy updated: device=%s mode=%s margin=%s admin=%s",
        device_id, policy.mode.value, policy.below_margin_percent, _ADMIN_ACTOR,
    )  # fmt: skip
    return SocTargetPolicyView.from_domain(policy)


#
# A separate configuration domain next to the inverter control above: the spot-market provider is a
# read-only data source for a future tariff engine and never writes a register. These routes only
# read/write the persisted configuration and run a one-off preview fetch; they touch neither the
# EnergyManager nor the dispatch layer, so an external API failure cannot affect inverter control.


def _spotmarket_store(request: Request):
    store = getattr(request.app.state, "admin_store", None)
    if store is None:
        raise HTTPException(503, "Administration is unavailable")
    return store


class FeeRegionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field("", max_length=64)
    label: str = Field("", max_length=128)


class SpotMarketConfigBody(BaseModel):
    """The spot-market configuration a save accepts. Validated again by ``SpotMarketConfig`` so the
    stored form and the in-process form share one validation contract.

    There is no ``api_url`` field on purpose: the provider owns its fixed trusted endpoint, so the
    operator chooses only the provider and the zone, never a network target (SSRF). There is no
    ``currency`` or ``price_unit`` either: both are provider metadata determined from the API
    response, not operator input, so the snapshot can never claim a unit the source did not deliver.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    provider: SpotMarketProvider
    bidding_zone: str = Field(max_length=32)
    fee_region: FeeRegionBody = Field(default_factory=FeeRegionBody)


class FeeRegionView(BaseModel):
    code: str
    label: str

    @classmethod
    def from_domain(cls, region: FeeRegion) -> "FeeRegionView":
        return cls(code=region.code, label=region.label)


class SpotMarketConfigView(BaseModel):
    """The saved configuration plus the provider-owned, read-only currency and source unit.

    ``currency`` and ``price_unit`` are not operator input (Finding 2): they describe what the
    provider delivers, so the GUI shows them read-only rather than offering an editable field. The
    ``fee_region`` concept stays in the model and the view even though the GUI no longer edits it.
    """

    enabled: bool
    provider: SpotMarketProvider
    bidding_zone: str
    fee_region: FeeRegionView
    currency: str  # provider metadata, read-only in the UI
    price_unit: PriceUnit  # the source unit the provider reports, read-only in the UI

    @classmethod
    def from_domain(cls, config: SpotMarketConfig) -> "SpotMarketConfigView":
        return cls(
            enabled=config.enabled,
            provider=config.provider,
            bidding_zone=config.bidding_zone,
            fee_region=FeeRegionView.from_domain(config.fee_region),
            currency=ENERGY_CHARTS_CURRENCY,
            price_unit=PriceUnit.EUR_PER_MWH,
        )


class BiddingZonesView(BaseModel):
    """The bidding-zone choices for the form: the accepted zones, the provider default, whether the
    list is stale (a refresh failed and the last good list is shown), and when it was last fetched.
    """

    zones: list[str]
    default: str
    stale: bool
    last_update: datetime


class SpotMarketIntervalView(BaseModel):
    start: datetime
    end: datetime
    price_eur_per_kwh: float


class SpotMarketPreviewInterval(BaseModel):
    start: datetime
    end: datetime
    price_eur_per_kwh: float


class SpotMarketPreviewView(BaseModel):
    """A connection/preview result for the Spot Market tab and its three-day chart.

    ``status`` is ``connected`` or ``error``; on error ``message`` names the cause and the price
    fields stay ``None``. The optional min/max figures summarise tomorrow's series; yesterday's,
    today's, and tomorrow's intervals are preview data only, with no storage or battery decision.
    """

    provider: SpotMarketProvider
    bidding_zone: str
    currency: str
    status: str
    message: str | None = None
    last_update: datetime | None = None
    intervals_available: int = 0
    current_price_eur_per_kwh: float | None = None
    min_price_eur_per_kwh: float | None = None
    max_price_eur_per_kwh: float | None = None
    min_price_at: datetime | None = None
    max_price_at: datetime | None = None
    yesterday_intervals: list[SpotMarketPreviewInterval] = Field(default_factory=list)
    today_intervals: list[SpotMarketPreviewInterval] = Field(default_factory=list)
    next_day_intervals: list[SpotMarketPreviewInterval] = Field(default_factory=list)
    yesterday_message: str | None = None
    next_day_message: str | None = None
    timezone: str | None = None


def _preview_from_snapshot(
    today: SpotPriceSnapshot,
    yesterday: SpotPriceSnapshot | None,
    next_day: SpotPriceSnapshot | None,
    yesterday_message: str | None = None,
    next_day_message: str | None = None,
) -> SpotMarketPreviewView:
    snapshot = next_day or today
    intervals = next_day.intervals if next_day else ()
    view = SpotMarketPreviewView(
        provider=snapshot.provider,
        bidding_zone=snapshot.bidding_zone,
        currency=snapshot.currency,
        status="connected",
        last_update=snapshot.fetched_at,
        intervals_available=len(intervals),
        yesterday_intervals=[
            SpotMarketPreviewInterval(
                start=item.start,
                end=item.end,
                price_eur_per_kwh=float(item.price_eur_per_kwh),
            )
            for item in yesterday.intervals
        ]
        if yesterday
        else [],
        today_intervals=[
            SpotMarketPreviewInterval(
                start=item.start,
                end=item.end,
                price_eur_per_kwh=float(item.price_eur_per_kwh),
            )
            for item in today.intervals
        ],
        next_day_intervals=[
            SpotMarketPreviewInterval(
                start=item.start,
                end=item.end,
                price_eur_per_kwh=float(item.price_eur_per_kwh),
            )
            for item in next_day.intervals
        ]
        if next_day
        else [],
        yesterday_message=yesterday_message,
        next_day_message=next_day_message,
        timezone=today.timezone,
    )
    if not intervals:
        return view
    lowest = min(intervals, key=lambda item: item.price_eur_per_kwh)
    highest = max(intervals, key=lambda item: item.price_eur_per_kwh)
    now = snapshot.fetched_at
    current = next((item for item in intervals if item.start <= now < item.end), None)
    view.min_price_eur_per_kwh = float(lowest.price_eur_per_kwh)
    view.max_price_eur_per_kwh = float(highest.price_eur_per_kwh)
    view.min_price_at = lowest.start
    view.max_price_at = highest.start
    if current is not None:
        view.current_price_eur_per_kwh = float(current.price_eur_per_kwh)
    return view


def _spot_dashboard_view(
    config: SpotMarketConfig, snapshot: SpotPriceSnapshot | None, failed: bool,
    current_snapshot: SpotPriceSnapshot | None, current_failed: bool, now: datetime,
) -> dict:
    """Project only operator-facing prices and freshness, never provider payloads."""
    base = {
        "status": "disabled" if not config.enabled else "error" if failed else "unavailable",
        "bidding_zone": config.bidding_zone,
        "currency": ENERGY_CHARTS_CURRENCY,
        "fetched_at": None,
        "timezone": None,
        "source_generated_at": None,
        "available_from": None,
        "available_until": None,
        "current_valid_until": None,
        "age_seconds": None,
        "current_price_ct_per_kwh": None,
        "current_status": "disabled" if not config.enabled else "unavailable",
        "current_fetched_at": None,
        "intervals_available": 0,
        "future_curve": [],
    }
    current = next((item for item in current_snapshot.intervals if item.start <= now < item.end), None) if current_snapshot else None
    if current_snapshot is not None:
        expired = (now >= current_snapshot.valid_until) if current_snapshot.valid_until else (
            (now - current_snapshot.fetched_at).total_seconds() > 20 * 60
        )
        base["current_fetched_at"] = current_snapshot.fetched_at.isoformat()
        base["current_valid_until"] = current_snapshot.valid_until.isoformat() if current_snapshot.valid_until else None
        base["current_status"] = "stale" if expired or current_failed or current is None else "available"
        if current is not None and not expired:
            base["current_price_ct_per_kwh"] = float(current.price_eur_per_kwh * 100)
    elif current_failed and config.enabled:
        base["current_status"] = "error"
    if snapshot is None:
        return base
    age = max(0, (now - snapshot.fetched_at).total_seconds())
    future = [item for item in snapshot.intervals if item.start >= now]
    stale = failed or age > 6 * 3600 or (
        snapshot.available_until is not None and now >= snapshot.available_until
    ) or not future
    base.update(
        status="stale" if stale else "available",
        bidding_zone=snapshot.bidding_zone,
        currency=snapshot.currency,
        fetched_at=snapshot.fetched_at.isoformat(),
        timezone=snapshot.timezone,
        source_generated_at=snapshot.source_generated_at.isoformat() if snapshot.source_generated_at else None,
        available_from=snapshot.available_from.isoformat() if snapshot.available_from else None,
        available_until=snapshot.available_until.isoformat() if snapshot.available_until else None,
        age_seconds=round(age),
        intervals_available=len(future),
        future_curve=[
            {"start": item.start.isoformat(), "end": item.end.isoformat(),
             "price_ct_per_kwh": float(item.price_eur_per_kwh * 100)}
            for item in future
        ],
    )
    return base


def _solar_dashboard_view(forecast: SolarForecast | None, now: datetime) -> dict:
    base = {"status": "unavailable", "created_at": None, "age_seconds": None,
            "covered_until": None, "future_curve": []}
    if forecast is None:
        return base
    future = [item for item in forecast.intervals if item.end > now]
    age = max(0, (now - forecast.created_at).total_seconds())
    stale = age > 6 * 3600 or not future
    base.update(
        status="stale" if stale else "available",
        created_at=forecast.created_at.isoformat(),
        age_seconds=round(age),
        covered_until=future[-1].end.isoformat() if future else None,
        future_curve=[
            {"start": item.start.isoformat(), "end": item.end.isoformat(),
             "power_kw": item.energy_wh / (item.end - item.start).total_seconds() * 3.6}
            for item in future
        ] if not stale else [],
    )
    return base


@router.get("/dashboard-forecast")
async def dashboard_forecast(
    request: Request, admin: Annotated[dict | None, Depends(_require_admin_read)],
    include: Annotated[list[Literal["spot_current", "spot_day_ahead", "solar"]], Query(min_length=1, max_length=3)],
) -> dict:
    """Project only requested forecast data; a browser never calls an external provider."""
    del admin
    if len(set(include)) != len(include):
        raise HTTPException(422, "Duplicate forecast selection")
    now = datetime.now(UTC)
    config = request.app.state.spotmarket_config
    requested = set(include)
    snapshot, failed = (None, False)
    current, current_failed = (None, False)
    if "spot_day_ahead" in requested:
        snapshot, failed = await asyncio.to_thread(request.app.state.spotmarket_snapshot_cache.get, config)
    if "spot_current" in requested:
        current, current_failed = await asyncio.to_thread(request.app.state.spotmarket_current_cache.get, config)
    result = {}
    if requested & {"spot_current", "spot_day_ahead"}:
        result["spot"] = _spot_dashboard_view(config, snapshot, failed, current, current_failed, now)
    if "solar" in requested:
        result["solar"] = _solar_dashboard_view(getattr(request.app.state, "solar_forecast", None), now)
    return result


@router.get("/spot-market/config")
def get_spot_market_config(
    request: Request, admin: Annotated[dict | None, Depends(_require_admin_read)]
) -> SpotMarketConfigView:
    del admin
    config = getattr(request.app.state, "spotmarket_config", None) or SpotMarketConfig()
    return SpotMarketConfigView.from_domain(config)


@router.get("/spot-market/bidding-zones")
async def get_spot_market_bidding_zones(
    request: Request, admin: Annotated[dict | None, Depends(_require_admin_read)]
) -> BiddingZonesView:
    """The accepted bidding zones for the form select, read cache-first from provider metadata.

    The cache serves the last good list without a network call while it is fresh, so opening the
    form does not hit the rate-limited API every time; a failed refresh returns the last list
    flagged ``stale``. A provider failure with no cached list at all is reported as an error status,
    isolated from inverter control.
    """
    del admin
    cache = getattr(request.app.state, "spotmarket_zone_cache", None)
    if cache is None:
        raise HTTPException(503, "The spot-market provider is not configured")
    try:
        listing = await asyncio.to_thread(cache.get)
    except SpotMarketError as exc:
        raise HTTPException(502, f"Bidding zones are unavailable: {exc}") from exc
    return BiddingZonesView(
        zones=list(listing.zones),
        default=listing.default,
        stale=listing.stale,
        last_update=listing.last_update,
    )


@router.put("/spot-market/config")
def put_spot_market_config(
    request: Request,
    body: SpotMarketConfigBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> SpotMarketConfigView:
    del admin
    try:
        config = SpotMarketConfig.model_validate(body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, "Invalid spot-market configuration") from exc
    _spotmarket_store(request).put("spotmarket_config", config.model_dump(mode="json"))
    request.app.state.spotmarket_config = config
    log.info(
        "Spot-market configuration saved: provider=%s zone=%s enabled=%s admin=%s",
        config.provider.value, config.bidding_zone, config.enabled, _ADMIN_ACTOR,
    )  # fmt: skip
    return SpotMarketConfigView.from_domain(config)


@router.post("/spot-market/preview")
async def preview_spot_market(
    request: Request,
    body: SpotMarketConfigBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> SpotMarketPreviewView:
    """Fetch a one-off yesterday/today/tomorrow price preview with the submitted configuration.

    Uses the posted (possibly unsaved) configuration so the operator can test before saving. The
    provider is read-only and its failures are reported as ``status=error``; nothing is persisted
    and no battery action is taken.
    """
    del admin
    try:
        config = SpotMarketConfig.model_validate(body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, "Invalid spot-market configuration") from exc
    provider = request.app.state.spotmarket_provider
    try:
        today = await asyncio.to_thread(provider.fetch_today, config)
    except SpotMarketError as exc:
        log.info("Spot-market preview failed: provider=%s zone=%s", config.provider.value, config.bidding_zone)
        return SpotMarketPreviewView(
            provider=config.provider,
            bidding_zone=config.bidding_zone,
            # No snapshot on failure: report the provider's fixed currency, not operator input.
            currency=ENERGY_CHARTS_CURRENCY,
            status="error",
            message=str(exc),
        )
    results = await asyncio.gather(
        asyncio.to_thread(provider.fetch_yesterday, config, today.timezone),
        asyncio.to_thread(provider.fetch_day_ahead, config),
        return_exceptions=True,
    )
    yesterday, next_day = results
    if isinstance(yesterday, BaseException) and not isinstance(yesterday, SpotMarketError):
        raise yesterday
    if isinstance(next_day, BaseException) and not isinstance(next_day, SpotMarketError):
        raise next_day
    yesterday_message = str(yesterday) if isinstance(yesterday, SpotMarketError) else None
    next_day_message = str(next_day) if isinstance(next_day, SpotMarketError) else None
    return _preview_from_snapshot(
        today,
        None if isinstance(yesterday, BaseException) else yesterday,
        None if isinstance(next_day, BaseException) else next_day,
        yesterday_message,
        next_day_message,
    )
