#!/usr/bin/env python3
#
# app/gateway/energy_readings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""RCT adapter for the Energy Manager's readings port (design 2.3.2).

The only place the register names behind the five published figures appear, and the only place the
grid sign convention is applied to them. It reads the **cache** exclusively: a status GET must never
queue a device transaction, because the GUI polls and the device budget must not depend on how many
browsers are open.

Nothing here raises. A missing device, an unknown metric, a non-numeric value or an absent cache
entry yields ``DeviceReading(None, None, stale=True)``: these figures are advisory, and a stale one
must not be able to block a stop command.
"""

import math

from app.cache import CacheFreshness
from app.dispatch.capabilities import CapabilityName, CapabilityRegistry
from app.dispatch.models import PowerDirection
from app.energy.readings import ABSENT, DeviceReading, EnergyReadings
from app.gateway.conventions import RctBatteryPowerConvention, RctGridPowerConvention
from app.gateway.rct import RctGateway

_BATTERY_SOC = "battery_soc"
_GRID_POWER = "grid_power"
_SOLAR_POWER = ("solar_a_power", "solar_b_power")
_HOUSE_LOAD = "household_load_power"
_BATTERY_POWER = "battery_power"


class RctEnergyReadings:
    def __init__(self, gateway: RctGateway, *, capabilities: CapabilityRegistry) -> None:
        self._rct = gateway
        # Read per call and per device: the convention differs between the devices of one process,
        # and a verification has to take effect live, without a restart. An empty registry answers
        # with the same assumption defaults the dispatch adapter already works with.
        self._capabilities = capabilities

    def readings(self, device_id: str) -> EnergyReadings:
        return EnergyReadings(
            battery_soc_percent=self._soc(device_id),
            grid_power_w=self._grid(device_id),
            pv_power_w=self._pv(device_id),
            house_load_w=self._sample(device_id, _HOUSE_LOAD),
            battery_power_w=self._battery(device_id),
        )

    def _sample(self, device_id: str, name: str) -> DeviceReading:
        sample = self._rct.cached_sample(device_id, name)
        if sample is None:
            return ABSENT
        value, age, freshness = sample
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            return ABSENT
        # GRACE is a usable but ageing figure, and the published age says how ageing it is.
        return DeviceReading(float(value), age, freshness is not CacheFreshness.FRESH)

    def _soc(self, device_id: str) -> DeviceReading:
        reading = self._sample(device_id, _BATTERY_SOC)
        if reading.value is None:
            return reading
        # The catalog reports battery_soc as a ratio; the published figure is a percentage. Same
        # rule RctDispatchGateway.read_soc() uses, so both cannot disagree about the unit.
        value = reading.value * 100.0 if reading.value <= 1.5 else reading.value
        return DeviceReading(value, reading.age_seconds, reading.stale)

    def _grid(self, device_id: str) -> DeviceReading:
        reading = self._sample(device_id, _GRID_POWER)
        if reading.value is None:
            return reading
        record = self._capabilities.record(device_id, CapabilityName.GRID_POWER_SIGN)
        watts = RctGridPowerConvention(record.grid_import_positive).import_watts(reading.value)
        return DeviceReading(watts, reading.age_seconds, reading.stale)

    def _battery(self, device_id: str) -> DeviceReading:
        reading = self._sample(device_id, _BATTERY_POWER)
        if reading.value is None:
            return reading
        record = self._capabilities.record(device_id, CapabilityName.BATTERY_POWER_SIGN)
        setpoint = RctBatteryPowerConvention(record.battery_discharge_positive).setpoint(reading.value)
        # Business convention: positive discharges, negative charges, zero rests.
        watts = {PowerDirection.DISCHARGE: setpoint.watts, PowerDirection.CHARGE: -setpoint.watts}.get(
            setpoint.direction, 0.0
        )
        return DeviceReading(watts, reading.age_seconds, reading.stale)

    def _pv(self, device_id: str) -> DeviceReading:
        parts = [self._sample(device_id, name) for name in _SOLAR_POWER]
        present = [part for part in parts if part.value is not None]
        if not present:
            return ABSENT  # None only when neither string is present
        return DeviceReading(
            sum(part.value for part in present),
            max((part.age_seconds for part in present if part.age_seconds is not None), default=None),
            # One ageing present string makes the sum ageing; an absent string does not.
            any(part.stale for part in present),
        )
