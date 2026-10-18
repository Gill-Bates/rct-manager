#!/usr/bin/env python3
#
# app/dispatch/live_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Vendor-neutral Live Charge Envelope contract and provider seam.

The optimizer/business layer requests a charge power; dispatch is responsible ONLY for safe
execution. The effective charge power is

    min(requested_power, configured_device_limit, verified_fresh_live_limit_if_available)

This module defines the live-limit term of that minimum: an explicit domain contract carrying a
value, when it was measured, and where it came from, plus the provider seam dispatch reads it
through. Nothing here is RCT-specific.

A missing, invalid, stale or unverified live limit is NEVER turned into an invented number: a
provider returns ``None`` and dispatch falls back to the already-configured static device limit
(``DeviceLimits.max_charge_power_w``). A ``ChargePowerLimit`` therefore only ever represents a value
that is BOTH hardware-verified by its producer AND positive/finite; its freshness is decided
separately by the caller against a configurable maximum age (``is_fresh``).
"""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.clock import require_aware


@dataclass(frozen=True, slots=True)
class ChargePowerLimit:
    """One hardware-verified live charge-power ceiling, with its measurement time and provenance.

    ``value_w`` is a positive, finite power in watts; a producer that cannot verify a real ceiling
    returns ``None`` from the provider instead of constructing one of these. ``measured_at`` is the
    timezone-aware instant the value was measured (freshness is judged against it by ``is_fresh``,
    not assumed). ``source`` names the producer/provenance so a log can say where a derate came
    from. Construction rejects anything that could be mistaken for a limit but is not one.
    """

    value_w: float
    measured_at: datetime
    source: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.value_w):
            raise ValueError("charge power limit value_w must be finite")
        if self.value_w <= 0:
            raise ValueError("charge power limit value_w must be positive")
        # The project's single awareness rule: a tzinfo that actually yields a UTC offset, not
        # just a non-None tzinfo (P3-2 DRY). require_aware raises ValueError, the established contract.
        require_aware(self.measured_at, "charge power limit measured_at")
        if not self.source:
            raise ValueError("charge power limit source must not be empty")

    def is_fresh(self, now: datetime, max_age_seconds: float) -> bool:
        """Whether this limit may still constrain dispatch at ``now``.

        Fresh means the measurement age is in ``[0, max_age_seconds]``. A measurement timestamped in
        the future (negative age) is NOT fresh — a clock disagreement must not be read as a brand
        new reading — and a non-finite age is never fresh.
        """
        age = (now - self.measured_at).total_seconds()
        if not math.isfinite(age):
            return False
        return 0.0 <= age <= max_age_seconds


class LiveChargeLimitProvider(Protocol):
    """The seam dispatch reads a live charge-power ceiling through.

    Returns a ``ChargePowerLimit`` only when a producer can verify a real, hardware-backed ceiling
    for the device; otherwise ``None`` (absent / invalid / unverified). It must NEVER invent a limit
    from an unverified register or a derivation that has not itself been verified — in that case the
    configured static device limit governs. Returning ``None`` is always the safe answer.
    """

    def live_charge_limit(self, device_id: str) -> ChargePowerLimit | None: ...


class NullLiveChargeLimitProvider:
    """The default provider: no live limit for any device.

    This is the inert-until-verified state. The envelope's contract, clamp chain, freshness rule and
    immediate-derate safety write are all exercised through the seam, but no live value is ever
    emitted, so dispatch falls back to the configured static device limit. A real producer may
    replace this only once its hardware register semantics are verified on the device.
    """

    def live_charge_limit(self, device_id: str) -> ChargePowerLimit | None:
        return None
