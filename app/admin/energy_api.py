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
this module authenticates, validates the body and projects the result. The battery diagnostic view
reuses ``app.admin.api``'s stabilized module report without changing its trust rules.
"""

import asyncio
import logging
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from app.admin.api import (
    _BATTERY_TOWER_PREFIXES,
    _battery_module_report,
    _battery_tower_present,
    _csrf,
    require_admin,
)
from app.admin.consumption_backfill import WINDOW_DAYS as CONSUMPTION_WINDOW_DAYS
from app.admin.consumption_backfill import load_daily_consumption, resolve_zone
from app.admin.dispatch_api import (
    _ADMIN_ACTOR,
    _conflict,
    _device_or_404,
    _dispatch_or_503,
    _require_admin_read,
    _require_admin_write,
)
from app.admin.forecast_refresh import (
    SOLAR_STATUS_KEY,
    SPOT_STATUS_KEY,
    load_refresh_status,
    local_three_day_window,
    solar_config_key,
    solar_refresh_plan,
    spot_config_key,
    spot_refresh_plan,
)
from app.api.routers.energy import EnergyCommandBody, EnergyStatusResponse
from app.catalog.base import is_numeric
from app.dispatch.battery_facts import resolve_device_battery_facts
from app.dispatch.capabilities import (
    REGISTER_BYTE_WIDTH_MAX,
    REGISTER_BYTE_WIDTH_MIN,
    SOC_STRATEGY_CODE_MAX,
    CapabilityName,
    CapabilityRecord,
    CapabilityStatus,
)
from app.dispatch.controller import CapabilityConflict
from app.dispatch.models import DeviceLimits
from app.dispatch.soc_policy import (
    BELOW_MARGIN_PERCENT_MAX,
    NOTE_MAX_LENGTH,
    SocTargetMode,
    SocTargetPolicy,
)
from app.dispatch.store import DispatchStore
from app.energy.base import EnergyAdminPort
from app.energy.models import ADMIN_ACTOR, EnergyAction, EnergyCommand, EnergyMode
from app.energy.soc_window import effective_target_window
from app.errors import DeviceApiError, DeviceUnreachable
from app.gateway.base import DeviceState
from app.gateway.cache import CacheFreshness
from app.gateway.rct_history import MIN_COMMISSIONING_DATE
from app.limits import (
    MAX_POWER_W,
    MAX_USABLE_CAPACITY_WH,
    TARGET_SOC_MAX_PERCENT,
    TARGET_SOC_MIN_PERCENT,
)
from app.optimizer.adapters import (
    DryRunInputs,
    EnergyReadingsBatteryStateProvider,
    ForecastBasis,
    ForecastConfidence,
    PlanStatus,
    StoredConsumptionForecastProvider,
    build_dry_run_plan,
    solar_forecast_from_rows,
    spot_snapshot_from_rows,
)
from app.optimizer.models import (
    EnergyPlan,
    PlanPolicy,
    SolarForecast,
    SolarForecastInterval,
)
from app.optimizer.planner import PlanParameters
from app.optimizer.replan import DEFAULT_REPLAN_THRESHOLDS, TRIGGER_DESCRIPTIONS
from app.spotmarket.base import SpotMarketError
from app.spotmarket.models import (
    ENERGY_CHARTS_CURRENCY,
    FeeRegion,
    PriceUnit,
    SpotMarketConfig,
    SpotMarketProvider,
    SpotPriceInterval,
    SpotPriceSnapshot,
)
from app.weather.base import WeatherError
from app.weather.geocoding import (
    MAX_QUERY_LENGTH,
    MAX_RESULTS,
    MIN_QUERY_LENGTH,
    GeocodingResult,
)
from app.weather.models import (
    _FORECAST_REQUIRED_FIELDS,
    MAX_LOCATION_NAME_LENGTH,
    WeatherConfig,
    WeatherProvider,
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
    below_margin_percent: float = Field(
        5.0, ge=0, le=BELOW_MARGIN_PERCENT_MAX, allow_inf_nan=False
    )
    note: str | None = Field(None, max_length=NOTE_MAX_LENGTH)


class SocTargetPolicyView(BaseModel):
    mode: SocTargetMode
    below_margin_percent: float
    note: str | None

    @classmethod
    def from_domain(cls, policy: SocTargetPolicy) -> "SocTargetPolicyView":
        return cls(
            mode=policy.mode,
            below_margin_percent=policy.below_margin_percent,
            note=policy.note,
        )


class LimitsView(BaseModel):
    max_charge_power_w: float
    max_discharge_power_w: float
    engineering_mode: bool
    usable_capacity_wh: float | None = None


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
    soc_strategy_external_code: int = Field(ge=0, le=SOC_STRATEGY_CODE_MAX)
    enum_byte_width: int = Field(ge=REGISTER_BYTE_WIDTH_MIN, le=REGISTER_BYTE_WIDTH_MAX)
    bool_byte_width: int = Field(ge=REGISTER_BYTE_WIDTH_MIN, le=REGISTER_BYTE_WIDTH_MAX)
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
    write_support_enabled: (
        bool  # the global write switch; the checklist's write access needs it
    )
    host: str
    connected: bool  # the inverter answers (state ok or degraded)
    connection_state: str
    gates: list[GateView]
    soc_target_policy: SocTargetPolicyView
    limits: LimitsView | None
    capabilities: list[CapabilityView]
    added_write_names: list[str]  # what mode switches contributed, display only
    approved_write_names: list[
        str
    ]  # the live write allowlist, state-independent (Setup checklist)
    required_write_names: list[
        str
    ]  # what the Setup checklist needs approved (single source: backend)
    mode_changed_at: datetime | None
    mode_changed_by: str | None
    restore_attempts: int = (
        0  # failed automatic restore attempts since the last clean restore
    )
    next_restore_at: datetime | None = None


class HardwareStrategyView(BaseModel):
    device_id: str
    device_name: str
    host: str
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
                # Page loading must never create an inverter transaction. Missing or stale cache is
                # reported as n/a; an explicit mode/action path may perform device I/O when needed.
                reading = runtime.gateway.cached_metric(
                    device_id, "power_mng_soc_strategy"
                )
                if (
                    reading is not None
                    and isinstance(reading.value, int)
                    and not isinstance(reading.value, bool)
                ):
                    code = reading.value
                    label = reading.enum_label
                    stale = reading.stale
            except DeviceApiError:
                pass
        result.append(
            HardwareStrategyView(
                device_id=device_id,
                device_name=device.display_name
                or runtime.gateway.reported_name(device_id)
                or device_id,
                host=device.host,
                code=code,
                label=label,
                stale=stale,
            )
        )
    return result


def _is_connected(state: DeviceState) -> bool:
    """Only a heartbeat-proven device is connected; DEGRADED already missed a heartbeat (the
    dashboard shows it as offline too), so the Energy page must not call it connected.
    """
    return state is DeviceState.OK


def _require_connected(request: Request, device_id: str) -> None:
    """Fail fast: a device command to an unreachable inverter would only wait for the timeout."""
    if not _is_connected(
        request.app.state.runtime.gateway.device_status(device_id).state
    ):
        raise DeviceUnreachable(device_id=device_id)


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
        device_name=device.display_name
        or runtime.gateway.reported_name(device_id)
        or device_id,
        host=device.host,
        connected=_is_connected(state),
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
        soc_target_policy=SocTargetPolicyView.from_domain(
            dispatch.soc_target_policy(device_id)
        ),
        limits=(
            None
            if limits is None
            else LimitsView(
                max_charge_power_w=limits.max_charge_power_w,
                max_discharge_power_w=limits.max_discharge_power_w,
                engineering_mode=limits.engineering_mode,
                usable_capacity_wh=limits.usable_capacity_wh,
            )
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
    request: Request,
    admin: Annotated[dict | None, Depends(_require_admin_sensitive_read)],
) -> list[AdminEnergyDeviceStatus]:
    """One entry per configured device, readings included, so the GUI needs one poll per cycle."""
    del admin
    result = []
    for device_id in sorted(
        request.app.state.runtime.devices
    ):  # sorted() copies: safe across the awaits
        try:
            result.append(await _admin_status(request, device_id))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            # removed by a concurrent reconfiguration: leave it out of the listing
    return result


async def _status_after_action(
    request: Request, device_id: str
) -> AdminEnergyDeviceStatus:
    """The action already happened: a failing status projection must not turn it into a 500.

    The caller could retry a hardware action that did run, so the success is reported anyway.
    """
    try:
        return await _admin_status(request, device_id)
    except Exception:
        log.exception(
            "Status projection failed after an executed energy action (device=%s)",
            device_id,
        )
        return JSONResponse(  # type: ignore[return-value]
            {"executed": True, "status_available": False, "device_id": device_id},
            status_code=202,
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
    _require_connected(request, device_id)
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
    _require_connected(request, device_id)
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
        flag
        for flag in ("write_frame_layout_verified", "apply_sequence_verified")
        if not getattr(body, flag)
    ]
    if not body.note.strip():
        missing.append("note")
    if missing:
        raise HTTPException(
            400, f"missing evidence for write_path_convention: {', '.join(missing)}"
        )
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
        replace(
            current[name],
            status=CapabilityStatus.UNVERIFIED,
            verified_at=None,
            verified_by=None,
        )
        for name in _VERIFIED_CAPABILITIES
    ]
    try:
        await dispatch.set_capabilities(device_id, revoked)
    except CapabilityConflict as exc:
        raise _conflict(exc) from exc
    log.warning(
        "Energy hardware verification revoked: device=%s admin=%s",
        device_id,
        _ADMIN_ACTOR,
    )
    return await _admin_status(request, device_id)


@router.get("/devices/{device_id}/soc-target-policy")
def get_soc_target_policy(
    request: Request,
    device_id: str,
    admin: Annotated[dict | None, Depends(_require_admin_read)],
) -> SocTargetPolicyView:
    del admin
    _device_or_404(request, device_id)
    return SocTargetPolicyView.from_domain(
        _dispatch_or_503(request).soc_target_policy(device_id)
    )


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


def _admin_store_or_503(request: Request):
    """The admin store, or HTTP 503 when administration is not configured.

    Single source for the spot-market, weather and commissioning routes, which all persist through
    the same admin store: the "store present, else 503" rule must have one definition, not one per
    data domain.
    """
    store = getattr(request.app.state, "admin_store", None)
    if store is None:
        raise HTTPException(503, "Administration is unavailable")
    return store


def _signal_forecast_refresh(request: Request, event_name: str) -> None:
    event = getattr(request.app.state, event_name, None)
    loop = getattr(request.app.state, "loop", None)
    if event is not None and loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(event.set)


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

    # ``enabled`` is no longer an operator toggle: the GUI dropped the control and does not send it.
    # The server derives the stored value (a configured spot market is on), and whether prices are
    # actually fetched follows the Energy Manager mode (manual fetches; off/external do not). The
    # field stays accepted-but-ignored so an older client that still sends it is not rejected.
    enabled: StrictBool | None = None
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
    last_update: datetime | None


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

    ``status`` is ``connected``, ``unavailable`` or ``error``. The optional min/max figures
    summarise the best populated day; all three interval lists are read from the rolling SQLite
    price table and never trigger provider calls in a request.
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
    refresh_error: str | None = None
    refresh_error_at: datetime | None = None
    # True only when the daily retrieval failed AND the data that is due now is still missing.
    stale: bool = False


def _preview_from_snapshot(
    today: SpotPriceSnapshot,
    yesterday: SpotPriceSnapshot | None,
    next_day: SpotPriceSnapshot | None,
    yesterday_message: str | None = None,
    next_day_message: str | None = None,
    now: datetime | None = None,
) -> SpotMarketPreviewView:
    snapshot = next_day or today
    # Re-bucket by each interval's OWN local calendar date rather than trusting which fetch call
    # produced it. The provider's "today" endpoint window is not guaranteed to be exactly today's
    # local day, so filing ``today.intervals`` straight into ``today_intervals`` can misplace the
    # whole curve (the "one grey Yesterday line, no green Today, tiles n/a" regression). Comparing
    # each start, converted to the bidding zone's LOCAL timezone, against local_today-1/±0/+1 keeps
    # the series correct regardless of what window the provider returned. No provider request is
    # changed by this; it is purely a projection-side reclassification.
    zone = _preview_zone(today, yesterday, next_day)
    # Anchor "today" on the today snapshot's fetch moment (the preview's "now"), not next_day's, so
    # tomorrow's curve is classified +1 relative to the real current day.
    local_today = (now or today.fetched_at).astimezone(zone).date()

    buckets: dict[int, dict[datetime, SpotPriceInterval]] = {-1: {}, 0: {}, 1: {}}
    # Prefer the dedicated-fetch source when the same start appears twice, in order yesterday →
    # today → next_day, but always file by the interval's actual local date, not its source.
    for source in (yesterday, today, next_day):
        if source is None:
            continue
        for item in source.intervals:
            offset = (item.start.astimezone(zone).date() - local_today).days
            if offset in buckets:
                buckets[offset][item.start] = item

    def _ordered(offset: int) -> list[SpotPriceInterval]:
        return [buckets[offset][start] for start in sorted(buckets[offset])]

    yesterday_series = _ordered(-1)
    today_series = _ordered(0)
    next_day_series = _ordered(1)

    def _projected(items: list[SpotPriceInterval]) -> list[SpotMarketPreviewInterval]:
        return [
            SpotMarketPreviewInterval(
                start=item.start,
                end=item.end,
                price_eur_per_kwh=float(item.price_eur_per_kwh),
            )
            for item in items
        ]

    # The summary figures (Lowest/Highest tiles) must reflect the best available POPULATED series,
    # not only tomorrow: in the normal daily pre-auction state tomorrow is unpublished, but
    # yesterday/today prices exist and the tiles must still show real numbers. Prefer the
    # re-bucketed next_day → today → yesterday.
    summary = next_day_series or today_series or yesterday_series

    view = SpotMarketPreviewView(
        provider=snapshot.provider,
        bidding_zone=snapshot.bidding_zone,
        currency=snapshot.currency,
        status="connected",
        last_update=snapshot.fetched_at,
        # ``intervals_available`` keeps its original meaning: the count the next-day fetch returned.
        # It is derived from the raw next_day snapshot (not the re-bucketed series) so it is unchanged
        # by the date reclassification, and the frontend does not read it.
        intervals_available=len(next_day.intervals) if next_day else 0,
        yesterday_intervals=_projected(yesterday_series),
        today_intervals=_projected(today_series),
        next_day_intervals=_projected(next_day_series),
        yesterday_message=yesterday_message,
        next_day_message=next_day_message,
        timezone=today.timezone,
    )
    if not summary:
        return view
    lowest = min(summary, key=lambda item: item.price_eur_per_kwh)
    highest = max(summary, key=lambda item: item.price_eur_per_kwh)
    current = next(
        (
            item
            for item in summary
            if item.start <= (now or snapshot.fetched_at) < item.end
        ),
        None,
    )
    view.min_price_eur_per_kwh = float(lowest.price_eur_per_kwh)
    view.max_price_eur_per_kwh = float(highest.price_eur_per_kwh)
    view.min_price_at = lowest.start
    view.max_price_at = highest.start
    if current is not None:
        view.current_price_eur_per_kwh = float(current.price_eur_per_kwh)
    return view


def _preview_zone(*snapshots: SpotPriceSnapshot | None) -> ZoneInfo:
    """The bidding zone's local timezone for date bucketing, from the first snapshot that carries a
    valid IANA zone. Degrades to UTC if none is present, so a missing timezone never crashes the
    preview — the series are then bucketed in UTC, which is still internally consistent.
    """
    for snapshot in snapshots:
        if snapshot is not None and snapshot.timezone:
            try:
                return ZoneInfo(snapshot.timezone)
            except (ValueError, ZoneInfoNotFoundError):
                continue
    return ZoneInfo("UTC")


def _refresh_failure(status: dict | None) -> tuple[str | None, datetime | None]:
    """The message and time of a failed latest background refresh, or ``(None, None)`` after a success."""
    if not status or status.get("ok"):
        return None, None
    return status.get("message") or "Refresh failed", datetime.fromisoformat(
        status["at"]
    )


def _with_refresh_failure(view, status: dict | None, *, complete: bool = True):
    """Attach the failed daily retrieval without alarming the user while nothing is missing.

    ``refresh_error`` always stays on the view for the API and logs. ``stale`` is only set when the
    last retrieval failed AND the stored data is still incomplete for what is due now (see
    ``spot_refresh_plan``/``solar_refresh_plan``). Without any stored data the failure replaces the
    loading hint.
    """
    error, at = _refresh_failure(status)
    if error is None:
        return view
    update = {"refresh_error": error, "refresh_error_at": at}
    if view.status == "unavailable":
        update.update(
            status="error",
            message="The data could not be loaded yet. Another attempt follows automatically.",
        )
    else:
        update["stale"] = not complete
    return view.model_copy(update=update)


def _spot_preview_from_rows(
    config: SpotMarketConfig, rows: list[dict], now: datetime
) -> SpotMarketPreviewView:
    if not rows:
        return SpotMarketPreviewView(
            provider=config.provider,
            bidding_zone=config.bidding_zone,
            currency=ENERGY_CHARTS_CURRENCY,
            status="unavailable",
            message="Spot prices are being loaded into the database in the background.",
        )
    zone_name = rows[0]["timezone"]
    zone, first_day, last_day = local_three_day_window(now, zone_name)
    today_date = now.astimezone(zone).date()
    first_date = first_day.isoformat()
    last_date = last_day.isoformat()
    rows = [row for row in rows if first_date <= row["local_date"] <= last_date]
    if not rows:
        return SpotMarketPreviewView(
            provider=config.provider,
            bidding_zone=config.bidding_zone,
            currency=ENERGY_CHARTS_CURRENCY,
            status="unavailable",
            message="The current three-day spot-price window is being refreshed in the background.",
        )
    latest = max(datetime.fromisoformat(row["fetched_at"]) for row in rows)

    def _snapshot(day_offset: int) -> SpotPriceSnapshot | None:
        target = today_date + timedelta(days=day_offset)
        intervals = tuple(
            sorted(
                (
                    SpotPriceInterval(
                        start=datetime.fromisoformat(row["interval_start"]),
                        end=datetime.fromisoformat(row["interval_end"]),
                        price_eur_per_kwh=Decimal(str(row["price_eur_per_kwh"])),
                    )
                    for row in rows
                    if row["local_date"] == target.isoformat()
                ),
                key=lambda item: item.start,
            )
        )
        if not intervals:
            return None
        return SpotPriceSnapshot(
            provider=config.provider,
            bidding_zone=config.bidding_zone,
            currency=ENERGY_CHARTS_CURRENCY,
            fetched_at=latest,
            intervals=intervals,
            timezone=zone_name,
        )

    today = _snapshot(0) or SpotPriceSnapshot(
        provider=config.provider,
        bidding_zone=config.bidding_zone,
        currency=ENERGY_CHARTS_CURRENCY,
        fetched_at=latest,
        intervals=(),
        timezone=zone_name,
    )
    yesterday, next_day = _snapshot(-1), _snapshot(1)
    tomorrow_message = (
        None
        if next_day is not None
        else "Tomorrow's prices are not available yet; the database refresh runs automatically."
    )
    return _preview_from_snapshot(
        today, yesterday, next_day, None, tomorrow_message, now
    )


def _spot_snapshot_from_rows(
    config: SpotMarketConfig, rows: list[dict]
) -> SpotPriceSnapshot | None:
    if not rows:
        return None
    return SpotPriceSnapshot(
        provider=config.provider,
        bidding_zone=config.bidding_zone,
        currency=ENERGY_CHARTS_CURRENCY,
        fetched_at=max(datetime.fromisoformat(row["fetched_at"]) for row in rows),
        timezone=rows[0]["timezone"],
        intervals=tuple(
            sorted(
                (
                    SpotPriceInterval(
                        start=datetime.fromisoformat(row["interval_start"]),
                        end=datetime.fromisoformat(row["interval_end"]),
                        price_eur_per_kwh=Decimal(str(row["price_eur_per_kwh"])),
                    )
                    for row in rows
                ),
                key=lambda item: item.start,
            )
        ),
    )


def _spot_dashboard_view(
    config: SpotMarketConfig,
    snapshot: SpotPriceSnapshot | None,
    failed: bool,
    now: datetime,
) -> dict:
    """Project only operator-facing prices and freshness, never provider payloads.

    The current price is the stored interval that contains ``now``; ``failed`` means the daily
    retrieval failed while data that is due is still missing.
    """
    base = {
        "status": (
            "disabled" if not config.enabled else "error" if failed else "unavailable"
        ),
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
    if not config.enabled:
        return base
    if snapshot is None:
        if failed:
            base["current_status"] = "error"
        return base
    current = next(
        (item for item in snapshot.intervals if item.start <= now < item.end), None
    )
    base["current_fetched_at"] = snapshot.fetched_at.isoformat()
    base["current_valid_until"] = current.end.isoformat() if current else None
    base["current_status"] = "stale" if failed or current is None else "available"
    if current is not None:
        base["current_price_ct_per_kwh"] = float(current.price_eur_per_kwh * 100)
    age = max(0, (now - snapshot.fetched_at).total_seconds())
    future = [item for item in snapshot.intervals if item.start >= now]
    stale = failed or not future
    base.update(
        status="stale" if stale else "available",
        bidding_zone=snapshot.bidding_zone,
        currency=snapshot.currency,
        fetched_at=snapshot.fetched_at.isoformat(),
        timezone=snapshot.timezone,
        source_generated_at=(
            snapshot.source_generated_at.isoformat()
            if snapshot.source_generated_at
            else None
        ),
        available_from=(
            snapshot.available_from.isoformat() if snapshot.available_from else None
        ),
        available_until=(
            snapshot.available_until.isoformat() if snapshot.available_until else None
        ),
        age_seconds=round(age),
        intervals_available=len(future),
        future_curve=[
            {
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "price_ct_per_kwh": float(item.price_eur_per_kwh * 100),
            }
            for item in future
        ],
    )
    return base


_SOLAR_SOURCE = "Open-Meteo"


def _interval_power_kw(interval) -> float:
    """Average power over one forecast interval, derived from its energy and duration.

    ``energy_wh / seconds`` is average watts; ``* 3600 / 1000`` converts Wh/s to kW. The combined
    factor is ``* 3.6``.
    """
    return interval.energy_wh / (interval.end - interval.start).total_seconds() * 3.6


def _energy_next_24h_wh(future: list, now: datetime) -> float:
    """Total forecast energy over the next 24 hours, summing only the still-future interval slice.

    A partial first interval (``start < now < end``) contributes only its future fraction of energy,
    so past production is never counted. Intervals beyond the 24-hour window are clipped at the
    window edge. The provider already excludes fully past intervals, but clipping here keeps the
    aggregate honest regardless of the forecast horizon.
    """
    window_end = now + timedelta(hours=24)
    total = 0.0
    for interval in future:
        start = max(interval.start, now)
        end = min(interval.end, window_end)
        if end <= start:
            continue
        full = (interval.end - interval.start).total_seconds()
        total += interval.energy_wh * ((end - start).total_seconds() / full)
    return total


def _solar_dashboard_view(
    forecast: SolarForecast | None,
    now: datetime,
    failed: bool = False,
    timezone: str | None = None,
) -> dict:
    """Project only operator-facing solar figures and freshness, never provider payloads.

    ``status`` is ``disabled`` is decided by the caller (no forecast requested); here a present
    forecast is ``available`` or ``stale`` and an absent one is ``error`` (a provider failure with no
    last-known-good) or ``unavailable``. A stale last-known-good forecast keeps its still-future
    intervals so a chart can keep showing it, clearly marked stale.
    """
    base = {
        "status": "error" if failed else "unavailable",
        "source": _SOLAR_SOURCE,
        # The site's IANA timezone, so the dashboard chart can label the curve in local time rather
        # than hard-coding UTC. None falls back to UTC in the browser.
        "timezone": timezone,
        "created_at": None,
        "fetched_at": None,
        "age_seconds": None,
        "covered_until": None,
        "energy_next_24h_wh": None,
        "future_curve": [],
    }
    if forecast is None:
        return base
    future = [item for item in forecast.intervals if item.end > now]
    age = max(0, (now - forecast.created_at).total_seconds())
    # Stale when the daily retrieval failed while data is still missing, or when no future interval
    # remains to show. Age alone is no criterion: the forecast is retrieved once per local day.
    stale = failed or not future
    base.update(
        status="stale" if stale else "available",
        created_at=forecast.created_at.isoformat(),
        fetched_at=forecast.created_at.isoformat(),
        age_seconds=round(age),
        covered_until=future[-1].end.isoformat() if future else None,
        energy_next_24h_wh=_energy_next_24h_wh(future, now) if future else None,
        # A stale last-known-good forecast still exposes its remaining future intervals so the chart
        # keeps the curve visible; only a forecast with no future intervals left blanks the curve.
        future_curve=[
            {
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "power_kw": _interval_power_kw(item),
                "energy_wh": item.energy_wh,
            }
            for item in future
        ],
    )
    return base


def _solar_forecast_from_rows(rows: list[dict]) -> SolarForecast | None:
    if not rows:
        return None
    return SolarForecast(
        created_at=max(datetime.fromisoformat(row["fetched_at"]) for row in rows),
        intervals=tuple(
            sorted(
                (
                    SolarForecastInterval(
                        start=datetime.fromisoformat(row["interval_start"]),
                        end=datetime.fromisoformat(row["interval_end"]),
                        energy_wh=float(row["energy_wh"]),
                    )
                    for row in rows
                ),
                key=lambda item: item.start,
            )
        ),
    )


@router.get("/dashboard-forecast")
async def dashboard_forecast(
    request: Request,
    admin: Annotated[dict | None, Depends(_require_admin_read)],
    include: Annotated[
        list[Literal["spot_current", "spot_day_ahead", "solar"]],
        Query(min_length=1, max_length=3),
    ],
) -> dict:
    """Project only requested forecast data; a browser never calls an external provider."""
    del admin
    if len(set(include)) != len(include):
        raise HTTPException(422, "Duplicate forecast selection")
    store = _admin_store_or_503(request)
    now = request.app.state.runtime.clock.now()
    config = request.app.state.spotmarket_config
    requested = set(include)
    result = {}
    if requested & {"spot_current", "spot_day_ahead"}:
        spot_key = spot_config_key(config)
        rows = await asyncio.to_thread(store.get_spot_market_intervals, spot_key)
        spot_status = await asyncio.to_thread(
            load_refresh_status, store, SPOT_STATUS_KEY, spot_key
        )
        spot_error, spot_error_at = _refresh_failure(spot_status)
        complete = spot_refresh_plan(rows, now).complete
        result["spot"] = {
            **_spot_dashboard_view(
                config,
                _spot_snapshot_from_rows(config, rows),
                spot_error is not None and not complete,
                now,
            ),
            "refresh_error": spot_error,
            "refresh_error_at": spot_error_at.isoformat() if spot_error_at else None,
        }
    if "solar" in requested:
        weather_config = (
            getattr(request.app.state, "weather_config", None) or WeatherConfig()
        )
        if not weather_config.is_forecastable():
            # No network touch for a disabled or incomplete configuration: a disabled forecast is a
            # published state, not an error.
            result["solar"] = {
                **_solar_dashboard_view(None, now, timezone=weather_config.timezone),
                "status": "disabled",
            }
        else:
            _, first_day, last_day = local_three_day_window(
                now, weather_config.timezone or "UTC"
            )
            solar_rows = await asyncio.to_thread(
                store.get_solar_forecast_intervals,
                solar_config_key(weather_config),
                first_day.isoformat(),
                last_day.isoformat(),
            )
            solar_forecast = _solar_forecast_from_rows(solar_rows)
            solar_status = await asyncio.to_thread(
                load_refresh_status,
                store,
                SOLAR_STATUS_KEY,
                solar_config_key(weather_config),
            )
            solar_error, solar_error_at = _refresh_failure(solar_status)
            solar_complete = solar_refresh_plan(
                solar_rows, now, weather_config.timezone
            ).complete
            result["solar"] = {
                **_solar_dashboard_view(
                    solar_forecast,
                    now,
                    solar_error is not None and not solar_complete,
                    timezone=weather_config.timezone,
                ),
                "refresh_error": solar_error,
                "refresh_error_at": (
                    solar_error_at.isoformat() if solar_error_at else None
                ),
            }
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
    """The accepted bidding zones for the form select, read from the stored provider metadata.

    The list is refreshed by the daily spot-price retrieval; this route never calls the provider. With
    no stored list the answer is an empty, stale list (HTTP 200), so the form keeps showing the saved zone.
    """
    del admin
    cache = getattr(request.app.state, "spotmarket_zone_cache", None)
    if cache is None:
        raise HTTPException(503, "The spot-market provider is not configured")
    try:
        listing = await asyncio.to_thread(cache.get)
    except SpotMarketError as exc:
        # No stored list yet: the form keeps showing the saved zone. The daily retrieval logs its own
        # failure; the browser console must not report a gateway fault for this.
        log.info("No bidding-zone list is stored yet: %s", exc)
        return BiddingZonesView(zones=[], default="", stale=True, last_update=None)
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
    # Enablement is derived, not an operator toggle: the GUI omits it, and a saved spot-market
    # configuration is on. Whether day-ahead prices are fetched follows the Energy Manager mode
    # (manual fetches; off/external do not), enforced by the background refresh loop's activity
    # gate, not by this flag. An explicit value (older client, direct API) is still honored.
    data = body.model_dump()
    if data.get("enabled") is None:
        data["enabled"] = True
    try:
        config = SpotMarketConfig.model_validate(data)
    except ValueError as exc:
        raise HTTPException(422, "Invalid spot-market configuration") from exc
    store = _admin_store_or_503(request)
    # Read previous, persist, clear and adopt all under the refresh write lock so two concurrent
    # saves (sync routes run in a threadpool) cannot interleave the DB write and the state swap and
    # leave the persisted config and runtime state disagreeing. A refresh that started under the old
    # configuration then sees the new one and discards its result instead of writing it back.
    with store.forecast_write_lock:
        previous = getattr(request.app.state, "spotmarket_config", None)
        identity_changed = previous is None or spot_config_key(
            previous
        ) != spot_config_key(config)
        store.put("spotmarket_config", config.model_dump(mode="json"))
        if identity_changed:
            store.clear_spot_market_intervals()
        request.app.state.spotmarket_config = config
    if config.enabled and (identity_changed or previous != config):
        _signal_forecast_refresh(request, "spotmarket_refresh_event")
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
    """Project yesterday/today/tomorrow exclusively from the rolling SQLite interval table."""
    del admin
    # Enablement is derived, not an operator toggle (see put_spot_market_config): the GUI omits it,
    # so treat the previewed configuration as on for identity comparison. An explicit value stays
    # honored. Whether a preview is shown depends on whether a configuration was saved and is on.
    data = body.model_dump()
    if data.get("enabled") is None:
        data["enabled"] = True
    try:
        config = SpotMarketConfig.model_validate(data)
    except ValueError as exc:
        raise HTTPException(422, "Invalid spot-market configuration") from exc
    store = _admin_store_or_503(request)
    saved_config = getattr(request.app.state, "spotmarket_config", None)
    if not config.enabled or saved_config is None or not saved_config.enabled:
        return SpotMarketPreviewView(
            provider=config.provider,
            bidding_zone=config.bidding_zone,
            currency=ENERGY_CHARTS_CURRENCY,
            status="disabled",
            message="Enable and save the Spot Market configuration to load prices in the background.",
        )
    if spot_config_key(config) != spot_config_key(saved_config):
        return SpotMarketPreviewView(
            provider=config.provider,
            bidding_zone=config.bidding_zone,
            currency=ENERGY_CHARTS_CURRENCY,
            status="unavailable",
            message="Save the configuration to refresh its database-backed prices.",
        )
    now = request.app.state.runtime.clock.now()
    key = spot_config_key(config)
    rows = await asyncio.to_thread(store.get_spot_market_intervals, key)
    status = await asyncio.to_thread(load_refresh_status, store, SPOT_STATUS_KEY, key)
    return _with_refresh_failure(
        _spot_preview_from_rows(config, rows, now),
        status,
        complete=spot_refresh_plan(rows, now).complete,
    )


#
# Weather / solar forecast: a second read-only configuration domain next to the spot market. The
# Open-Meteo provider only supplies irradiance for a later optimizer; it never writes a register, so
# a provider failure cannot reach inverter control. These routes read/write the persisted config and
# run a one-off preview fetch off the event loop, touching neither the EnergyManager nor dispatch.


class WeatherConfigBody(BaseModel):
    """The weather configuration a save accepts. Validated again by ``WeatherConfig`` so the stored
    form and the in-process form share one validation contract.

    There is no endpoint/URL field on purpose: the provider owns its fixed trusted origin, so the
    operator chooses only the provider and the PV geometry, never a network target (SSRF). Latitude
    and longitude are optional so a disabled first configuration may be saved incomplete; an enabled
    configuration is rejected by ``WeatherConfig`` unless every forecast value is present.
    """

    model_config = ConfigDict(extra="forbid")

    # ``enabled`` is no longer an operator toggle: the GUI dropped the control and does not send it.
    # When absent the server derives it from configuration completeness (a complete config is on);
    # whether the forecast is fetched follows the Energy Manager mode. An explicit value is honored.
    enabled: StrictBool | None = None
    provider: WeatherProvider = WeatherProvider.OPEN_METEO
    location_name: str | None = Field(default=None, max_length=MAX_LOCATION_NAME_LENGTH)
    location_region: str | None = Field(
        default=None, max_length=MAX_LOCATION_NAME_LENGTH
    )
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    pv_peak_power_wp: float | None = Field(
        default=None, gt=0, le=10_000_000, allow_inf_nan=False
    )
    tilt_deg: float | None = Field(default=None, ge=0, le=90, allow_inf_nan=False)
    azimuth_deg: float | None = Field(
        default=None, ge=-180, le=180, allow_inf_nan=False
    )
    performance_ratio: float = Field(default=0.85, gt=0, le=1, allow_inf_nan=False)


class WeatherProviderMetaView(BaseModel):
    """Read-only, provider-owned facts the form shows but never edits: the source name, the azimuth
    convention, and the usage/attribution note for the free Open-Access endpoint.
    """

    source: str
    azimuth_convention: str
    attribution: str
    # The credit for the ACTIVE location geocoder. Photon (OpenStreetMap, ODbL) is the default and
    # the fallback when a view is built without knowing the live geocoder; the config endpoint
    # overrides it with the running geocoder's own ``location_attribution``.
    location_attribution: str = "Location data from Photon © OpenStreetMap contributors"
    no_api_key_required: bool = True
    non_commercial_only: bool = True


class WeatherConfigView(BaseModel):
    """The saved configuration plus the read-only provider metadata the GUI displays."""

    enabled: bool
    provider: WeatherProvider
    location_name: str | None
    location_region: str | None
    latitude: float | None
    longitude: float | None
    timezone: str | None
    pv_peak_power_wp: float | None
    tilt_deg: float | None
    azimuth_deg: float | None
    performance_ratio: float
    provider_meta: WeatherProviderMetaView

    @classmethod
    def from_domain(
        cls, config: WeatherConfig, *, location_attribution: str | None = None
    ) -> "WeatherConfigView":
        meta_kwargs: dict = {
            "source": _SOLAR_SOURCE,
            "azimuth_convention": "0° = south, -90° = east, +90° = west, ±180° = north",
            "attribution": "Weather data by Open-Meteo.com (CC BY 4.0)",
        }
        # Reflect the active geocoder's own attribution when the caller knows it; otherwise keep the
        # Photon default so a view built without a request still carries a valid credit.
        if location_attribution is not None:
            meta_kwargs["location_attribution"] = location_attribution
        return cls(
            enabled=config.enabled,
            provider=config.provider,
            location_name=config.location_name,
            location_region=config.location_region,
            latitude=config.latitude,
            longitude=config.longitude,
            timezone=config.timezone,
            pv_peak_power_wp=config.pv_peak_power_wp,
            tilt_deg=config.tilt_deg,
            azimuth_deg=config.azimuth_deg,
            performance_ratio=config.performance_ratio,
            provider_meta=WeatherProviderMetaView(**meta_kwargs),
        )


class WeatherPreviewInterval(BaseModel):
    start: datetime
    end: datetime
    power_kw: float
    energy_wh: float


class WeatherPreviewView(BaseModel):
    """A database projection of the rolling solar forecast for the Weather tab.

    ``status`` is ``connected``, ``disabled``, ``unavailable`` (the database is warming up),
    ``unsaved`` (the submitted configuration differs from the saved one) or ``error``. Provider calls run only in the background
    refresh loop; the raw Open-Meteo response is never forwarded to the browser.
    """

    provider: WeatherProvider
    source: str
    status: str
    message: str | None = None
    timezone: str | None = None
    fetched_at: datetime | None = None
    covered_until: datetime | None = None
    energy_next_24h_kwh: float | None = None
    intervals: list[WeatherPreviewInterval] = Field(default_factory=list)
    yesterday_intervals: list[WeatherPreviewInterval] = Field(default_factory=list)
    today_intervals: list[WeatherPreviewInterval] = Field(default_factory=list)
    next_day_intervals: list[WeatherPreviewInterval] = Field(default_factory=list)
    refresh_error: str | None = None
    refresh_error_at: datetime | None = None
    stale: bool = False


class WeatherLocationView(BaseModel):
    """One geocoded place the Weather tab offers as a location choice.

    Only the mapped fields the GUI needs are projected; the raw Open-Meteo geocoding response is
    never forwarded to the browser.
    """

    name: str
    latitude: float
    longitude: float
    country: str | None = None
    country_code: str | None = None
    admin1: str | None = None
    admin2: str | None = None
    district: str | None = None
    city: str | None = None
    timezone: str | None = None
    # The single canonical postcode a code query matched exactly, or the place's own postcode for a
    # name hit; never an arbitrary code. Display-only, not persisted in the weather configuration.
    postal_code: str | None = None

    @classmethod
    def from_domain(cls, result: GeocodingResult) -> "WeatherLocationView":
        return cls(
            name=result.name,
            latitude=result.latitude,
            longitude=result.longitude,
            country=result.country,
            country_code=result.country_code,
            admin1=result.admin1,
            admin2=result.admin2,
            district=result.district,
            city=result.city,
            timezone=result.timezone,
            postal_code=result.postal_code,
        )


class WeatherLocationsView(BaseModel):
    """The location-search result for the Weather tab.

    ``status`` is ``ok`` (the search ran, possibly with zero matches) or ``error`` (the geocoder
    failed); on error ``message`` names the cause and ``results`` stays empty. A dead geocoder is a
    clean status here, never a 5xx, so location search can never look like a server fault or reach
    the inverter control path.
    """

    status: str
    message: str | None = None
    results: list[WeatherLocationView] = Field(default_factory=list)


class WeatherTimezoneView(BaseModel):
    """The timezone the Weather tab derives from a site's coordinates.

    The timezone is always determined from the geolocation, never typed by the operator. ``status``
    is ``ok`` (``timezone`` carries a validated IANA zone) or ``error`` (the derivation failed;
    ``message`` names the cause and ``timezone`` stays ``None``). A failed derivation is a clean
    status here, never a 5xx, so it stays isolated from the inverter/dispatch/forecast path.
    """

    status: str
    message: str | None = None
    timezone: str | None = None


@router.get("/weather/config")
def get_weather_config(
    request: Request, admin: Annotated[dict | None, Depends(_require_admin_read)]
) -> WeatherConfigView:
    del admin
    config = getattr(request.app.state, "weather_config", None) or WeatherConfig()
    # Project the ACTIVE geocoder's attribution so the API never claims Photon while Open-Meteo (or
    # vice versa) is the configured provider.
    geocoder = getattr(request.app.state, "solar_geocoder", None)
    location_attribution = getattr(geocoder, "location_attribution", None)
    return WeatherConfigView.from_domain(
        config, location_attribution=location_attribution
    )


@router.get("/weather/locations")
async def search_weather_locations(
    request: Request,
    admin: Annotated[dict | None, Depends(_require_admin_read)],
    q: Annotated[str, Query(max_length=MAX_QUERY_LENGTH)],
    country_code: Annotated[str | None, Query(max_length=2)] = None,
) -> WeatherLocationsView:
    """Resolve a city or international postal-code query to a short list of locations.

    The browser calls only this admin endpoint, never the geocoding provider directly. The active
    provider (Photon by default) owns the postcode knowledge, so there is no application-side
    postal-code regex or country guess. A too-short query returns an empty ``ok`` result without a
    network call; a geocoder failure is reported as ``status=error`` (a 200, not a 5xx) so location
    search stays isolated from inverter control and never leaks the raw provider response. The
    blocking fetch runs off the event loop via ``asyncio.to_thread``.

    ``country_code`` is an optional ISO 3166-1 alpha-2 filter forwarded verbatim to the geocoder;
    the provider validates it (an invalid explicit value raises a :class:`WeatherError`, reported
    as ``status=error``), so there is no application-side country branching or postal regex.
    """
    del admin
    query = q.strip()
    if len(query) < MIN_QUERY_LENGTH:
        return WeatherLocationsView(status="ok")
    geocoder = request.app.state.solar_geocoder
    try:
        results = await asyncio.to_thread(
            geocoder.search, query, count=MAX_RESULTS, country_code=country_code
        )
    except WeatherError as exc:
        log.info("Weather location search failed")
        return WeatherLocationsView(status="error", message=str(exc))
    return WeatherLocationsView(
        status="ok",
        results=[WeatherLocationView.from_domain(result) for result in results],
    )


@router.get("/weather/timezone")
async def resolve_weather_timezone(
    request: Request,
    admin: Annotated[dict | None, Depends(_require_admin_read)],
    latitude: Annotated[float, Query(ge=-90, le=90)],
    longitude: Annotated[float, Query(ge=-180, le=180)],
) -> WeatherTimezoneView:
    """Derive the site's IANA timezone from its coordinates, provider-independent.

    The timezone is always determined from the geolocation (lat/lon), never typed by the operator.
    The default Photon geocoder carries no zone, so the derivation is done server-side through the
    hardened Open-Meteo transport, off the event loop via ``asyncio.to_thread``. A derivation
    failure is reported as ``status=error`` (a 200, not a 5xx) so it stays isolated from inverter
    control and never leaks the raw provider response; it never fabricates or empties a zone on
    error. No coordinates is not a request the browser makes, so this endpoint always has a pair.
    """
    del admin
    resolver = request.app.state.solar_timezone_resolver
    try:
        timezone = await asyncio.to_thread(resolver.resolve, latitude, longitude)
    except WeatherError as exc:
        log.info("Weather timezone derivation failed")
        return WeatherTimezoneView(status="error", message=str(exc))
    return WeatherTimezoneView(status="ok", timezone=timezone)


# Field-friendly labels for the forecast values an enabled config must carry, so a client that does
# not post an explicit field (the completeness error has no single field location) can still render
# an inline hint per field rather than one opaque string.
_WEATHER_FIELD_LABELS = {
    "latitude": "Latitude",
    "longitude": "Longitude",
    "pv_peak_power_wp": "PV peak power",
    "tilt_deg": "Panel tilt",
    "azimuth_deg": "Panel azimuth",
    "performance_ratio": "Performance ratio",
    "timezone": "Timezone",
    "provider": "Provider",
    "location_name": "Location name",
    "location_region": "Location region",
}


def _weather_validation_errors(exc: ValidationError) -> list[dict]:
    """Project a pydantic ``ValidationError`` into FastAPI's ``{loc, msg, type}`` detail shape.

    The admin client already renders ``detail[].msg`` for a 422; carrying ``loc`` as well lets it
    attach each message to the offending field instead of surfacing one generic string.
    """
    return [
        {
            "loc": list(error.get("loc", ())),
            "msg": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in exc.errors()
    ]


async def _weather_body_with_timezone(
    request: Request, body: WeatherConfigBody
) -> dict:
    """Build WeatherConfig input with a timezone derived only from latitude/longitude.

    WeatherConfigBody deliberately has no timezone field, so the browser or another client cannot
    provide or preserve one. Coordinates are the sole authority.
    """
    data = body.model_dump()
    # ``enabled`` is no longer an operator toggle. When the client omits it (the GUI), the server
    # derives it from completeness: a config carrying every forecast-required value is on, an
    # incomplete one is stored off. Whether the forecast is actually fetched follows the Energy
    # Manager mode (manual fetches; off/external do not). An explicit value is honored unchanged, so
    # a direct API client sending ``enabled=True`` with missing fields still gets the 422.
    if data.get("enabled") is None:
        data["enabled"] = all(
            data.get(name) is not None for name in _FORECAST_REQUIRED_FIELDS
        )
    data["timezone"] = None
    if body.latitude is None or body.longitude is None:
        return data
    resolver = request.app.state.solar_timezone_resolver
    data["timezone"] = await asyncio.to_thread(
        resolver.resolve, body.latitude, body.longitude
    )
    return data


def _weather_incomplete_fields(exc: ValidationError) -> list[str]:
    """The forecast fields a config is missing when enabled, or ``[]`` if the error is something else.

    ``WeatherConfig._require_complete_when_enabled`` raises one model-level error whose message lists
    the missing fields. Only that completeness error turns a preview into a clean ``disabled`` view;
    any out-of-range or unknown-field error is a genuine 422 even in the preview path.
    """
    missing: list[str] = []
    for error in exc.errors():
        if error.get("type") != "value_error":
            return []
        message = error.get("msg", "")
        found = [name for name in _WEATHER_FIELD_LABELS if name in message]
        if "enabled solar forecast requires" not in message or not found:
            return []
        missing.extend(found)
    return missing


@router.put("/weather/config")
async def put_weather_config(
    request: Request,
    body: WeatherConfigBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> WeatherConfigView:
    del admin
    try:
        config = WeatherConfig.model_validate(
            await _weather_body_with_timezone(request, body)
        )
    except WeatherError as exc:
        raise HTTPException(503, f"Could not determine site timezone: {exc}") from exc
    except ValidationError as exc:
        # Carry the real per-field pydantic errors so the form can show which value is missing or
        # out of range, instead of a single opaque "Invalid weather configuration" string. Enabling
        # the forecast with empty fields is a specific, field-level validation result, not a mystery.
        raise HTTPException(422, _weather_validation_errors(exc)) from exc
    store = _admin_store_or_503(request)

    def _save_weather() -> tuple[WeatherConfig | None, bool]:
        # Persist, clear and adopt under the refresh write lock (see put_spot_market_config); the
        # background refresh re-reads the configuration on its next wake-up. This runs off the event
        # loop because AdminStore.connect() may busy-wait on SQLite for up to five seconds; doing it
        # inline would stall the whole loop. The read/persist/clear/swap stay atomic to each other.
        with store.forecast_write_lock:
            previous = getattr(request.app.state, "weather_config", None)
            identity_changed = previous is None or solar_config_key(
                previous
            ) != solar_config_key(config)
            store.put("weather_config", config.model_dump(mode="json"))
            if identity_changed:
                store.clear_solar_forecast_intervals()
            request.app.state.weather_config = config
            return previous, identity_changed

    previous, identity_changed = await asyncio.to_thread(_save_weather)
    if config.is_forecastable() and (
        identity_changed
        or (previous is not None and previous.enabled != config.enabled)
    ):
        _signal_forecast_refresh(request, "weather_refresh_event")
    log.info(
        "Weather configuration saved: provider=%s enabled=%s admin=%s",
        config.provider.value, config.enabled, _ADMIN_ACTOR,
    )  # fmt: skip
    return WeatherConfigView.from_domain(config)


@router.post("/weather/preview")
async def preview_weather(
    request: Request,
    body: WeatherConfigBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> WeatherPreviewView:
    """Project the persisted rolling solar window without making a provider request."""
    del admin
    saved_config = getattr(request.app.state, "weather_config", None)
    body_data = body.model_dump()
    # ``enabled`` is derived, not an operator toggle (see _weather_body_with_timezone): when the GUI
    # omits it, a complete config is on and an incomplete one off. An explicit value is honored.
    if body_data.get("enabled") is None:
        body_data["enabled"] = all(
            body_data.get(name) is not None for name in _FORECAST_REQUIRED_FIELDS
        )
    # A preview reads only the saved configuration's DB snapshot. Reuse its stored timezone and
    # never call the timezone resolver or forecast provider on this request.
    body_data["timezone"] = saved_config.timezone if saved_config is not None else None
    try:
        config = WeatherConfig.model_validate(body_data)
    except ValidationError as exc:
        # An enabled-but-incomplete config is not a forecastable config, so the preview reports the
        # same clean "disabled" view the handler already returns below for a disabled config, rather
        # than a 422 the operator sees while still filling the form. Any other validation error
        # (out of range, unknown provider, extra field) stays a structured 422.
        if _weather_incomplete_fields(exc):
            provider = (
                body.provider
                if isinstance(body.provider, WeatherProvider)
                else WeatherProvider.OPEN_METEO
            )
            return WeatherPreviewView(
                provider=provider,
                source=_SOLAR_SOURCE,
                status="disabled",
                timezone=None,
            )
        raise HTTPException(422, _weather_validation_errors(exc)) from exc
    if not config.is_forecastable():
        return WeatherPreviewView(
            provider=config.provider,
            source=_SOLAR_SOURCE,
            status="disabled",
            timezone=config.timezone,
        )
    now = request.app.state.runtime.clock.now()
    if saved_config is None or solar_config_key(config) != solar_config_key(
        saved_config
    ):
        # The unsaved form has no stored forecast to project. Report that as its own status with no
        # timezone, so the browser neither shows a "warming up" state nor lets the saved zone
        # override the one it is deriving for the newly chosen location.
        return WeatherPreviewView(
            provider=config.provider, source=_SOLAR_SOURCE, status="unsaved"
        )
    zone, first_day, last_day = local_three_day_window(now, config.timezone or "UTC")
    local_today = now.astimezone(zone).date()
    weather_store = _admin_store_or_503(request)
    solar_key = solar_config_key(config)
    rows = await asyncio.to_thread(
        weather_store.get_solar_forecast_intervals,
        solar_key,
        first_day.isoformat(),
        last_day.isoformat(),
    )
    status = await asyncio.to_thread(
        load_refresh_status, weather_store, SOLAR_STATUS_KEY, solar_key
    )
    forecast = _solar_forecast_from_rows(rows)
    if forecast is None:
        return _with_refresh_failure(
            WeatherPreviewView(
                provider=config.provider,
                source=_SOLAR_SOURCE,
                status="unavailable",
                message="Solar forecast values are being loaded into the database in the background.",
                timezone=config.timezone,
            ),
            status,
        )
    future = [item for item in forecast.intervals if item.end > now]

    def preview_intervals(day_offset: int) -> list[WeatherPreviewInterval]:
        target_date = local_today + timedelta(days=day_offset)
        return [
            WeatherPreviewInterval(
                start=item.start,
                end=item.end,
                power_kw=_interval_power_kw(item),
                energy_wh=item.energy_wh,
            )
            for item in forecast.intervals
            if item.start.astimezone(zone).date() == target_date
        ]

    return _with_refresh_failure(
        WeatherPreviewView(
            provider=config.provider,
            source=_SOLAR_SOURCE,
            status="connected",
            timezone=config.timezone,
            fetched_at=forecast.created_at,
            covered_until=future[-1].end if future else None,
            energy_next_24h_kwh=(
                _energy_next_24h_wh(future, now) / 1000 if future else 0.0
            ),
            intervals=[
                WeatherPreviewInterval(
                    start=item.start,
                    end=item.end,
                    power_kw=_interval_power_kw(item),
                    energy_wh=item.energy_wh,
                )
                for item in future
            ],
            yesterday_intervals=preview_intervals(-1),
            today_intervals=preview_intervals(0),
            next_day_intervals=preview_intervals(1),
        ),
        status,
        complete=solar_refresh_plan(rows, now, config.timezone).complete,
    )


#
# Dynamic charging planner — Dry-Run preview (design: Dynamic Charging Planner v1).
#
# This admin-only route returns a current Dry-Run EnergyPlan per device. It is a preview and
# evaluation surface ONLY: it never commands the battery, never calls EnergyManager.command() or the
# dispatch layer, and never triggers a provider network request. Every planning input comes from the
# local SQLite stores (spot prices, solar forecast, consumption history) and the periodic-read cache
# (battery SoC). Missing preconditions produce an understandable status, never a 500 or raw internal
# detail.


class DryRunPlanSlotView(BaseModel):
    start: datetime
    end: datetime
    action: str
    target_soc_percent: float | None
    max_power_w: float | None
    reason: str


class DryRunForecastWindowView(BaseModel):
    consumption_from: datetime | None = None
    consumption_until: datetime | None = None
    solar_from: datetime | None = None
    solar_until: datetime | None = None
    prices_from: datetime | None = None
    prices_until: datetime | None = None


class DryRunChargePowerView(BaseModel):
    """Permitted (hard) and preferred charge power; a missing live limit is never "unlimited"."""

    static_charge_limit_w: float
    live_charge_limit_w: float | None
    live_limit_available: bool
    permitted_charge_power_w: float
    preferred_min_w: float | None
    preferred_max_w: float | None


class DryRunEnergyView(BaseModel):
    """Charge energy in separate domains; efficiency applies only between terminal and grid."""

    commanded_charge_energy_wh: float
    battery_terminal_energy_wh: float
    stored_energy_delta_wh: float
    grid_charge_energy_wh: float


class DryRunAssumptionsView(BaseModel):
    charge_efficiency: float
    charge_efficiency_source: str
    power_reference: str
    power_reference_verified: bool
    live_limit_status: str
    forecast_basis: str
    battery_internal_losses: str
    charge_efficiency_note: str
    preferred_range_status: str


class DryRunLimitingReasonView(BaseModel):
    code: str
    detail: str


class DryRunReplanTriggerView(BaseModel):
    code: str
    description: str


class DryRunReplanView(BaseModel):
    """The conditions under which this plan is stale and must be recomputed (no auto-executor)."""

    triggers: list[DryRunReplanTriggerView]
    thresholds: dict[str, float]


def _replan_view() -> DryRunReplanView:
    thresholds = DEFAULT_REPLAN_THRESHOLDS
    return DryRunReplanView(
        triggers=[
            DryRunReplanTriggerView(code=code.value, description=text)
            for code, text in TRIGGER_DESCRIPTIONS.items()
        ],
        thresholds={
            "soc_deviation_percent": thresholds.soc_deviation_percent,
            "charge_limit_change_w": thresholds.charge_limit_change_w,
            "price_change_eur_per_kwh": thresholds.price_change_eur_per_kwh,
            "forecast_change_fraction": thresholds.forecast_change_fraction,
            "forecast_change_floor_wh": thresholds.forecast_change_floor_wh,
            "missed_slot_fraction": thresholds.missed_slot_fraction,
        },
    )


class DryRunBatteryView(BaseModel):
    """The BatteryState snapshot the plan was computed from (never re-read from the inverter).

    ``remaining_energy_wh`` is the verified usable capacity times the measured SoC, not the nominal
    total capacity.
    """

    soc_percent: float
    usable_capacity_wh: float
    remaining_energy_wh: float
    max_charge_power_w: float
    max_discharge_power_w: float
    measured_at: datetime


def _battery_view(state) -> DryRunBatteryView | None:
    if state is None:
        return None
    return DryRunBatteryView(
        soc_percent=state.soc_percent,
        usable_capacity_wh=state.usable_capacity_wh,
        remaining_energy_wh=state.usable_capacity_wh * state.soc_percent / 100.0,
        max_charge_power_w=state.max_charge_power_w,
        max_discharge_power_w=state.max_discharge_power_w,
        measured_at=state.measured_at,
    )


class DryRunDayView(BaseModel):
    date: str
    status: Literal["ok", "pending", "unavailable"]
    slots: list[DryRunPlanSlotView] = Field(default_factory=list)


class DryRunPlanView(BaseModel):
    """The Dry-Run plan preview for one device.

    ``status`` is ``ok``, ``energy_manager_off`` (nothing was computed) or one of the explicit
    unavailable reasons (``insufficient_history``,
    ``spot_disabled``, ``spot_prices_unavailable``, ``solar_not_configured``,
    ``solar_forecast_unavailable``, ``battery_state_unavailable``).
    ``price_disclaimer`` makes clear the plan uses the raw day-ahead price only: it is part of the
    plan basis and change detection but never moves charging into an earlier slot — charging is
    placed as late as the reserve and limits permit — and grid fees, taxes, levies and supplier
    margin are not included, so no absolute household cost is implied. The whole surface is
    preview-only and performs no battery control.
    ``forecast_basis`` and ``forecast_confidence`` describe the consumption input quality;
    ``consumption_history_available`` is false when the configured baseline was used.
    """

    device_id: str
    status: str
    detail: str | None = None
    created_at: datetime | None = None
    valid_until: datetime | None = None
    horizon_hours: float
    timezone: str = "UTC"
    today: DryRunDayView | None = None
    tomorrow: DryRunDayView | None = None
    forecast_basis: ForecastBasis | None = None
    consumption_history_available: bool | None = None
    forecast_confidence: ForecastConfidence | None = None
    # Battery snapshot of the plan's own input; None when no verified battery state was available.
    battery: DryRunBatteryView | None = None
    slots: list[DryRunPlanSlotView] = Field(default_factory=list)
    window: DryRunForecastWindowView = Field(default_factory=DryRunForecastWindowView)
    # Reserve (hard lower bound along the whole trajectory) and target (goal at target_time) are
    # reported independently; None while no plan exists.
    policy: str | None = None
    reserve_soc_percent: float | None = None
    reserve_satisfied: bool | None = None
    reserve_shortfall_wh: float | None = None
    target_soc_percent: float | None = None
    target_time: datetime | None = None
    target_reachable: bool | None = None
    unserved_target_energy_wh: float | None = None
    power: DryRunChargePowerView | None = None
    energy: DryRunEnergyView | None = None
    assumptions: DryRunAssumptionsView | None = None
    limiting_reasons: list[DryRunLimitingReasonView] = Field(default_factory=list)
    replan: DryRunReplanView = Field(default_factory=_replan_view)
    preview_only: bool = True
    price_disclaimer: str = (
        "Charging is placed as late as the reserve and limits permit. The raw day-ahead market price "
        "is part of the plan basis and change detection but never moves charging into an earlier "
        "slot. Grid fees, taxes, levies and supplier margin are not included; this is not an "
        "absolute household cost."
    )


def _plan_slot_views(plan: EnergyPlan) -> list[DryRunPlanSlotView]:
    return [
        DryRunPlanSlotView(
            start=slot.start,
            end=slot.end,
            action=slot.action.value,
            target_soc_percent=slot.target_soc_percent,
            max_power_w=slot.max_power_w,
            reason=slot.reason,
        )
        for slot in plan.slots
    ]


def _plan_flag_fields(plan: EnergyPlan | None) -> dict:
    """The policy, reserve/target flags, power, energy, assumptions and reasons of a plan."""
    if plan is None or plan.power is None or plan.assumptions is None:
        return {}
    assumptions = plan.assumptions
    return {
        "policy": plan.policy.value,
        "reserve_soc_percent": plan.reserve_soc_percent,
        "reserve_satisfied": plan.reserve_satisfied,
        "reserve_shortfall_wh": plan.reserve_shortfall_wh,
        "target_soc_percent": plan.target_soc_percent,
        "target_time": plan.target_time,
        "target_reachable": plan.target_reachable,
        "unserved_target_energy_wh": plan.unserved_target_energy_wh,
        "power": DryRunChargePowerView(
            static_charge_limit_w=plan.power.static_charge_limit_w,
            live_charge_limit_w=plan.power.live_charge_limit_w,
            live_limit_available=plan.power.live_limit_available,
            permitted_charge_power_w=plan.power.permitted_charge_power_w,
            preferred_min_w=plan.power.preferred_min_w,
            preferred_max_w=plan.power.preferred_max_w,
        ),
        "energy": DryRunEnergyView(
            commanded_charge_energy_wh=plan.energy.commanded_charge_energy_wh,
            battery_terminal_energy_wh=plan.energy.battery_terminal_energy_wh,
            stored_energy_delta_wh=plan.energy.stored_energy_delta_wh,
            grid_charge_energy_wh=plan.energy.grid_charge_energy_wh,
        ),
        "assumptions": DryRunAssumptionsView(
            charge_efficiency=assumptions.charge_efficiency.value,
            charge_efficiency_source=assumptions.charge_efficiency.source.value,
            power_reference=assumptions.power_reference,
            power_reference_verified=assumptions.power_reference_verified,
            live_limit_status=assumptions.live_limit_status,
            forecast_basis=assumptions.forecast_basis,
            battery_internal_losses=assumptions.battery_internal_losses,
            charge_efficiency_note=assumptions.charge_efficiency_note,
            preferred_range_status=assumptions.preferred_range_status,
        ),
        "limiting_reasons": [
            DryRunLimitingReasonView(code=reason.code.value, detail=reason.detail)
            for reason in plan.limiting_reasons
        ],
    }


def _planning_store(request: Request, *, create: bool) -> DispatchStore | None:
    """The persistent limits store, reachable without the dispatch controller (write support off).

    Planning only reads and writes the per-device limits row; it never activates the executor, so
    battery control stays gated by write support. Returns None when there is no secret to open the
    store with, or (``create`` false) when no database exists yet.
    """
    state_store = getattr(request.app.state, "dispatch_store", None)
    if state_store is not None:
        return state_store
    settings = request.app.state.runtime.settings
    if settings.hmac_secret is None or not (
        create or settings.dispatch_db_path.exists()
    ):
        return None
    store = DispatchStore(
        settings.dispatch_db_path, settings.hmac_secret.get_secret_value()
    )
    store.initialize()
    return store


async def _planning_limits(request: Request) -> dict[str, DeviceLimits]:
    """Per-device limits for planning: the live controller when present, else the stored rows."""
    from app.api.app_factory import (
        _seed_limits,
    )  # late import: app_factory imports this module

    runtime = request.app.state.runtime
    if runtime.dispatch is not None:
        return {
            device_id: limits
            for device_id in runtime.devices
            if (limits := runtime.dispatch.device_limits(device_id)) is not None
        }
    store = await asyncio.to_thread(_planning_store, request, create=False)
    stored = {} if store is None else await asyncio.to_thread(store.get_device_configs)
    return _seed_limits(runtime.settings, stored)


def _limits_view(limits: DeviceLimits | None) -> LimitsView | None:
    if limits is None:
        return None
    return LimitsView(
        max_charge_power_w=limits.max_charge_power_w,
        max_discharge_power_w=limits.max_discharge_power_w,
        engineering_mode=limits.engineering_mode,
        usable_capacity_wh=limits.usable_capacity_wh,
    )


class PlanningDeviceView(BaseModel):
    device_id: str
    device_name: str
    limits: LimitsView | None
    battery_capacity_source: Literal["verified", "expert_override", "unavailable"]
    # Energy Manager operating mode; None when the manager is not running. The plan page hides
    # "Next grid charge" while the mode is "off".
    mode: Literal["off", "manual", "external"] | None = None


_BATTERY_CHARACTERIZATION_NAMES = (
    "power_mng_n_batteries",
    "power_mng_battery_type",
    "power_mng_amp_hours_measured",
    "battery_ah_capacity",
    "battery_placeholder_0_ah_capacity",
    "battery_soh",
    "battery_voltage",
    "battery_maximum_charge_current",
    "battery_maximum_discharge_current",
    "battery_placeholder_0_current",
    "battery_placeholder_0_maximum_charge_current",
    "battery_placeholder_0_maximum_discharge_current",
)
_BATTERY_CHARACTERIZATION_REFRESH_MAX = 4


class BatteryCharacterizationMetricView(BaseModel):
    name: str
    value: int | float | None
    unit: str | None
    age_seconds: float | None
    status: Literal["fresh", "stale", "unavailable"]


class BatteryTowerModulesView(BaseModel):
    id: str
    present: bool
    module_count: int | None
    module_count_status: Literal["ok", "pending", "anomaly", "not_present"]
    populated_module_slots: list[int]


class BatteryCharacterizationView(BaseModel):
    """Raw diagnostic readings and stabilized module topology, with no usable-Wh inference."""

    device_id: str
    metrics: list[BatteryCharacterizationMetricView]
    towers: list[BatteryTowerModulesView]


class BatteryCalibrationHistoryPoint(BaseModel):
    measured_at: datetime
    battery_bms_sn: str | None
    battery_cycles: int | None
    reference_ah: float
    measured_ah: float
    rated_usable_capacity_wh: float
    current_usable_capacity_wh: float
    remaining_capacity_percent: float
    module_counts: list[int]
    source_contract: str


class BatteryCommissioningView(BaseModel):
    commissioned_at: datetime
    # 'logger_derived' is UNVERIFIED on hardware (see RctCommissioningHistory); 'manual' always wins.
    source: Literal["logger_derived", "manual"]
    determined_at: datetime


class BatteryCalibrationBaseline(BaseModel):
    """Synthetic first point of the curve: the datasheet capacity at commissioning, not a measurement."""

    date: datetime
    battery_cycles: Literal[0]
    rated_usable_capacity_wh: float
    current_usable_capacity_wh: float
    remaining_capacity_percent: float
    source: Literal["logger_derived", "manual"]


class BatteryCalibrationHistoryView(BaseModel):
    device_id: str
    inverter_serial: str | None
    battery_bms_sn: str | None
    battery_replaced: bool
    commissioning: BatteryCommissioningView | None
    baseline: BatteryCalibrationBaseline | None
    baseline_unavailable_reason: (
        Literal[
            "inverter_serial_unknown",
            "no_commissioning_date",
            "no_measurement",
            "battery_replaced",
        ]
        | None
    )
    points: list[BatteryCalibrationHistoryPoint]


class BatteryCommissioningBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commissioned_on: date


class BatteryCharacterizationRefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names: list[Annotated[str, Field(max_length=64)]] = Field(
        min_length=1, max_length=_BATTERY_CHARACTERIZATION_REFRESH_MAX
    )


def _battery_characterization_view(
    runtime, device_id: str
) -> BatteryCharacterizationView:
    towers = []
    for prefix in _BATTERY_TOWER_PREFIXES:
        report = _battery_module_report(runtime, device_id, prefix)
        present = _battery_tower_present(
            runtime, device_id, prefix, report["populated_module_slots"]
        )
        towers.append(
            BatteryTowerModulesView(
                id=prefix,
                present=present,
                **(
                    report
                    if present
                    else {**report, "module_count_status": "not_present"}
                ),
            )
        )
    second_tower_present = towers[1].present
    metrics = []
    for name in _BATTERY_CHARACTERIZATION_NAMES:
        entry = (
            runtime.catalog.object_entry(name) if runtime.catalog.exists(name) else None
        )
        sample = (
            runtime.gateway.cached_sample(device_id, name)
            if entry is not None
            and (not name.startswith("battery_placeholder_0_") or second_tower_present)
            else None
        )
        value = sample[0] if sample is not None else None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            sample = None
        metrics.append(
            BatteryCharacterizationMetricView(
                name=name,
                value=sample[0] if sample is not None else None,
                unit=entry.unit if entry is not None else None,
                age_seconds=sample[1] if sample is not None else None,
                status=(
                    ("fresh" if sample[2] is CacheFreshness.FRESH else "stale")
                    if sample is not None
                    else "unavailable"
                ),
            )
        )
    return BatteryCharacterizationView(
        device_id=device_id, metrics=metrics, towers=towers
    )


@router.get("/devices/{device_id}/battery-characterization")
async def battery_characterization(
    request: Request,
    device_id: str,
    admin: Annotated[dict | None, Depends(_require_admin_read)],
) -> BatteryCharacterizationView:
    """Return cache-only raw battery diagnostics without device transactions or capacity claims."""
    del admin
    _device_or_404(request, device_id)
    return _battery_characterization_view(request.app.state.runtime, device_id)


def _calibration_view(
    store, device_id: str, serial: str | None, limit: int
) -> BatteryCalibrationHistoryView:
    """History of the inverter's current battery series plus the commissioning baseline (store reads only)."""
    if not serial:
        return BatteryCalibrationHistoryView(
            device_id=device_id,
            inverter_serial=None,
            battery_bms_sn=None,
            battery_replaced=False,
            commissioning=None,
            baseline=None,
            baseline_unavailable_reason="inverter_serial_unknown",
            points=[],
        )
    history = store.get_battery_calibration_history(serial, limit)
    points = history["points"]
    commissioning = store.get_inverter_commissioning(serial)
    baseline = None
    reason = None
    if commissioning is None:
        reason = "no_commissioning_date"
    elif not points:
        reason = "no_measurement"
    elif history["battery_replaced"] and commissioning["source"] != "manual":
        reason = "battery_replaced"  # a derived date describes the first battery, not the present one
    else:
        rated = points[-1]["rated_usable_capacity_wh"]
        baseline = BatteryCalibrationBaseline(
            date=commissioning["commissioned_at"],
            battery_cycles=0,
            rated_usable_capacity_wh=rated,
            current_usable_capacity_wh=rated,
            remaining_capacity_percent=100.0,
            source=commissioning["source"],
        )
    return BatteryCalibrationHistoryView(
        device_id=device_id,
        inverter_serial=serial,
        battery_bms_sn=history["battery_bms_sn"],
        battery_replaced=history["battery_replaced"],
        commissioning=(
            None if commissioning is None else BatteryCommissioningView(**commissioning)
        ),
        baseline=baseline,
        baseline_unavailable_reason=reason,
        points=points,
    )


