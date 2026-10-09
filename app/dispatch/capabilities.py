#!/usr/bin/env python3
#
# app/dispatch/capabilities.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Per-device hardware capabilities and the dispatch gate (REQ-061..REQ-064).

A capability answers one hardware question that the shipped object catalog does not answer: the
sign conventions, the write path including the external-control strategy code, and the meaning of
the export limit. It is bound to a ``device_id``: a verification of device A never releases device
B, and model equality is not inherited. The shipping state is ``unverified``, which blocks
productive dispatch of the affected mode for exactly that device.

The gate is a pure function so it is testable without a store and without a device. The only way
to dispatch on unverified hardware is the per-device engineering mode, which runs under the
shorter TTL cap and is visibly marked as ``active``.

This module stays vendor-neutral: it must not import ``app.gateway.rct``,
``app.gateway.rct_dispatch`` or ``app.gateway.conventions``.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import datetime
from enum import StrEnum
from typing import Literal

from app.dispatch.models import NOTE_MAX_LENGTH, DispatchMode, check_printable_ascii


class CapabilityName(StrEnum):
    BATTERY_POWER_SIGN = "battery_power_sign_convention"
    GRID_POWER_SIGN = "grid_power_sign_convention"
    WRITE_PATH = "write_path_convention"
    EXPORT_LIMIT = "export_limit_convention"
    SETPOINT_VOLATILITY = "setpoint_volatility"


class CapabilityStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    """One capability of one device: its status plus the evidence and assumptions behind it.

    The assumption defaults are the values the adapter works with until hardware proves better
    ones. They are assumptions, never a release: they travel with ``status=unverified``, which the
    gate refuses.
    """

    device_id: str
    name: CapabilityName
    status: CapabilityStatus = CapabilityStatus.UNVERIFIED
    # Assumptions the adapter works with until a verification replaces them.
    battery_discharge_positive: bool = True  # BATTERY_POWER_SIGN (V-07/V-08)
    grid_import_positive: bool = True  # GRID_POWER_SIGN (V-04/V-05)
    # WRITE_PATH: the partial conditions of the hardware verification plan.
    soc_strategy_external_code: int | None = None  # V-06; always settable, required for verified
    enum_byte_width: int | None = None  # V-02, 1..4
    bool_byte_width: int | None = None  # V-03, 1..4
    write_frame_layout_verified: bool = False  # V-01
    apply_sequence_verified: bool = False  # V-18
    sequence_order_relevant: bool | None = None  # V-18
    soc_target_unit: Literal["ratio", "percent"] = "ratio"  # V-17
    # EXPORT_LIMIT (V-19)
    export_limit_zero_blocks_export: bool | None = None
    export_limit_unit: Literal["watt"] = "watt"
    # SETPOINT_VOLATILITY (V-10) — feeds no gate.
    volatile: bool | None = None
    refresh_interval_seconds: float | None = None
    # Evidence
    verified_device_model: str | None = None
    verified_firmware: str | None = None
    verified_at: datetime | None = None
    verified_by: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        check_printable_ascii(self.note, "note", NOTE_MAX_LENGTH)
        # Capability-specific value ranges are enforced here regardless of status, so the admin API
        # is only an additional input bound, never the single safe one (H6): a directly constructed
        # or historically mis-persisted record must not carry an out-of-range code or byte width.
        # The bounds mirror app.admin.dispatch_api.CapabilityUpdate's own Field() limits.
        if self.soc_strategy_external_code is not None and not 0 <= self.soc_strategy_external_code <= 255:
            raise ValueError("soc_strategy_external_code must be within 0..255")
        for width_name in ("enum_byte_width", "bool_byte_width"):
            width = getattr(self, width_name)
            if width is not None and not 1 <= width <= 4:
                raise ValueError(f"{width_name} must be within 1..4")
        if self.refresh_interval_seconds is not None and (
            not math.isfinite(self.refresh_interval_seconds) or not 0.0 <= self.refresh_interval_seconds <= 86_400.0
        ):
            raise ValueError("refresh_interval_seconds must be finite and within 0..86400")
        # Mirrors app.admin.dispatch_api's own _REQUIRED_FOR_VERIFIED/_missing_evidence gate, but
        # enforces it at the dataclass boundary too (H6): a directly constructed/copied record, or
        # a future caller that bypasses the admin API, must not be able to carry a VERIFIED
        # capability record without the evidence that status claims. The dispatch gate decides on
        # status alone (CapabilityRegistry.unverified_for), so a VERIFIED record with the safety
        # evidence missing would otherwise pass the gate.
        if self.status is CapabilityStatus.VERIFIED:
            if self.name is CapabilityName.WRITE_PATH and (
                self.soc_strategy_external_code is None
                or self.enum_byte_width is None
                or self.bool_byte_width is None
                or not self.write_frame_layout_verified
                or not self.apply_sequence_verified
            ):
                raise ValueError("WRITE_PATH cannot be verified without write-path evidence")
            if self.name is CapabilityName.EXPORT_LIMIT and self.export_limit_zero_blocks_export is None:
                raise ValueError("EXPORT_LIMIT cannot be verified without export_limit_zero_blocks_export")

    def to_dict(self) -> dict:
        """JSON-capable projection for the encrypted part of ``dispatch_capabilities``."""
        data = {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name not in ("name", "status", "verified_at")
        }
        data["name"] = self.name.value
        data["status"] = self.status.value
        data["verified_at"] = self.verified_at.isoformat() if self.verified_at is not None else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "CapabilityRecord":
        verified_at = data.get("verified_at")
        return cls(
            device_id=data["device_id"],
            name=CapabilityName(data["name"]),
            status=CapabilityStatus(data["status"]),
            battery_discharge_positive=_strict_bool(data, "battery_discharge_positive", True),
            grid_import_positive=_strict_bool(data, "grid_import_positive", True),
            soc_strategy_external_code=_strict_optional_int(data.get("soc_strategy_external_code")),
            enum_byte_width=_strict_optional_int(data.get("enum_byte_width")),
            bool_byte_width=_strict_optional_int(data.get("bool_byte_width")),
            write_frame_layout_verified=_strict_bool(data, "write_frame_layout_verified", False),
            apply_sequence_verified=_strict_bool(data, "apply_sequence_verified", False),
            sequence_order_relevant=_optional_bool(data.get("sequence_order_relevant")),
            soc_target_unit=data.get("soc_target_unit", "ratio"),
            export_limit_zero_blocks_export=_optional_bool(data.get("export_limit_zero_blocks_export")),
            export_limit_unit=data.get("export_limit_unit", "watt"),
            volatile=_optional_bool(data.get("volatile")),
            refresh_interval_seconds=_optional_float(data.get("refresh_interval_seconds")),
            verified_device_model=data.get("verified_device_model"),
            verified_firmware=data.get("verified_firmware"),
            verified_at=datetime.fromisoformat(verified_at) if verified_at else None,
            verified_by=data.get("verified_by"),
            note=data.get("note"),
        )


