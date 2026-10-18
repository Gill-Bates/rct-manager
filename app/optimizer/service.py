#!/usr/bin/env python3
#
# app/optimizer/service.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The planning service: wire the forecast sources to the optimizer and serve the current plan.

:class:`PlanningService` owns an :class:`InMemoryPlanScheduler`, the injected input sources and a
bounded recompute loop. On every tick it re-reads the four inputs (spot prices, solar forecast,
household-consumption forecast and the measured battery state), checks each against its configured
maximum age, and — only when all four are present and fresh — builds a :class:`PlanningInput`, runs
the optimizer and stores the resulting :class:`EnergyPlan`. Any stale or missing required input
takes the **no-plan path**: the previous plan is left to expire naturally and nothing is charged on
stale data.

The service never commands hardware. It reads inputs, computes a proposal and stores it; the
read-only API serves it and the separate (default-OFF) handoff is the only thing that may later
offer a slot to the Energy Manager.

Recompute trigger (design Part 3, pinned): a single bounded periodic loop of the same shape as
``run_solar_refresh_loop`` — ``await asyncio.wait_for(self._wake.wait(), timeout=interval)`` then
``self._wake.clear()`` — on the service's **own private** :class:`asyncio.Event`, never a shared
refresh event. A changed input is therefore picked up within at most one
``recompute_interval_seconds``. Concurrent recomputes (an on-demand wake landing on top of a tick)
are collapsed with :class:`SingleFlight` keyed per device.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal, Protocol

from app.clock import Clock
from app.dispatch.battery_facts import (
    VerifiedCapacityPort,
    resolve_battery_facts,
    resolve_device_battery_facts,
)
from app.dispatch.models import DeviceLimits
from app.energy.activity import EnergyActivity
from app.energy.readings import EnergyReadingsPort
from app.optimizer.adapters import FORECAST_BASIS
from app.optimizer.base import (
    ConsumptionForecastProvider,
    EnergyOptimizer,
    SolarForecastProvider,
)
from app.optimizer.models import (
    BatteryState,
    ConsumptionForecast,
    EnergyPlan,
    ForecastBasis,
    ForecastConfidence,
    PlanningInput,
    SolarForecast,
)
from app.optimizer.planner import SLOT, planning_start
from app.optimizer.scheduler import InMemoryPlanScheduler
from app.scheduling.singleflight import SingleFlight
from app.spotmarket.models import SpotPriceSnapshot

log = logging.getLogger(__name__)

_PENDING_RETRY_SECONDS = 5.0


class _TimedInterval(Protocol):
    @property
    def start(self) -> datetime: ...

    @property
    def end(self) -> datetime: ...


def _covers_plan_horizon(
    intervals: Sequence[_TimedInterval], start: datetime, end: datetime
) -> bool:
    """Require contiguous source coverage through the final full planner slot."""
    cursor = start
    tolerance = timedelta(seconds=1)
    for interval in intervals:
        if interval.end <= cursor:
            continue
        if interval.start > cursor + tolerance:
            return False
        cursor = max(cursor, interval.end)
        if cursor >= end - tolerance:
            return True
    return False


class SpotPriceSource(Protocol):
    """Supplies the current day-ahead price snapshot, or ``None`` when none is available."""

    def snapshot(self) -> SpotPriceSnapshot | None: ...


class BatteryStateProvider(Protocol):
    """Supplies the current measured battery state, or ``None`` when it cannot be built safely."""

    def current(self, device_id: str) -> BatteryState | None: ...


class DeviceLimitsSource(Protocol):
    def device_limits(self, device_id: str) -> DeviceLimits | None: ...


@dataclass(frozen=True, slots=True)
class DataAge:
    """The age in seconds of each input, for the read-only surface (``None`` when the input is absent)."""

    spot_seconds: float | None
    solar_seconds: float | None
    consumption_seconds: float | None
    battery_seconds: float | None


@dataclass(frozen=True, slots=True)
class PlanView:
    """A read-only snapshot of the current plan together with the data age of its inputs."""

    plan: EnergyPlan
    data_age: DataAge
    # Consumption input quality as stable enums; the free-form text stays in plan.assumptions.
    forecast_basis: ForecastBasis | None = None
    forecast_confidence: ForecastConfidence | None = None


