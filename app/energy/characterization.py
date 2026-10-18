#!/usr/bin/env python3
#
# app/energy/characterization.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Vendor-neutral, read-only Charge Characterization measurement contract.

This is PREPARATION only. It aggregates independently-aged cached device observations, and
nothing more. It explicitly does NOT:

- run an automated characterization sequence (no 500 -> 1000 -> ... -> 9000 W stepping);
- compute any efficiency (no eta_AC2BAT), no efficiency curve, no preferred charging power;
- difference the raw cumulative energy counters (grid-import, load and battery-stored energy are
  recorded VERBATIM); ``dE_grid - dE_house`` is NOT assumed to be a validated battery-input-energy
  measurement, and counter wrap/reset semantics are unconfirmed and out of scope;
- own any vendor register-restore sequence — a future active run must use the existing Energy
  Manager / dispatch command path and the existing AUTO / snapshot-restore path.

This module is the vendor-neutral domain contract: it names NO device register, object-ID or sign
convention. Which concrete registers feed these fields is the adapter's business
(``app.gateway.characterization_readings``), exactly as the Energy Manager readings port keeps its
register names in ``app.gateway.energy_readings``.

Two properties make this a measurement contract rather than a bag of nullable numbers:

- Per-field freshness. Each measured value is a ``MeasuredValue`` carrying its own ``age_seconds``,
  because the cache ages independently per register. The sample must NOT present an 8 s-old battery
  value and a 100 ms-old grid value as one synchronous instant, so ``max_sample_age_seconds``
  reports the oldest contributing age for the skew to be visible.
- Explicit unavailability. ``None`` alone cannot distinguish "not cached" from "register unverified"
  from "invalid value", so every absent/unverified field is also named in ``unavailable_fields``.
  A field whose semantics or availability is not verified stays absent AND flagged, never
  fabricated. In particular ``battery_voltage``, ``battery_current``, ``maximum_charge_current`` and
  ``battery_temperature`` carry unverified device semantics (measuring point, sign convention and
  filter characteristics are unconfirmed); they are captured as optional observations only and never
  used to constrain dispatch.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.clock import is_aware

# The measured fields of a sample, in declaration order. Used to validate values and to let the
# sampler flag the ones it could not populate.
MEASURED_FIELDS: tuple[str, ...] = (
    "requested_power_w",
    "commanded_power_w",
    "measured_battery_power_w",
    "grid_power_w",
    "house_load_w",
    "battery_soc",
    "battery_voltage",
    "battery_current",
    "maximum_charge_current",
    "battery_temperature",
    "grid_import_energy_total_raw_wh",
    "load_energy_total_raw_wh",
    "battery_stored_energy_raw_wh",
)


@dataclass(frozen=True, slots=True)
class MeasuredValue:
    """One measured number with its own measurement age.

    ``age_seconds`` is the age of the cache entry the value came from at sample time, so the
    independent ages of several registers are preserved instead of being collapsed onto one shared
    timestamp. ``None`` age means the age was not available (e.g. a caller-supplied value such as
    the requested/commanded power, which has no cache age).
    """

    value: float
    age_seconds: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.value):
            raise ValueError("measured value must be finite")
        if self.age_seconds is not None and (not math.isfinite(self.age_seconds) or self.age_seconds < 0):
            raise ValueError("measured value age_seconds must be a non-negative finite number")


@dataclass(frozen=True, slots=True)
class CharacterizationSample:
    """One read-only capture of independently-aged observations. Required ``timestamp``; every measurement optional.

    Each measured field is a ``MeasuredValue | None``. ``requested_power_w`` / ``commanded_power_w``
    are supplied by the caller from the dispatch status (what was asked for / commanded), not read
    from a register, so they carry no cache age. The remaining fields come from telemetry; any that
    is absent, non-numeric, non-finite or unverified is ``None`` AND listed in ``unavailable_fields``.
    The three ``*_raw`` counters are the cumulative energy registers recorded exactly as read — no
    differencing, no efficiency.
    """

    timestamp: datetime
    requested_power_w: MeasuredValue | None = None
    commanded_power_w: MeasuredValue | None = None
    measured_battery_power_w: MeasuredValue | None = None
    grid_power_w: MeasuredValue | None = None
    house_load_w: MeasuredValue | None = None
    battery_soc: MeasuredValue | None = None
    # UNVERIFIED catalog semantics (measuring point / sign / filter "not specified by the
    # protocol"): captured as optional observations only, never used to derive a limit.
    battery_voltage: MeasuredValue | None = None
    battery_current: MeasuredValue | None = None
    maximum_charge_current: MeasuredValue | None = None
    battery_temperature: MeasuredValue | None = None
    # Cumulative energy counters in Wh, recorded VERBATIM. Never differenced here; wrap/reset
    # semantics unconfirmed. The battery-stored-energy counter is the operand a future, out-of-scope
    # differencing would need. These are semantic names; the concrete device registers behind them
    # live only in the adapter.
    grid_import_energy_total_raw_wh: MeasuredValue | None = None
    load_energy_total_raw_wh: MeasuredValue | None = None
    battery_stored_energy_raw_wh: MeasuredValue | None = None
    # Fields that could NOT be populated (not cached / unverified / invalid). Named explicitly so a
    # None value is distinguishable from a measured zero and from a field that was simply not asked
    # for. A field is in exactly one of: populated (not here), or absent (here).
    unavailable_fields: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not is_aware(self.timestamp):
            raise ValueError("characterization sample timestamp must be timezone-aware")
        unknown = set(self.unavailable_fields) - set(MEASURED_FIELDS)
        if unknown:
            raise ValueError(f"unavailable_fields names unknown measurement(s): {sorted(unknown)}")
        # Enforce the bijection across every measured field: a field is None IFF its name is in
        # unavailable_fields. This removes the exact ambiguity unavailable_fields exists to kill —
        # a None field with no flag, or a flagged field that still carries a value — so a sample is
        # self-consistent no matter which adapter built it.
        for name in MEASURED_FIELDS:
            is_absent = getattr(self, name) is None
            is_flagged = name in self.unavailable_fields
            if is_absent and not is_flagged:
                raise ValueError(f"{name} is absent but not listed in unavailable_fields")
            if is_flagged and not is_absent:
                raise ValueError(f"{name} is populated but also listed as unavailable")

    @property
    def max_sample_age_seconds(self) -> float | None:
        """The oldest contributing cache age, so measurement skew across registers is visible.

        ``None`` when no populated field carries an age (only caller-supplied values are present).
        A future consumer must treat a large value as "these readings are not truly simultaneous".
        """
        ages = [
            getattr(self, name).age_seconds
            for name in MEASURED_FIELDS
            if getattr(self, name) is not None and getattr(self, name).age_seconds is not None
        ]
        return max(ages) if ages else None


class CharacterizationReadingsPort(Protocol):
    """The seam the sampler reads device-side characterization telemetry through.

    Vendor-neutral: it returns a ``CharacterizationSample`` populated from whatever telemetry is
    available for the device, leaving every unavailable/unverified field ``None`` and listing it in
    ``unavailable_fields``. Missing or invalid telemetry never raises and it never queues a device
    transaction — a characterization read is advisory and must not cost the device budget or block a
    stop command. The one precondition the caller must meet is a timezone-aware ``timestamp``: it is
    not device telemetry, and ``CharacterizationSample`` rejects a naive value.
    """

    def sample(
        self,
        device_id: str,
        *,
        timestamp: datetime,
        requested_power_w: float | None = None,
        commanded_power_w: float | None = None,
    ) -> CharacterizationSample: ...