def _strict_bool(data: dict, key: str, default: bool) -> bool:
    """Fail-closed read of a safety-relevant bool field (H6): ``bool("false")`` is ``True``, so a
    permissive coercion would silently accept a corrupted/malformed persisted value instead of
    letting the store's existing (ValueError, TypeError) handling treat the row as unreadable.
    """
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a bool, got {type(value).__name__}")
    return value


def _strict_optional_int(value: object) -> int | None:
    """Fail-closed read of a safety-relevant optional int field (H6): ``int("4")`` would silently
    accept a value that was never actually an int.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected int, got {type(value).__name__}")
    return value


def _optional_float(value: object) -> float | None:
    """Fail-closed read of a safety-relevant optional float field (H6): a stored string or bool is
    corruption, not a value to coerce. ``float("1.0")`` would silently accept a non-number.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"expected a number, got {type(value).__name__}")
    return float(value)


def _optional_bool(value: object) -> bool | None:
    """Fail-closed read of a safety-relevant optional bool field (H6): ``bool("false")`` is
    ``True``, so a permissive coercion would silently accept a corrupted persisted value. The
    store's existing (ValueError, TypeError) handling then treats the row as unreadable, which for
    a capability means it reads as unverified — the safe default.
    """
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError(f"expected a bool, got {type(value).__name__}")
    return value


