#!/usr/bin/env python3
#
# app/dispatch/battery_facts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Resolve verified battery facts and persisted expert overrides for planning."""

import math
from dataclasses import dataclass
from typing import Literal, Protocol

from app.dispatch.models import DeviceLimits


@dataclass(frozen=True, slots=True)
class BatteryFacts:
    usable_capacity_wh: float | None
    capacity_source: Literal["verified", "expert_override", "unavailable"]


def resolve_battery_facts(
    limits: DeviceLimits | None, *, verified_usable_capacity_wh: float | None = None
) -> BatteryFacts:
    """An existing stored capacity is an expert override; never infer Wh from live voltage."""
    if verified_usable_capacity_wh is not None and (
        not math.isfinite(verified_usable_capacity_wh) or verified_usable_capacity_wh <= 0
    ):
        raise ValueError("verified usable capacity must be finite and positive")
    if limits is not None and limits.usable_capacity_wh is not None:
        return BatteryFacts(limits.usable_capacity_wh, "expert_override")
    if verified_usable_capacity_wh is not None:
        return BatteryFacts(verified_usable_capacity_wh, "verified")
    return BatteryFacts(None, "unavailable")


class VerifiedCapacityPort(Protocol):
    """Source of an evidence-backed usable capacity; must read cache/store only, never the device."""

    def verified_usable_capacity_wh(self, device_id: str) -> float | None: ...


class NoVerifiedCapacity:
    """Default source: no RCT register is proven to yield usable Wh, so it never reports a value.

    battery_ah_capacity x battery_voltage is gross, voltage-dependent and ignores module count and
    state of health; battery_stored_energy / battery_used_energy are unconfirmed (cumulative?) counters.
    """

    def verified_usable_capacity_wh(self, device_id: str) -> float | None:
        return None


def resolve_device_battery_facts(
    limits: DeviceLimits | None, source: VerifiedCapacityPort, device_id: str
) -> BatteryFacts:
    """Single resolution path for the device list and the plan; an invalid source value counts as absent."""
    value = source.verified_usable_capacity_wh(device_id)
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
    ):
        value = None
    return resolve_battery_facts(limits, verified_usable_capacity_wh=value)