@router.get("/devices/{device_id}/battery-calibration-history")
async def battery_calibration_history(
    request: Request,
    device_id: str,
    admin: Annotated[dict | None, Depends(_require_admin_read)],
    limit: Annotated[int, Query(ge=1, le=5000)] = 1200,
) -> BatteryCalibrationHistoryView:
    """Read permanent local BMS calibration records without contacting the inverter.

    The device_id only addresses the inverter; the history is stored and returned per inverter serial.
    """
    del admin
    _device_or_404(request, device_id)
    serial = request.app.state.runtime.gateway.reported_serial(device_id)
    return await asyncio.to_thread(
        _calibration_view, _admin_store_or_503(request), device_id, serial, limit
    )


@router.put("/devices/{device_id}/battery-commissioning")
async def put_battery_commissioning(
    request: Request,
    device_id: str,
    body: BatteryCommissioningBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> BatteryCalibrationHistoryView:
    """Operator-entered commissioning date; it overrides the one derived from the inverter loggers."""
    del admin
    _device_or_404(request, device_id)
    runtime = request.app.state.runtime
    serial = runtime.gateway.reported_serial(device_id)
    if not serial:
        raise HTTPException(409, "The inverter serial number is not known yet")
    if (
        not MIN_COMMISSIONING_DATE
        <= body.commissioned_on
        <= runtime.clock.now().astimezone(UTC).date()
    ):
        raise HTTPException(
            422, "The commissioning date must lie between 2010-01-01 and today"
        )
    store = _admin_store_or_503(request)
    moment = datetime(
        body.commissioned_on.year,
        body.commissioned_on.month,
        body.commissioned_on.day,
        tzinfo=UTC,
    )
    await asyncio.to_thread(store.set_inverter_commissioning, serial, moment, "manual")
    log.warning(
        "Battery commissioning date set manually: device=%s date=%s admin=%s",
        device_id,
        body.commissioned_on,
        _ADMIN_ACTOR,
    )
    return await asyncio.to_thread(_calibration_view, store, device_id, serial, 1200)


@router.delete("/devices/{device_id}/battery-commissioning")
async def delete_battery_commissioning(
    request: Request,
    device_id: str,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> BatteryCalibrationHistoryView:
    """Remove the operator override; a date derived from the loggers applies again."""
    del admin
    _device_or_404(request, device_id)
    serial = request.app.state.runtime.gateway.reported_serial(device_id)
    if not serial:
        raise HTTPException(409, "The inverter serial number is not known yet")
    store = _admin_store_or_503(request)
    await asyncio.to_thread(store.delete_inverter_commissioning, serial, "manual")
    log.warning(
        "Battery commissioning date override removed: device=%s admin=%s",
        device_id,
        _ADMIN_ACTOR,
    )
    return await asyncio.to_thread(_calibration_view, store, device_id, serial, 1200)


def _require_admin_device_refresh(request: Request) -> dict | None:
    session = require_admin(request)
    if session is not None:
        _csrf(request, session)
    return session


@router.post("/devices/{device_id}/battery-characterization/refresh")
async def refresh_battery_characterization(
    request: Request,
    device_id: str,
    body: BatteryCharacterizationRefreshBody,
    admin: Annotated[dict | None, Depends(_require_admin_device_refresh)],
) -> BatteryCharacterizationView:
    """Budgeted, explicit read of at most four allowlisted numeric metrics; never a write."""
    del admin
    _device_or_404(request, device_id)
    if len(set(body.names)) != len(body.names) or any(
        name not in _BATTERY_CHARACTERIZATION_NAMES for name in body.names
    ):
        raise HTTPException(422, "Unknown or duplicate battery characterization metric")
    runtime = request.app.state.runtime
    second_report = _battery_module_report(runtime, device_id, "battery_placeholder_0")
    second_tower_present = _battery_tower_present(
        runtime,
        device_id,
        "battery_placeholder_0",
        second_report["populated_module_slots"],
    )
    for name in body.names:
        if name.startswith("battery_placeholder_0_") and not second_tower_present:
            continue
        if not runtime.catalog.exists(name) or not is_numeric(
            runtime.catalog.object_entry(name).value_type
        ):
            continue
        try:
            await runtime.gateway.read_metric(device_id, name, fresh=False)
        except DeviceApiError:
            continue
    return _battery_characterization_view(runtime, device_id)


@router.get("/planning-devices")
async def planning_devices(
    request: Request, admin: Annotated[dict | None, Depends(_require_admin_read)]
) -> list[PlanningDeviceView]:
    """Device list for the Dry-Run preview: read-only, independent of write support and dispatch."""
    del admin
    runtime = request.app.state.runtime
    limits = await _planning_limits(request)
    return [
        PlanningDeviceView(
            device_id=device_id,
            device_name=device.display_name
            or runtime.gateway.reported_name(device_id)
            or device_id,
            limits=_limits_view(limits.get(device_id)),
            battery_capacity_source=resolve_device_battery_facts(
                limits.get(device_id), runtime.battery_capacity, device_id
            ).capacity_source,
            mode=(
                runtime.energy.mode(device_id).value
                if runtime.energy is not None
                else None
            ),
        )
        for device_id, device in sorted(runtime.devices.items())
    ]


class PlanningLimitsBody(BaseModel):
    """The battery data the planner needs; saved even while write support is off."""

    model_config = ConfigDict(extra="forbid")

    max_charge_power_w: float = Field(gt=0, le=MAX_POWER_W, allow_inf_nan=False)
    max_discharge_power_w: float = Field(gt=0, le=MAX_POWER_W, allow_inf_nan=False)
    # Omitted keeps the stored value, explicit null clears it.
    usable_capacity_wh: float | None = Field(
        default=None, gt=0, le=MAX_USABLE_CAPACITY_WH, allow_inf_nan=False
    )


@router.put("/devices/{device_id}/planning-limits")
async def put_planning_limits(
    request: Request,
    device_id: str,
    body: PlanningLimitsBody,
    admin: Annotated[dict | None, Depends(_require_admin_write)],
) -> LimitsView:
    del admin
    _device_or_404(request, device_id)
    runtime = request.app.state.runtime
    existing = (await _planning_limits(request)).get(device_id)
    capacity_wh = body.usable_capacity_wh
    if "usable_capacity_wh" not in body.model_fields_set:
        capacity_wh = None if existing is None else existing.usable_capacity_wh
    limits = DeviceLimits(
        max_charge_power_w=body.max_charge_power_w,
        max_discharge_power_w=body.max_discharge_power_w,
        engineering_mode=False if existing is None else existing.engineering_mode,
        usable_capacity_wh=capacity_wh,
    )
    try:
        if runtime.dispatch is not None:
            await runtime.dispatch.set_device_limits(device_id, limits)
        else:
            store = await asyncio.to_thread(_planning_store, request, create=True)
            if store is None:
                raise HTTPException(503, "The limits store is not configured")
            await asyncio.to_thread(store.put_device_config, device_id, limits)
    except CapabilityConflict as exc:
        raise _conflict(exc) from exc
    log.warning(
        "Planning limits updated: device=%s charge_w=%s discharge_w=%s capacity_wh=%s",
        device_id, limits.max_charge_power_w, limits.max_discharge_power_w, limits.usable_capacity_wh,
    )  # fmt: skip
    return _limits_view(limits)  # type: ignore[return-value]


class ConsumptionDayView(BaseModel):
    date: str
    energy_wh: float | None
    source: Literal["local_measured", "inverter_history", "unavailable"]


class ConsumptionHistoryView(BaseModel):
    """Household consumption per completed local day, oldest first; local store only."""

    device_id: str
    timezone: str
    days: list[ConsumptionDayView]
    # Explain an empty chart: when local recording began.
    recording_since: str | None


@router.get("/devices/{device_id}/consumption-history")
async def consumption_history(
    request: Request,
    device_id: str,
    admin: Annotated[dict | None, Depends(_require_admin_read)],
) -> ConsumptionHistoryView:
    """The last completed local days (the running day excluded), with no device I/O."""
    del admin
    _device_or_404(request, device_id)
    store = _admin_store_or_503(request)
    runtime = request.app.state.runtime
    zone_name = (
        getattr(getattr(request.app.state, "weather_config", None), "timezone", None)
        or "UTC"
    )
    now = runtime.clock.now()
    last = now.astimezone(resolve_zone(zone_name)).date() - timedelta(days=1)
    first = last - timedelta(days=CONSUMPTION_WINDOW_DAYS - 1)
    days = await asyncio.to_thread(
        load_daily_consumption,
        store,
        device_id,
        first,
        last,
        zone_name=zone_name,
        now=now,
    )
    recording_since = await asyncio.to_thread(
        store.first_consumption_measurement, device_id
    )
    return ConsumptionHistoryView(
        device_id=device_id,
        timezone=zone_name,
        days=[
            ConsumptionDayView(
                date=d.date.isoformat(), energy_wh=d.energy_wh, source=d.source.value
            )
            for d in days
        ],
        recording_since=recording_since,
    )


@router.get("/devices/{device_id}/dynamic-plan")
async def get_dynamic_plan(
    request: Request,
    device_id: str,
    admin: Annotated[dict | None, Depends(_require_admin_read)],
    policy: PlanPolicy = PlanPolicy.GENTLE,
    target_soc_percent: Annotated[
        float | None, Query(ge=TARGET_SOC_MIN_PERCENT, le=TARGET_SOC_MAX_PERCENT)
    ] = None,
    target_time: datetime | None = None,
) -> DryRunPlanView:
    """Compute and return the current Dry-Run dynamic charging plan for one device.

    Lazy planning on request: the plan is recomputed deterministically from the current local data
    every call, with no provider I/O. A missing precondition returns an understandable status with
    HTTP 200, not a 500.

    ``policy`` (``gentle`` or ``meet_deadline``) and the optional pair ``target_soc_percent`` /
    ``target_time`` (timezone-aware) steer the plan; the reserve is always the Energy Manager's
    effective minimum SoC. Both are planning inputs only: nothing is executed.
    """
    del admin
    _device_or_404(request, device_id)
    if (target_soc_percent is None) != (target_time is None):
        raise HTTPException(
            422, "target_soc_percent and target_time must be given together"
        )
    if target_time is not None and target_time.tzinfo is None:
        raise HTTPException(422, "target_time must include a UTC offset")
    runtime = request.app.state.runtime
    now = runtime.clock.now()
    site_zone = (
        getattr(getattr(request.app.state, "weather_config", None), "timezone", None)
        or "UTC"
    )
    today_date = now.astimezone(ZoneInfo(site_zone)).date()
    tomorrow_date = today_date + timedelta(days=1)
    if runtime.energy is None or not runtime.energy.device_energy_active(device_id):
        # Off (also: no stored mode) is operational rest: read no input and run no optimizer.
        return DryRunPlanView(
            device_id=device_id,
            status=PlanStatus.ENERGY_MANAGER_OFF.value,
            detail="Energy Manager is off for this inverter; the charging plan is inactive.",
            horizon_hours=0.0,
            timezone=site_zone,
            today=DryRunDayView(date=today_date.isoformat(), status="unavailable"),
            tomorrow=DryRunDayView(date=tomorrow_date.isoformat(), status="unavailable"),
        )
    store = _admin_store_or_503(request)

    # Spot prices and solar forecast from the rolling SQLite tables; no provider call is made here.
    spot_config = (
        getattr(request.app.state, "spotmarket_config", None) or SpotMarketConfig()
    )
    spot_rows = (
        await asyncio.to_thread(
            store.get_spot_market_intervals, spot_config_key(spot_config)
        )
        if spot_config.enabled
        else []
    )
    prices = (
        spot_snapshot_from_rows(spot_config, spot_rows) if spot_config.enabled else None
    )

    weather_config = (
        getattr(request.app.state, "weather_config", None) or WeatherConfig()
    )
    solar = None
    if weather_config.is_forecastable():
        _, first_day, last_day = local_three_day_window(
            now, weather_config.timezone or "UTC"
        )
        solar_rows = await asyncio.to_thread(
            store.get_solar_forecast_intervals,
            solar_config_key(weather_config),
            first_day.isoformat(),
            last_day.isoformat(),
        )
        solar = solar_forecast_from_rows(solar_rows)

    # Priority: persisted expert override, then the cache-only verified source, else unavailable.
    # The same resolver feeds the device list; nothing here touches the device.
    limits = (await _planning_limits(request)).get(device_id)
    battery_facts = resolve_device_battery_facts(
        limits, runtime.battery_capacity, device_id
    )
    readings_port = runtime.energy_readings
    battery_provider = EnergyReadingsBatteryStateProvider(
        readings_port,
        device_id=device_id,
        usable_capacity_wh=battery_facts.usable_capacity_wh,
        charge_power_w=limits.max_charge_power_w if limits is not None else None,
        discharge_power_w=limits.max_discharge_power_w if limits is not None else None,
        clock=runtime.clock,
        # Only a verified, fresh live limit counts; without the dispatch controller there is none.
        live_charge_limit_w=(
            runtime.dispatch.verified_live_charge_limit_w(device_id)
            if runtime.dispatch is not None
            else None
        ),
    )

    consumption_provider = StoredConsumptionForecastProvider(
        store,
        device_id=device_id,
        timezone=weather_config.timezone or "UTC",
        clock=runtime.clock,
    )

    # The plan must not propose a target the executor would reject, so clamp to the same effective
    # SoC window the Energy Manager enforces (operator bounds narrowed to the product window).
    reserve, cap = effective_target_window(
        runtime.settings.dispatch_min_soc, runtime.settings.dispatch_max_soc
    )
    try:
        parameters = PlanParameters(
            reserve_soc_percent=reserve,
            max_soc_percent=cap,
            policy=policy,
            target_soc_percent=target_soc_percent,
            target_time=target_time,
        )
    except ValueError as exc:
        raise HTTPException(422, "The planning parameters are invalid") from exc

    result = build_dry_run_plan(
        now=now,
        battery_provider=battery_provider,
        consumption_provider=consumption_provider,
        baseline_household_load_w=runtime.settings.baseline_household_load_w,
        inputs=DryRunInputs(
            prices=prices,
            solar=solar,
            spot_enabled=spot_config.enabled,
            solar_configurable=weather_config.is_forecastable(),
        ),
        parameters=parameters,
        timezone=site_zone,
    )

    plan = result.plan if result.status is PlanStatus.OK else None
    slot_views = _plan_slot_views(plan) if plan is not None else []
    today_slots = [
        slot for slot in slot_views if slot.start.astimezone(ZoneInfo(site_zone)).date() == today_date
    ]
    tomorrow_slots = [
        slot for slot in slot_views if slot.start.astimezone(ZoneInfo(site_zone)).date() == tomorrow_date
    ]
    tomorrow_end = datetime.combine(
        tomorrow_date + timedelta(days=1), datetime.min.time(), ZoneInfo(site_zone)
    )
    today_end = datetime.combine(
        tomorrow_date, datetime.min.time(), ZoneInfo(site_zone)
    )
    today_complete = bool(today_slots) and today_slots[-1].end >= today_end
    tomorrow_complete = bool(tomorrow_slots) and tomorrow_slots[-1].end >= tomorrow_end
    tomorrow_pending = (
        result.status is PlanStatus.OK
        and spot_config.enabled
        and weather_config.is_forecastable()
        and solar is not None
        and bool(solar.intervals)
        and solar.intervals[-1].end >= tomorrow_end
        and (
            prices is None
            or not prices.intervals
            or prices.intervals[-1].end < tomorrow_end
        )
    )
    return DryRunPlanView(
        device_id=device_id,
        status=result.status.value,
        detail=result.detail,
        created_at=plan.created_at if plan is not None else None,
        valid_until=plan.valid_until if plan is not None else None,
        horizon_hours=result.horizon.total_seconds() / 3600.0,
        timezone=site_zone,
        today=DryRunDayView(
            date=today_date.isoformat(),
            status="ok" if today_complete else "unavailable",
            slots=today_slots,
        ),
        tomorrow=DryRunDayView(
            date=tomorrow_date.isoformat(),
            status="ok" if tomorrow_complete else "pending" if tomorrow_pending else "unavailable",
            slots=tomorrow_slots,
        ),
        forecast_basis=result.forecast_basis,
        consumption_history_available=result.consumption_history_available,
        forecast_confidence=result.forecast_confidence,
        battery=_battery_view(result.battery_state),
        slots=slot_views,
        window=DryRunForecastWindowView(
            consumption_from=result.window.consumption_from,
            consumption_until=result.window.consumption_until,
            solar_from=result.window.solar_from,
            solar_until=result.window.solar_until,
            prices_from=result.window.prices_from,
            prices_until=result.window.prices_until,
        ),
        **_plan_flag_fields(plan),
    )
