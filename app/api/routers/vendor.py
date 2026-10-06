#!/usr/bin/env python3
#
# app/api/routers/vendor.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Vendor-specific diagnostics under ``/api/v1/vendor/rct``; not part of the neutral contract (Requirement 30)."""

from dataclasses import asdict

from fastapi import APIRouter, Depends

from app.api.models_vendor import (
    VendorObjectDescriptor,
    VendorSlaveCollection,
    VendorSlaveDescriptor,
    VendorTransportDescriptor,
)
from app.api.problems import ErrorCode, problem_responses
from app.api.runtime import Runtime, RuntimeDep
from app.errors import DeviceApiError
from app.protocol.values import DEFAULT_WIDTHS
from app.security.dependencies import require_vendor

NOTE = "Vendor specific (RCT); not part of the vendor-neutral contract."
router = APIRouter(
    prefix="/api/v1/vendor/rct",
    tags=["vendor-rct"],
    dependencies=[Depends(require_vendor)],
)
_ERRORS = (
    ErrorCode.MISSING_TOKEN,
    ErrorCode.INVALID_TOKEN,
    ErrorCode.INSUFFICIENT_SCOPE,
    ErrorCode.RATE_LIMITED,
    ErrorCode.NOT_READY,
)


def _vendor(runtime: Runtime):
    if runtime.vendor is None:
        raise DeviceApiError("internal_error")
    return runtime.vendor


@router.get(
    "/objects", summary="Registry objects (vendor specific)", description=NOTE, responses=problem_responses(*_ERRORS)
)
async def objects(runtime: RuntimeDep) -> list[VendorObjectDescriptor]:
    return [
        VendorObjectDescriptor(
            name=e.name,
            object_id=f"0x{e.object_id:08X}",
            protocol_data_type=e.data_type,
            effective_byte_width=e.expected_payload or DEFAULT_WIDTHS[e.data_type],
            idempotent_write=e.idempotent_write,
        )
        for e in _vendor(runtime).objects()
    ]


@router.get(
    "/transports",
    summary="Transport endpoints (vendor specific)",
    description=NOTE,
    responses=problem_responses(*_ERRORS),
)
async def transports(runtime: RuntimeDep) -> list[VendorTransportDescriptor]:
    return [VendorTransportDescriptor(**asdict(t)) for t in _vendor(runtime).transports()]


@router.get(
    "/devices/{device_id}/slaves",
    summary="Slave devices of a plant network (vendor specific)",
    description=NOTE,
    responses=problem_responses(*_ERRORS, ErrorCode.UNKNOWN_DEVICE),
)
async def slaves(device_id: str, runtime: RuntimeDep) -> VendorSlaveCollection:
    runtime.device(device_id)
    runtime.ensure_accepting()
    result = await _vendor(runtime).discover_slaves(device_id)
    return VendorSlaveCollection(
        device_id=device_id,
        slaves=[
            VendorSlaveDescriptor(
                network_id=s.network_id,
                name=s.name,
                ac_power_w=s.ac_power_w,
                battery_power_w=s.battery_power_w,
                battery_soc_ratio=s.battery_soc_ratio,
                fault_index=s.fault_index,
                device_state=s.device_state,
                external_power_w=s.external_power_w,
                software_version=s.software_version,
                serial_number=s.serial_number,
                bms_software_version=s.bms_software_version,
                battery_supported=bool(s.equipment_bits & 1),
                battery_connected=bool(s.equipment_bits & 2),
                dc_supported=bool(s.equipment_bits & 4),
                external_power=bool(s.equipment_bits & 8),
            )
            for s in result.slaves
        ],
        complete=result.complete,
        error_code=result.error_code,
    )
