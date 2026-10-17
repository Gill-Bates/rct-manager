#!/usr/bin/env python3
#
# app/optimizer/runtime_sources.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Local, read-only inputs for the runtime charging-plan service."""

from collections.abc import Callable
from datetime import datetime, timedelta

from app.admin.forecast_refresh import (
    local_three_day_window,
    solar_config_key,
    spot_config_key,
)
from app.admin.store import AdminStore
from app.api.runtime import Runtime
from app.dispatch.models import DeviceLimits
from app.dispatch.store import DispatchStore
from app.energy.activity import EnergyActivity
from app.energy.models import EnergyMode
from app.energy.soc_window import effective_target_window
from app.errors import DeviceApiError
from app.gateway.base import DeviceState
from app.optimizer.adapters import (
    ConsumptionResolution,
    StoredConsumptionForecastProvider,
    resolve_consumption_forecast,
    solar_forecast_from_rows,
    spot_snapshot_from_rows,
)
from app.optimizer.models import ConsumptionForecast, SolarForecast
from app.optimizer.planner import DynamicChargeOptimizer, PlanParameters, planning_start
from app.optimizer.scheduler import InMemoryPlanScheduler
from app.optimizer.service import PlanningService, ReadingsBatteryStateProvider
from app.spotmarket.models import SpotMarketConfig, SpotPriceSnapshot
from app.weather.models import WeatherConfig


class StoredSpotPriceSource:
    def __init__(
        self, store: AdminStore, config: Callable[[], SpotMarketConfig]
    ) -> None:
        self._store = store
        self._config = config

    def snapshot(self) -> SpotPriceSnapshot | None:
        config = self._config()
        if not config.enabled:
            return None
        rows = self._store.get_spot_market_intervals(spot_config_key(config))
        return spot_snapshot_from_rows(config, rows)


class StoredSolarForecastSource:
    def __init__(self, store: AdminStore, config: Callable[[], WeatherConfig]) -> None:
        self._store = store
        self._config = config

    def forecast(self, now: datetime, horizon: timedelta) -> SolarForecast | None:
        config = self._config()
        if not config.is_forecastable():
            return None
        _, first_day, last_day = local_three_day_window(now, config.timezone or "UTC")
        rows = self._store.get_solar_forecast_intervals(
            solar_config_key(config), first_day.isoformat(), last_day.isoformat()
        )
        return solar_forecast_from_rows(rows)


class StoredConsumptionForecastSource:
    """Runtime consumption input with the same policy as the Dry-Run (shared resolver).

    No history yields the configured baseline and thin history the median fallback, both marked LOW
    confidence in :attr:`last_resolution`; the planner never stalls on "consumption" for want of history.
    """

    def __init__(
        self,
        store: AdminStore,
        device_id: str,
        config: Callable[[], WeatherConfig],
        baseline_household_load_w: float,
    ) -> None:
        self._store = store
        self._device_id = device_id
        self._config = config
        self._baseline_household_load_w = baseline_household_load_w
        self.last_resolution: ConsumptionResolution | None = None

    def forecast(self, now: datetime, horizon: timedelta) -> ConsumptionForecast | None:
        provider = StoredConsumptionForecastProvider(
            self._store,
            device_id=self._device_id,
            timezone=self._config().timezone or "UTC",
        )
        start = planning_start(now)
        resolution = resolve_consumption_forecast(
            provider,
            now=now,
            start=start,
            plannable_span=now + horizon - start,
            planner_horizon=horizon,
            baseline_household_load_w=self._baseline_household_load_w,
        )
        self.last_resolution = resolution
        return resolution.forecast


class StoredDeviceLimitsSource:
    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    def device_limits(self, device_id: str) -> DeviceLimits | None:
        runtime = self._runtime
        if runtime.dispatch is not None:
            return runtime.dispatch.device_limits(device_id)
        settings = runtime.settings
        if settings.hmac_secret is None or not settings.dispatch_db_path.exists():
            return None
        store = DispatchStore(
            settings.dispatch_db_path, settings.hmac_secret.get_secret_value()
        )
        store.initialize()
        return store.get_device_configs().get(device_id)


def _device_started(runtime: Runtime, device_id: str) -> bool:
    """False only while the gateway still reports STARTING (not yet connected); never touches the device."""
    try:
        status = runtime.gateway.device_status(device_id)
        # STARTING lasts until the first heartbeat tick; any completed transaction already proves the link.
        return (
            status.state is not DeviceState.STARTING
            or status.last_success_at is not None
        )
    except DeviceApiError:
        return True  # an unknown device is not a start race; let the normal checks report it


def build_runtime_planning_service(
    runtime: Runtime,
    store: AdminStore,
    device_id: str,
    spot_config: Callable[[], SpotMarketConfig],
    weather_config: Callable[[], WeatherConfig],
) -> PlanningService:
    """Bind one device to persisted forecasts, measured SoC and per-device limits."""
    settings = runtime.settings
    if runtime.energy_readings is None:
        raise RuntimeError("energy readings are not configured")
    reserve, cap = effective_target_window(
        settings.dispatch_min_soc, settings.dispatch_max_soc
    )
    return PlanningService(
        device_id=device_id,
        clock=runtime.clock,
        optimizer=DynamicChargeOptimizer(
            PlanParameters(reserve_soc_percent=reserve, max_soc_percent=cap)
        ),
        scheduler=InMemoryPlanScheduler(runtime.clock),
        prices=StoredSpotPriceSource(store, spot_config),
        solar=StoredSolarForecastSource(store, weather_config),
        consumption=StoredConsumptionForecastSource(
            store, device_id, weather_config, settings.baseline_household_load_w
        ),
        battery=ReadingsBatteryStateProvider(
            readings=runtime.energy_readings,
            dispatch=StoredDeviceLimitsSource(runtime),
            clock=runtime.clock,
            usable_capacity_wh=None,
            max_age_seconds=settings.battery_max_age_seconds,
            capacity_source=runtime.battery_capacity,
        ),
        horizon=timedelta(hours=settings.plan_horizon_hours),
        plan_ttl=timedelta(seconds=settings.plan_ttl_seconds),
        recompute_interval_seconds=settings.plan_recompute_interval_seconds,
        spot_max_age_seconds=settings.spot_max_age_seconds,
        solar_max_age_seconds=settings.solar_max_age_seconds,
        consumption_max_age_seconds=settings.consumption_max_age_seconds,
        battery_max_age_seconds=settings.battery_max_age_seconds,
        device_ready=lambda device_id: _device_started(runtime, device_id),
        # Without an Energy Manager there is no mode, which is the same as off.
        activity=(
            runtime.energy.activity
            if runtime.energy is not None
            else EnergyActivity(lambda _device_id: EnergyMode.OFF, lambda: ())
        ),
    )
