#!/usr/bin/env python3
#
# app/dispatch/soc_policy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Per-device policy for deriving the device-level SoC target from the business stop goal.

Two different numbers hide behind one name today: the business stop goal an operator means by
"charge to 80 %" (``DispatchIntent.target_soc_percent``, enforced by
``app.dispatch.strategy.target_reached``) and the raw value a device's SoC-target register wants so
external power control actually delivers power. This module owns *which rule* derives the second
from the first; the rule itself is vendor-specific and lives in ``app.gateway.conventions``.

The policy is per device because it is a device/firmware quirk, and it ships as
``BUSINESS_TARGET``, which reproduces today's behaviour exactly. It is an assumption, never a
measurement: nothing here marks anything verified.

This module stays vendor-neutral: like ``app.dispatch.capabilities`` it must not import
``app.gateway.*``.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.dispatch.models import NOTE_MAX_LENGTH, check_printable_ascii


class SocTargetMode(StrEnum):
    BUSINESS_TARGET = "business_target"  # shipped default: the stop goal is written as-is
    BELOW_CURRENT_SOC = "below_current_soc"  # accommodation for the unverified hypothesis H-1


@dataclass(frozen=True, slots=True)
class SocTargetPolicy:
    """How one device derives its SoC-target register value, plus the evidence behind the choice.

    ``note`` is where an operator records what a hardware run actually showed; it is bounded
    printable ASCII exactly like ``CapabilityRecord.note``.
    """

    device_id: str
    mode: SocTargetMode = SocTargetMode.BUSINESS_TARGET
    below_margin_percent: float = 5.0
    note: str | None = None

    def __post_init__(self) -> None:
        check_printable_ascii(self.note, "note", NOTE_MAX_LENGTH)
        # The numeric margin is the long-lived safety bound that derives the device-level SoC
        # target, so it is enforced here and not only in the admin API: a NaN/Infinity would
        # survive every clamp in RctSocTargetConvention, and a negative or oversized margin would
        # push the register target away from the operator's stop goal. Bounds match the admin API.
        if not math.isfinite(self.below_margin_percent) or not 0.0 <= self.below_margin_percent <= 50.0:
            raise ValueError("below_margin_percent must be finite and within 0..50")


class SocTargetPolicyRegistry:
    """In-memory projection of ``dispatch_soc_target_policy``. Only the dispatch port writes to it."""

    def __init__(self, policies: Mapping[str, SocTargetPolicy] | None = None) -> None:
        self._policies: dict[str, SocTargetPolicy] = {
            policy.device_id: policy for policy in (policies or {}).values()
        }

    def policy(self, device_id: str) -> SocTargetPolicy:
        """The stored policy, or the shipped ``BUSINESS_TARGET`` default (today's behaviour)."""
        stored = self._policies.get(device_id)
        if stored is not None:
            return stored
        return SocTargetPolicy(device_id=device_id)

    def replace(self, policy: SocTargetPolicy) -> None:
        """Take a policy over — only after it has durably committed."""
        self._policies[policy.device_id] = policy