class ReadingsBatteryStateProvider:
    """Build a :class:`BatteryState` from the real, read-only sources (design Part 3).

    No field is synthesized from a hardware register. ``soc_percent`` / ``measured_at`` come
    from the readings cache; the power bounds and usable capacity come from per-device limits.
    A missing, stale or unconfigured source yields ``None`` and no plan for this cycle.
    """

    def __init__(
        self,
        *,
        readings: EnergyReadingsPort,
        dispatch: DeviceLimitsSource,
        clock: Clock,
        usable_capacity_wh: float | None,
        max_age_seconds: float,
        capacity_source: VerifiedCapacityPort | None = None,
        capacity_pending: Callable[[str], bool] | None = None,
    ) -> None:
        self._capacity_source = capacity_source
        self._capacity_pending = capacity_pending
        self._readings = readings
        self._dispatch = dispatch
        self._clock = clock
        self._usable_capacity_wh = usable_capacity_wh
        self._max_age_seconds = max_age_seconds

    def current(self, device_id: str) -> BatteryState | None:
        limits = self._dispatch.device_limits(device_id)
        if limits is None:
            log.info(
                "No battery state: no static power limits configured for device %s",
                device_id,
            )
            return None
        if self._capacity_source is not None:
            # Same resolver as planning-devices and dynamic-plan, so all three agree on the source.
            facts = resolve_device_battery_facts(
                limits, self._capacity_source, device_id
            )
        else:
            facts = resolve_battery_facts(
                limits, verified_usable_capacity_wh=self._usable_capacity_wh
            )
        capacity_wh = facts.usable_capacity_wh
        if not (capacity_wh and capacity_wh > 0):
            # While the source settles the planner retries every few seconds and logs that state.
            settling = self._capacity_pending is not None and self._capacity_pending(
                device_id
            )
            (log.debug if settling else log.info)(
                "No battery state: usable battery capacity unavailable: no expert override and no verified automatic source"
            )
            return None
        try:
            soc_reading = self._readings.readings(device_id).battery_soc_percent
        except Exception:
            log.warning(
                "No battery state: readings for device %s could not be read",
                device_id,
                exc_info=True,
            )
            return None
        if (
            soc_reading.value is None
            or soc_reading.stale
            or soc_reading.age_seconds is None
        ):
            log.info(
                "No battery state: SoC for device %s is absent or stale", device_id
            )
            return None
        if soc_reading.age_seconds > self._max_age_seconds:
            log.info(
                "No battery state: SoC for device %s is older than the maximum age",
                device_id,
            )
            return None
        measured_at = self._clock.now() - timedelta(seconds=soc_reading.age_seconds)
        try:
            return BatteryState(
                soc_percent=soc_reading.value,
                usable_capacity_wh=capacity_wh,
                max_charge_power_w=limits.max_charge_power_w,
                max_discharge_power_w=limits.max_discharge_power_w,
                measured_at=measured_at,
            )
        except ValueError:
            # A malformed datum is treated as missing data, never silently defaulted.
            log.info(
                "No battery state: the measured values for device %s are invalid",
                device_id,
                exc_info=True,
            )
            return None


