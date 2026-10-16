#!/usr/bin/env python3
#
# app/gateway/characterization_readings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""RCT adapter for the read-only Charge Characterization port.

Mirrors the two-layer split of the Energy Manager readings (``app.energy.readings`` port /
``app.gateway.energy_readings`` RCT adapter): this is the one place the register names behind a
characterization sample appear. It reads the **cache** exclusively (never ``read_system``, never a
device transaction — the same rule as ``RctEnergyReadings``) so a characterization read cannot cost
the device budget. ``sample()`` never raises on device telemetry: a missing or invalid cache entry
and a non-numeric caller value all become an absent/flagged field. The one precondition is the
caller's own ``timestamp``, which must be timezone-aware — it is not device telemetry, it is passed
through unchanged, and ``CharacterizationSample`` raises on a naive value.

The adapter honours the sample's availability contract strictly. A field is populated ONLY when its
semantics are verified:

- ``battery_voltage``, ``battery_current``, ``maximum_charge_current`` and ``battery_temperature``
  carry UNVERIFIED catalog semantics (the catalog states measuring point, sign convention and filter
  characteristics are "not specified by the protocol"). They are ALWAYS left absent and flagged
  here, so a later measurement run can never mistake an unverified cache value for a usable battery
  voltage or BMS charge limit. Only an explicit future hardware verification may unlock them.
- ``grid_power_w`` and ``measured_battery_power_w`` are sign-normalized through a capability whose
  defaults are only ASSUMPTIONS until verified; the capability contract is explicit that
  ``status=unverified`` is not a go-ahead. So they are populated only when the relevant sign
  capability is VERIFIED for the device, and left absent/flagged otherwise.

