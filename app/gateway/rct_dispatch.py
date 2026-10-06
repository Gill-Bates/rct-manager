#!/usr/bin/env python3
#
# app/gateway/rct_dispatch.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""RCT adapter for the vendor-neutral battery dispatch port (REQ-054/055)."""

from app.dispatch.capabilities import CapabilityName, CapabilityRegistry
from app.dispatch.models import ControlTelemetry, DeviceControlSnapshot, PowerSetpoint
from app.errors import DeviceApiError
from app.gateway.conventions import RctBatteryPowerConvention, RctGridPowerConvention
from app.gateway.rct import RctGateway


class RctDispatchGateway:
    REQUIRED_WRITES = (
        "power_mng_soc_strategy",
        "power_mng_soc_target_set",
        "power_mng_battery_power_extern",
        "power_mng_use_grid_power_enable",
    )

    def __init__(
        self,
        gateway: RctGateway,
        *,
        capabilities: CapabilityRegistry,
        write_soc_target: bool = False,
        limit_export_during_discharge: bool = False,
    ) -> None:
        self._rct = gateway
        # Deployment decisions: they do not change at runtime, and required_metric_names() needs
        # them without a DispatchConfig at hand.
        self._write_soc_target = write_soc_target
        self._limit_export_during_discharge = limit_export_during_discharge
        # The capability values are read per call and per device instead: they differ between the
        # devices of one process, and a verification has to take effect live, without a restart.
        self._capabilities = capabilities

    def _battery_convention(self, device_id: str) -> RctBatteryPowerConvention:
        record = self._capabilities.record(device_id, CapabilityName.BATTERY_POWER_SIGN)
        return RctBatteryPowerConvention(record.battery_discharge_positive)

    def _grid_convention(self, device_id: str) -> RctGridPowerConvention:
        record = self._capabilities.record(device_id, CapabilityName.GRID_POWER_SIGN)
        return RctGridPowerConvention(record.grid_import_positive)

    def _external_strategy_code(self, device_id: str) -> int | None:
        return self._capabilities.record(device_id, CapabilityName.WRITE_PATH).soc_strategy_external_code

    @staticmethod
    def _number(value: object, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise DeviceApiError("protocol_error", name=name)
        return float(value)

    async def read_soc(self, device_id: str) -> float:
        reading = await self._rct.read_system(device_id, "battery_soc")
        # The catalog reports battery_soc as a ratio. The public dispatch contract uses percent.
        value = self._number(reading.value, reading.name)
        return value * 100.0 if value <= 1.5 else value

    async def read_control_telemetry(self, device_id: str) -> ControlTelemetry:
        soc = await self._rct.read_system(device_id, "battery_soc")
        grid = await self._rct.read_system(device_id, "grid_power")
        battery = await self._rct.read_system(device_id, "battery_power")
        load = await self._rct.read_system(device_id, "household_load_power")
        soc_value = self._number(soc.value, soc.name)
        return ControlTelemetry(
            soc_percent=soc_value * 100.0 if soc_value <= 1.5 else soc_value,
            soc_age_seconds=soc.age_seconds,
            soc_source=soc.source,
            grid_import_w=self._grid_convention(device_id).import_watts(
                self._number(grid.value, grid.name)
            ),
            grid_age_seconds=grid.age_seconds,
            grid_source=grid.source,
            battery_setpoint=self._battery_convention(device_id).setpoint(
                self._number(battery.value, battery.name)
            ),
            battery_age_seconds=battery.age_seconds,
            battery_source=battery.source,
            household_load_w=self._number(load.value, load.name),
            household_age_seconds=load.age_seconds,
        )

    async def read_snapshot(self, device_id: str) -> DeviceControlSnapshot:
        strategy = await self._rct.read_system(device_id, "power_mng_soc_strategy")
        target = await self._rct.read_system(device_id, "power_mng_soc_target_set")
        power = await self._rct.read_system(device_id, "power_mng_battery_power_extern")
        grid_charge = await self._rct.read_system(device_id, "power_mng_use_grid_power_enable")
        if not isinstance(strategy.value, int) or isinstance(strategy.value, bool):
            raise DeviceApiError("protocol_error", name=strategy.name)
        if not isinstance(grid_charge.value, bool):
            raise DeviceApiError("protocol_error", name=grid_charge.name)
        return DeviceControlSnapshot(
            battery_setpoint=self._battery_convention(device_id).setpoint(
                self._number(power.value, power.name)
            ),
            soc_target_ratio=self._number(target.value, target.name),
            soc_strategy_code=strategy.value,
            grid_charge_enabled=grid_charge.value,
            read_at=strategy.measured_at,
            all_fresh=all(r.source == "device" for r in (strategy, target, power, grid_charge)),
        )

    async def apply_setpoint(self, device_id: str, setpoint: PowerSetpoint):
        return await self._rct.write_metric(
            device_id,
            "power_mng_battery_power_extern",
            self._battery_convention(device_id).target(setpoint),
        )

    async def apply_soc_target(self, device_id: str, percent: float):
        return await self._rct.write_metric(device_id, "power_mng_soc_target_set", float(percent) / 100.0)

    async def apply_control_mode(self, device_id: str, *, external: bool):
        # Second, independent lock next to the controller's capability gate (defense in depth): a
        # future caller of this adapter cannot bypass the gate unnoticed.
        code = self._external_strategy_code(device_id)
        if not external or code is None:
            raise DeviceApiError("dispatch_unverified")
        return await self._rct.write_metric(device_id, "power_mng_soc_strategy", code)

    async def apply_grid_charge(self, device_id: str, *, enabled: bool):
        return await self._rct.write_metric(device_id, "power_mng_use_grid_power_enable", enabled)

    async def restore(self, device_id: str, snapshot: DeviceControlSnapshot, step: int):
        if step == 0:
            return await self.apply_setpoint(device_id, snapshot.battery_setpoint)
        if step == 1:
            return await self.apply_grid_charge(device_id, enabled=snapshot.grid_charge_enabled)
        if step == 2:
            return await self._rct.write_metric(
                device_id, "power_mng_soc_target_set", float(snapshot.soc_target_ratio)
            )
        if step == 3:
            return await self._rct.write_metric(
                device_id, "power_mng_soc_strategy", snapshot.soc_strategy_code
            )
        raise ValueError("invalid restore step")

    def required_metric_names(self) -> tuple[str, ...]:
        return self.REQUIRED_WRITES