class PlanningService:
    """Own the scheduler, the input sources and the bounded recompute loop for one device."""

    def __init__(
        self,
        *,
        device_id: str,
        clock: Clock,
        optimizer: EnergyOptimizer,
        scheduler: InMemoryPlanScheduler,
        prices: SpotPriceSource,
        solar: SolarForecastProvider,
        consumption: ConsumptionForecastProvider,
        battery: BatteryStateProvider,
        horizon: timedelta,
        plan_ttl: timedelta,
        recompute_interval_seconds: float,
        spot_max_age_seconds: float,
        solar_max_age_seconds: float,
        consumption_max_age_seconds: float,
        battery_max_age_seconds: float,
        single_flight: SingleFlight | None = None,
        device_ready: Callable[[str], bool] | None = None,
        activity: EnergyActivity | None = None,
        capacity_pending: Callable[[str], bool] | None = None,
    ) -> None:
        if horizon <= timedelta():
            raise ValueError("horizon must be a positive timedelta")
        if plan_ttl <= timedelta():
            raise ValueError("plan_ttl must be a positive timedelta")
        if recompute_interval_seconds <= 0:
            raise ValueError("recompute_interval_seconds must be positive")
        self._device_id = device_id
        self._clock = clock
        self._optimizer = optimizer
        self._scheduler = scheduler
        self._prices = prices
        self._solar = solar
        self._consumption = consumption
        self._battery = battery
        self._horizon = horizon
        self._plan_ttl = plan_ttl
        self._interval = recompute_interval_seconds
        self._spot_max_age = spot_max_age_seconds
        self._solar_max_age = solar_max_age_seconds
        self._consumption_max_age = consumption_max_age_seconds
        self._battery_max_age_seconds = battery_max_age_seconds
        self._single_flight = single_flight or SingleFlight()
        # None means "always ready". False marks the startup race: the device is not connected yet,
        # so a missing input is pending, not a configuration or hardware-fact gap.
        self._device_ready = device_ready
        # Asked only after the battery state came back None: True while the automatic capacity
        # source still settles, so the retry is quick instead of one full recompute interval.
        self._capacity_pending = capacity_pending
        # None means "always active"; with an activity, mode ``off`` suspends every computation.
        self._activity = activity
        # Why the last recompute was pending; kept per reason so each logs INFO once on entry.
        self._pending_reason: Literal["device", "capacity"] | None = None
        # The service's OWN private wake event: never a shared forecast-refresh event. An in-process
        # caller may set() it to shorten the one-interval worst-case latency.
        self._wake = asyncio.Event()
        self._last_age: DataAge = DataAge(None, None, None, None)
        self._forecast_quality: tuple[ForecastBasis, ForecastConfidence] | None = None

    @property
    def scheduler(self) -> InMemoryPlanScheduler:
        return self._scheduler

    def request_recompute(self) -> None:
        """Ask the loop to recompute before its next scheduled tick (never from a shared event)."""
        self._wake.set()

    def current_view(self) -> PlanView | None:
        """The current plan plus the data age of its inputs, or ``None`` when none is valid.

        An expired plan is reported as absent, because the read goes through
        ``InMemoryPlanScheduler.current_plan()`` which discards an expired plan against the clock.
        """
        plan = self._scheduler.current_plan()
        if plan is None:
            return None
        basis, confidence = self._forecast_quality or (None, None)
        return PlanView(
            plan=plan,
            data_age=self._last_age,
            forecast_basis=basis,
            forecast_confidence=confidence,
        )

    async def recompute(self) -> EnergyPlan | None:
        """Collapse concurrent recomputes for this device onto one computation."""
        return await self._single_flight.run(
            (self._device_id, "plan"), self._recompute_once
        )

    def _active(self) -> bool:
        return self._activity is None or self._activity.device_energy_active(
            self._device_id
        )

    async def _recompute_once(self) -> EnergyPlan | None:
        if not self._active():
            self._invalidate()
            return None
        now = self._clock.now()
        previous_reason = self._pending_reason
        self._pending_reason = None
        if self._device_ready is not None and not self._device_ready(self._device_id):
            self._pending_reason = "device"
            (log.debug if previous_reason == "device" else log.info)(
                "Planning for device %s is pending: the device is not connected yet",
                self._device_id,
            )
            return None
        start = planning_start(now)
        end = start
        while end < now + self._horizon:
            end += SLOT
        if end == start:
            log.info(
                "No plan for device %s: the planning horizon contains no full slot",
                self._device_id,
            )
            self._invalidate()
            return None
        forecast_horizon = end - now
        prices = await asyncio.to_thread(self._prices.snapshot)
        solar = await asyncio.to_thread(self._solar.forecast, now, forecast_horizon)
        consumption = await asyncio.to_thread(
            self._consumption.forecast, now, forecast_horizon
        )
        battery = await asyncio.to_thread(self._battery.current, self._device_id)
        if (
            battery is None
            and self._capacity_pending is not None
            and await asyncio.to_thread(self._capacity_pending, self._device_id)
        ):
            self._pending_reason = "capacity"
            (log.debug if previous_reason == "capacity" else log.info)(
                "Planning for device %s is pending: battery module inventory/topology is still settling",
                self._device_id,
            )
            self._invalidate()
            return None
        # Ages are computed locally and only ever published together with a successful plan, so a
        # surviving old plan can never be paired with a newer failed recompute's data ages.
        ages = self._ages(now, prices, solar, consumption, battery)

        missing = self._stale_or_missing(
            now, start, end, prices, solar, consumption, battery
        )
        if missing:
            log.info(
                "No plan for device %s: required inputs are stale or missing: %s",
                self._device_id,
                missing,
            )
            self._invalidate()
            return None

        assert (
            prices is not None
            and solar is not None
            and consumption is not None
            and battery is not None
        )
        uncovered = [
            name
            for name, intervals in (
                ("prices", prices.intervals),
                ("solar", solar.intervals),
                ("consumption", consumption.intervals),
            )
            if not _covers_plan_horizon(intervals, start, end)
        ]
        if uncovered:
            log.info(
                "No plan for device %s: forecast gaps in %s", self._device_id, uncovered
            )
            self._invalidate()
            return None
        if (
            not self._active()
        ):  # switched off while the inputs were read: compute and store nothing
            self._invalidate()
            return None
        resolution = getattr(self._consumption, "last_resolution", None)
        try:
            planning_input = PlanningInput(
                now=now,
                battery=battery,
                prices=prices,
                solar_forecast=solar,
                consumption_forecast=consumption,
                horizon=self._horizon,
                forecast_basis=(
                    resolution.description if resolution is not None else FORECAST_BASIS
                ),
            )
            plan = self._optimizer.plan(planning_input)
            plan = self._with_expiry(plan, now, prices, solar, consumption, battery)
        except ValueError:
            # A malformed snapshot that violates a dataclass invariant is treated as missing data.
            log.info(
                "No plan for device %s: the input snapshot was invalid",
                self._device_id,
                exc_info=True,
            )
            self._invalidate()
            return None

        if not self._active():
            self._invalidate()
            return None
        try:
            self._scheduler.set_plan(plan)
        except (TypeError, ValueError):
            # An already-expired plan (e.g. a zero-length horizon grid) is simply not stored.
            log.info(
                "No plan stored for device %s: the computed plan was not valid",
                self._device_id,
                exc_info=True,
            )
            self._invalidate()
            return None
        # One atomic snapshot generation: plan, data ages and forecast quality are published
        # together only on a successful recompute, so current_view() never mixes generations.
        self._last_age = ages
        self._forecast_quality = (
            (resolution.basis, resolution.confidence)
            if resolution is not None
            else None
        )
        return plan

    def _invalidate(self) -> None:
        """Drop the stored plan and its paired snapshot together on every hard no-plan recompute."""
        self._scheduler.clear()
        self._last_age = DataAge(None, None, None, None)
        self._forecast_quality = None

    def _with_expiry(
        self,
        plan: EnergyPlan,
        now: datetime,
        prices: SpotPriceSnapshot,
        solar: SolarForecast,
        consumption: ConsumptionForecast,
        battery: BatteryState,
    ) -> EnergyPlan:
        """Clamp ``valid_until`` to ``min(created_at+plan_ttl, now+horizon, earliest_input_expiry)``, preserving all other plan fields.

        The optimizer sets a horizon-length validity; the service tightens it so a plan can never be
        declared valid past the point any input would be considered stale.

        Persisted spot and solar forecasts are not refreshed hourly (admin/forecast_refresh.py loads
        day-ahead prices once published and the solar forecast once per local day), so their validity
        is governed by delivery/forecast COVERAGE — the end of their last interval — not by fetch age.
        ``spot_max_age`` / ``solar_max_age`` remain only as a secondary upper cap on top of that.
        """
        candidates = [now + self._plan_ttl, now + self._horizon]
        # Primary validity is delivery/forecast coverage (last interval end). The max-age term is a
        # secondary upper cap measured from ``now`` — it bounds how far ahead a plan may be declared
        # valid, but (unlike a fetched_at/created_at-relative cap) never clamps a still-covering
        # persisted snapshot into the past just because it was fetched hours ago.
        spot_coverage_expiry = prices.intervals[-1].end
        spot_expiry = min(
            spot_coverage_expiry, now + timedelta(seconds=self._spot_max_age)
        )
        if prices.valid_until:
            spot_expiry = min(spot_expiry, prices.valid_until)
        candidates.append(spot_expiry)
        solar_coverage_expiry = solar.intervals[-1].end
        candidates.append(
            min(solar_coverage_expiry, now + timedelta(seconds=self._solar_max_age))
        )
        candidates.append(
            consumption.created_at + timedelta(seconds=self._consumption_max_age)
        )
        candidates.append(
            battery.measured_at + timedelta(seconds=self._battery_max_age_seconds)
        )
        valid_until = min(candidates)
        if valid_until <= plan.created_at:
            # Guarded by _stale_or_missing (every input covers the horizon at build time), so this is defensive.
            raise ValueError("computed valid_until is not strictly after created_at")
        return replace(plan, valid_until=valid_until)

    def _stale_or_missing(
        self,
        now: datetime,
        start: datetime,
        end: datetime,
        prices: SpotPriceSnapshot | None,
        solar: SolarForecast | None,
        consumption: ConsumptionForecast | None,
        battery: BatteryState | None,
    ) -> list[str]:
        # Persisted spot and solar forecasts are not refreshed hourly: day-ahead prices are loaded
        # once published and the solar forecast once per local day (see admin/forecast_refresh.py).
        # Their freshness is therefore interval COVERAGE of the plan horizon, not fetched_at/created_at
        # age — a snapshot whose published delivery intervals still span [start, end] is current.
        missing: list[str] = []
        if prices is None:
            missing.append("prices")
        elif not _covers_plan_horizon(prices.intervals, start, end):
            missing.append("prices(stale)")
        if solar is None or not solar.intervals:
            missing.append("solar")
        elif not _covers_plan_horizon(solar.intervals, start, end):
            missing.append("solar(stale)")
        if consumption is None or not consumption.intervals:
            missing.append("consumption")
        elif (now - consumption.created_at).total_seconds() > self._consumption_max_age:
            missing.append("consumption(stale)")
        # The battery provider already enforces its own max age and returns None when stale/missing.
        if battery is None:
            missing.append("battery")
        return missing

    def _ages(
        self,
        now: datetime,
        prices: SpotPriceSnapshot | None,
        solar: SolarForecast | None,
        consumption: ConsumptionForecast | None,
        battery: BatteryState | None,
    ) -> DataAge:
        def age(moment: datetime | None) -> float | None:
            return None if moment is None else (now - moment).total_seconds()

        return DataAge(
            spot_seconds=age(prices.fetched_at if prices is not None else None),
            solar_seconds=age(solar.created_at if solar is not None else None),
            consumption_seconds=age(
                consumption.created_at if consumption is not None else None
            ),
            battery_seconds=age(battery.measured_at if battery is not None else None),
        )

    async def run(self) -> None:
        """The bounded recompute loop; never raises into its caller (design Part 3 error handling)."""
        if self._activity is not None:
            self._activity.add_listener(
                self._wake.set
            )  # a mode change re-evaluates at once
        try:
            while True:
                if not self._active():
                    # Off: drop any plan, then rest until a mode change wakes the loop.
                    self._invalidate()
                    self._wake.clear()
                    await self._wake.wait()
                    self._wake.clear()
                    continue
                try:
                    await self.recompute()
                except Exception:
                    log.exception(
                        "Unexpected planning recompute failure for device %s",
                        self._device_id,
                    )
                # A start race is re-checked quickly so the first plan does not wait a full interval.
                timeout = (
                    min(self._interval, _PENDING_RETRY_SECONDS)
                    if self._pending_reason is not None
                    else self._interval
                )
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                except TimeoutError:
                    pass
                self._wake.clear()
        finally:
            if self._activity is not None:
                self._activity.remove_listener(self._wake.set)