Each populated field carries its own cache age (``MeasuredValue.age_seconds``) so the independent
ages of the registers are preserved. The three raw cumulative energy counters
(``energy_grid_load_total``, ``energy_e_load_total``, ``battery_stored_energy``) are recorded
VERBATIM — no differencing, no efficiency, no derivation.
"""

import math
from datetime import datetime

from app.dispatch.capabilities import CapabilityName, CapabilityRegistry
from app.energy.characterization import (
    MEASURED_FIELDS,
    CharacterizationReadingsPort,
    CharacterizationSample,
    MeasuredValue,
)
from app.gateway.conventions import (
    BATTERY_SOC_UNIT,
    RctBatteryPowerConvention,
    RctGridPowerConvention,
    battery_business_watts,
    finite_sample_value,
    soc_percent,
)
from app.gateway.rct import RctGateway

_BATTERY_SOC = "battery_soc"
_GRID_POWER = "grid_power"
_HOUSE_LOAD = "household_load_power"
_BATTERY_POWER = "battery_power"
# Cumulative energy counters, recorded raw. Wrap/reset semantics unconfirmed. Confirmed present in
# app/catalog/objects.json at feature/v1.2.1: energy_grid_load_total (0x62FBE7DC, Wh),
# energy_e_load_total (0xEFF4B537, Wh), battery_stored_energy (0x5570401B, Wh). Note:
# energy_e_grid_load_total does NOT exist on this build; energy_e_grid_load_total_sum (0x1BFA5A33)
# is a DIFFERENT counter and is deliberately NOT substituted here.
_GRID_LOAD_TOTAL = "energy_grid_load_total"
_LOAD_TOTAL = "energy_e_load_total"
_BATTERY_STORED_ENERGY = "battery_stored_energy"


class RctCharacterizationReadings(CharacterizationReadingsPort):
    def __init__(self, gateway: RctGateway, *, capabilities: CapabilityRegistry) -> None:
        self._rct = gateway
        # Read per call and per device: the sign convention differs between devices of one process
        # and a verification must take effect live, exactly as the energy-readings adapter does.
        self._capabilities = capabilities

    def _cached(self, device_id: str, name: str) -> tuple[float, float | None] | None:
        """A cached numeric value and its age, or ``None`` when absent / non-numeric / non-finite."""
        sample = self._rct.cached_sample(device_id, name)
        if sample is None:
            return None
        value, age = sample[0], sample[1]
        number = finite_sample_value(value)
        return None if number is None else (number, age)

    def _raw(self, device_id: str, name: str) -> MeasuredValue | None:
        cached = self._cached(device_id, name)
        return None if cached is None else MeasuredValue(cached[0], cached[1])

    def _verified(self, device_id: str, name: CapabilityName) -> bool:
        return self._capabilities.record(device_id, name).is_verified

    def _grid_power(self, device_id: str) -> MeasuredValue | None:
        # The grid sign convention's defaults are assumptions; only a VERIFIED capability makes a
        # business-normalized figure trustworthy for a measurement dataset.
        if not self._verified(device_id, CapabilityName.GRID_POWER_SIGN):
            return None
        cached = self._cached(device_id, _GRID_POWER)
        if cached is None:
            return None
        record = self._capabilities.record(device_id, CapabilityName.GRID_POWER_SIGN)
        return MeasuredValue(RctGridPowerConvention(record.grid_import_positive).import_watts(cached[0]), cached[1])

    def _battery_power(self, device_id: str) -> MeasuredValue | None:
        if not self._verified(device_id, CapabilityName.BATTERY_POWER_SIGN):
            return None
        cached = self._cached(device_id, _BATTERY_POWER)
        if cached is None:
            return None
        record = self._capabilities.record(device_id, CapabilityName.BATTERY_POWER_SIGN)
        watts = battery_business_watts(RctBatteryPowerConvention(record.battery_discharge_positive), cached[0])
        return MeasuredValue(watts, cached[1])

    def _battery_soc(self, device_id: str) -> MeasuredValue | None:
        cached = self._cached(device_id, _BATTERY_SOC)
        if cached is None:
            return None
        try:
            return MeasuredValue(soc_percent(cached[0], BATTERY_SOC_UNIT), cached[1])
        except ValueError:
            return None

    @staticmethod
    def _caller_value(value: float | None) -> MeasuredValue | None:
        # A caller-supplied value (requested/commanded power) has no cache age. A non-numeric or
        # non-finite input becomes absent rather than raising: the port never raises.
        if value is None or isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if not math.isfinite(value):
            return None
        return MeasuredValue(float(value))

    def sample(
        self,
        device_id: str,
        *,
        timestamp: datetime,
        requested_power_w: float | None = None,
        commanded_power_w: float | None = None,
    ) -> CharacterizationSample:
        values: dict[str, MeasuredValue | None] = {
            "requested_power_w": self._caller_value(requested_power_w),
            "commanded_power_w": self._caller_value(commanded_power_w),
            "measured_battery_power_w": self._battery_power(device_id),
            "grid_power_w": self._grid_power(device_id),
            "house_load_w": self._raw(device_id, _HOUSE_LOAD),
            "battery_soc": self._battery_soc(device_id),
            # UNVERIFIED catalog semantics: always absent and flagged until a hardware verification
            # unlocks them. Reading them here as regular measurements would let a later run treat an
            # unverified value as a usable battery voltage / BMS charge limit.
            "battery_voltage": None,
            "battery_current": None,
            "maximum_charge_current": None,
            "battery_temperature": None,
            # Semantic domain field -> concrete RCT register (the mapping lives only here).
            "grid_import_energy_total_raw_wh": self._raw(device_id, _GRID_LOAD_TOTAL),
            "load_energy_total_raw_wh": self._raw(device_id, _LOAD_TOTAL),
            "battery_stored_energy_raw_wh": self._raw(device_id, _BATTERY_STORED_ENERGY),
        }
        unavailable = frozenset(name for name in MEASURED_FIELDS if values[name] is None)
        # Missing or invalid telemetry (absent cache entry, non-numeric or non-finite value) and a
        # non-numeric caller value have already been turned into absent/flagged fields above, so no
        # device-side input can make this raise. The one remaining precondition is the caller's own
        # tz-aware timestamp, which the contract requires (see the port docstring); it is not device
        # telemetry and is validated by CharacterizationSample.
        return CharacterizationSample(timestamp=timestamp, unavailable_fields=unavailable, **values)