def required_for(mode: DispatchMode, *, limit_export: bool) -> frozenset[CapabilityName]:
    """The capabilities ``mode`` cannot run without.

    ``limit_export`` is ``DispatchConfig.limit_export_during_discharge``: writing the export limit
    needs its own verification, so it only enters the requirement when that write is switched on.
    """
    if mode is DispatchMode.CHARGE_FROM_GRID:
        return frozenset({CapabilityName.WRITE_PATH, CapabilityName.BATTERY_POWER_SIGN})
    if mode is DispatchMode.DISCHARGE_TO_LOAD:
        base = {
            CapabilityName.WRITE_PATH,
            CapabilityName.BATTERY_POWER_SIGN,
            CapabilityName.GRID_POWER_SIGN,
        }
        if limit_export:
            base.add(CapabilityName.EXPORT_LIMIT)
        return frozenset(base)
    if mode is DispatchMode.HOLD:
        # WRITE_PATH because a hold activates external control and therefore needs the external
        # strategy code. BATTERY_POWER_SIGN because the *restore* path writes the captured,
        # possibly non-zero snapshot.battery_setpoint back through the battery convention — a wrong
        # sign there would turn a handback into a charge or discharge. The 0 W hold write itself is
        # sign-free. No new CapabilityName is introduced for HOLD.
        return frozenset({CapabilityName.WRITE_PATH, CapabilityName.BATTERY_POWER_SIGN})
    return frozenset()  # EXPORT_TO_GRID is refused before the gate (409 dispatch_mode_unavailable)


class CapabilityRegistry:
    """In-memory projection of ``dispatch_capabilities``. Only the dispatch port writes to it."""

    def __init__(self, records: Iterable[CapabilityRecord] = ()) -> None:
        self._records: dict[tuple[str, CapabilityName], CapabilityRecord] = {
            (record.device_id, record.name): record for record in records
        }

    def record(self, device_id: str, name: CapabilityName) -> CapabilityRecord:
        """The stored record, or an unverified one with the assumption defaults (REQ-061)."""
        stored = self._records.get((device_id, name))
        if stored is not None:
            return stored
        return CapabilityRecord(device_id=device_id, name=name)

    def all(self, device_id: str) -> tuple[CapabilityRecord, ...]:
        return tuple(self.record(device_id, name) for name in CapabilityName)

    def replace(self, record: CapabilityRecord) -> None:
        """Take a record over — only after it has durably committed (4.4)."""
        self._records[(record.device_id, record.name)] = record

    def unverified_for(
        self, device_id: str, mode: DispatchMode, *, limit_export: bool
    ) -> tuple[CapabilityName, ...]:
        required = required_for(mode, limit_export=limit_export)
        return tuple(
            name
            for name in CapabilityName
            if name in required and self.record(device_id, name).status is not CapabilityStatus.VERIFIED
        )


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    engineering_mode: bool  # "active": this release came about THROUGH the per-device switch
    unverified: tuple[CapabilityName, ...]
    reject_detail: str | None = None  # only set when allowed is False


def evaluate_gate(
    mode: DispatchMode,
    registry: CapabilityRegistry,
    *,
    device_id: str,
    device_engineering_mode: bool,
    limit_export: bool,
) -> GateDecision:
    """Decide whether ``mode`` may run on ``device_id`` — without touching the device.

    ``device_engineering_mode`` is the per-device switch (``enabled``). The returned
    ``engineering_mode`` is ``active``: true only when the release came about through that switch,
    i.e. the switch is on *and* a required capability is still unverified. Only then does dispatch
    run on unverified hardware, and only then does the shorter TTL cap apply.
    """
    unverified = registry.unverified_for(device_id, mode, limit_export=limit_export)
    if not unverified:
        return GateDecision(allowed=True, engineering_mode=False, unverified=())
    if not device_engineering_mode:
        names = ", ".join(name.value for name in unverified)
        return GateDecision(
            allowed=False,
            engineering_mode=False,
            unverified=unverified,
            reject_detail=f"unverified capabilities: {names}",
        )
    # Engineering mode: WRITE_PATH is unverified by definition, so the strategy code may be
    # missing. Without it no external-control write can be formed at all, so the gate refuses here
    # instead of letting the adapter fail after the intent was already persisted (D-08).
    if registry.record(device_id, CapabilityName.WRITE_PATH).soc_strategy_external_code is None:
        return GateDecision(
            allowed=False,
            engineering_mode=False,
            unverified=unverified,
            reject_detail="soc_strategy_external_code required for engineering mode",
        )
    return GateDecision(allowed=True, engineering_mode=True, unverified=unverified)
