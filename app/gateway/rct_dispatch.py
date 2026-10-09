#!/usr/bin/env python3
#
# app/gateway/rct_dispatch.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""RCT adapter for the vendor-neutral battery dispatch port (REQ-054/055)."""

from typing import Literal

from app.dispatch.capabilities import CapabilityName, CapabilityRegistry
from app.dispatch.models import (
    ControlTelemetry,
    DeviceControlSnapshot,
    DispatchMode,
    PowerSetpoint,
)
from app.dispatch.soc_policy import SocTargetPolicyRegistry
from app.errors import DeviceApiError
from app.gateway.conventions import (
    RctBatteryPowerConvention,
    RctGridPowerConvention,
    RctSocTargetConvention,
    soc_percent,
)
from app.gateway.rct import RctGateway


class RctDispatchGateway:
    REQUIRED_WRITES = (
        "power_mng_soc_strategy",
        "power_mng_soc_target_set",
        "power_mng_battery_power_extern",
        "power_mng_use_grid_power_enable",
    )

    SOC_TARGET_REGISTER = "power_mng_soc_target_set"

    @staticmethod
    def soc_target_unit(value: float) -> Literal["ratio", "percent"] | None:
        """The unit a live register value is written in, or ``None`` when it cannot be told.

        1.0 and 0.0 read the same as 1 % / 0 % and 100 % / 0 %, so they decide nothing.
        """
        if 0.02 <= value <= 0.98:
            return "ratio"
        if 2.0 <= value <= 100.0:
            return "percent"
        return None

    def __init__(
        self,
        gateway: RctGateway,
        *,
        capabilities: CapabilityRegistry,
        soc_target_policies: SocTargetPolicyRegistry,
    ) -> None:
        self._rct = gateway
        # The capability values are read per call and per device instead: they differ between the
        # devices of one process, and a verification has to take effect live, without a restart.
        self._capabilities = capabilities
        # Same reasoning for the SoC-target derivation policy, and the same single instance the
        # controller writes to: an operator change must reach the adapter that writes the register.
        self._soc_target_policies = soc_target_policies

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

    def _soc_percent(self, reading) -> float:
        """SoC in percent from a reading, converted by its catalog unit and bounded to 0..100.

        The unit comes from the catalog (``MetricReading.unit``), not from the number's magnitude,
        and the domain bound is enforced here at the adapter boundary because ``ControlTelemetry``
        does not yet carry a SoC-range invariant.
        """
        value = soc_percent(self._number(reading.value, reading.name), reading.unit)
        if not 0.0 <= value <= 100.0:
            raise DeviceApiError("protocol_error", name=reading.name)
        return value

    async def read_soc(self, device_id: str) -> float:
        reading = await self._rct.read_system(device_id, "battery_soc")
        return self._soc_percent(reading)

    async def read_control_telemetry(self, device_id: str) -> ControlTelemetry:
        soc = await self._rct.read_system(device_id, "battery_soc")
        grid = await self._rct.read_system(device_id, "grid_power")
        battery = await self._rct.read_system(device_id, "battery_power")
        load = await self._rct.read_system(device_id, "household_load_power")
        return ControlTelemetry(
            soc_percent=self._soc_percent(soc),
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

    def restore_barrier(self, device_id: str):
        return self._rct.restore_barrier(device_id)

    async def apply_setpoint(self, device_id: str, setpoint: PowerSetpoint):
        return await self._rct.write_metric(
            device_id,
            "power_mng_battery_power_extern",
            self._battery_convention(device_id).target(setpoint),
            system=True,
        )

    async def apply_soc_target(
        self,
        device_id: str,
        *,
        dispatch_mode: DispatchMode,
        stop_target_percent: float,
        soc_percent: float,
    ):
        policy = self._soc_target_policies.policy(device_id)
        # The register's wire unit is attested WRITE_PATH evidence (V-17), not a fixed ratio: a
        # percent-attested device must receive e.g. 80, not 0.8. Reading it here closes the gap
        # where the verification attested a unit that the write then ignored.
        unit = self._capabilities.record(device_id, CapabilityName.WRITE_PATH).soc_target_unit
        try:
            value = RctSocTargetConvention(policy.mode, policy.below_margin_percent).register_value(
                dispatch_mode, stop_target_percent=stop_target_percent, soc_percent=soc_percent, unit=unit
            )
        except ValueError as exc:
            raise DeviceApiError("protocol_error", name="power_mng_soc_target_set") from exc
        return await self._rct.write_metric(device_id, "power_mng_soc_target_set", value, system=True)

    async def apply_control_mode(self, device_id: str, *, external: bool):
        # Second, independent lock next to the controller's capability gate (defense in depth): a
        # future caller of this adapter cannot bypass the gate unnoticed.
        code = self._external_strategy_code(device_id)
        if not external or code is None:
            raise DeviceApiError("dispatch_unverified")
        return await self._rct.write_metric(device_id, "power_mng_soc_strategy", code, system=True)

    async def apply_grid_charge(self, device_id: str, *, enabled: bool):
        return await self._rct.write_metric(device_id, "power_mng_use_grid_power_enable", enabled, system=True)

    async def restore(self, device_id: str, snapshot: DeviceControlSnapshot, step: int):
        if step == 0:
            return await self.apply_setpoint(device_id, snapshot.battery_setpoint)
        if step == 1:
            return await self.apply_grid_charge(device_id, enabled=snapshot.grid_charge_enabled)
        if step == 2:
            return await self._rct.write_metric(
                device_id, "power_mng_soc_target_set", float(snapshot.soc_target_ratio), system=True
            )
        if step == 3:
            return await self._rct.write_metric(
                device_id, "power_mng_soc_strategy", snapshot.soc_strategy_code, system=True
            )
        raise ValueError("invalid restore step")

    def required_metric_names(self) -> tuple[str, ...]:
        return self.REQUIRED_WRITES
