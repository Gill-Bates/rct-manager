#!/usr/bin/env python3
#
# tests/test_optimizer_service.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Tests for the planning service: recompute loop, data age, staleness and plan expiry."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.energy.activity import EnergyActivity
from app.energy.models import EnergyMode
from app.optimizer.adapters import FORECAST_BASIS
from app.optimizer.consumption import BaselineConsumptionForecastProvider
from app.optimizer.models import (
    BatteryState,
    EnergyAction,
    PlanningInput,
    SolarForecast,
    SolarForecastInterval,
)
from app.optimizer.planner import DynamicChargeOptimizer, PlanParameters, planning_start
from app.optimizer.scheduler import InMemoryPlanScheduler
from app.optimizer.service import PlanningService
from app.spotmarket.models import (
    SpotMarketProvider,
    SpotPriceInterval,
    SpotPriceSnapshot,
)
from tests.conftest import ManualClock

DEVICE = "main"
HORIZON = timedelta(hours=1)


def _live_clock() -> ManualClock:
    """A manual clock anchored at the real wall clock.

    The scheduler now shares this injected clock (see ``_service``), so plan expiry is judged on the
    same deterministic time axis the service plans with. Anchoring near real time keeps the fixtures
    close to production instants; advancing the clock is what expires a plan, not the wall clock.
    """
    clock = ManualClock()
    clock._wall = datetime.now(UTC)
    return clock


class _PricesSource:
    def __init__(self, snapshot: SpotPriceSnapshot | None) -> None:
        self.snapshot_value = snapshot

    def snapshot(self) -> SpotPriceSnapshot | None:
        return self.snapshot_value


class _SolarSource:
    def __init__(self, forecast: SolarForecast | None) -> None:
        self.forecast_value = forecast

    def forecast(self, now: datetime, horizon: timedelta) -> SolarForecast:
        if self.forecast_value is None:
            raise AssertionError("solar source has no forecast")
        return self.forecast_value


class _ConsumptionSource:
    def __init__(self, forecast) -> None:
        self.forecast_value = forecast

    def forecast(self, now: datetime, horizon: timedelta):
        return self.forecast_value


class _BatterySource:
    def __init__(self, state: BatteryState | None) -> None:
        self.state = state

    def current(self, device_id: str) -> BatteryState | None:
        return self.state


def _prices(now: datetime, *, fetched_at: datetime | None = None) -> SpotPriceSnapshot:
    start = planning_start(now)
    intervals = tuple(
        SpotPriceInterval(
            start + timedelta(minutes=15 * index),
            start + timedelta(minutes=15 * (index + 1)),
            Decimal("0.10") + Decimal("0.01") * index,
        )
        for index in range(4)
    )
    return SpotPriceSnapshot(
        provider=SpotMarketProvider.ENERGY_CHARTS,
        bidding_zone="DE-LU",
        currency="EUR",
        fetched_at=fetched_at or now,
        intervals=intervals,
    )


def _solar(now: datetime, *, created_at: datetime | None = None) -> SolarForecast:
    start = planning_start(now)
    return SolarForecast(
        created_at=created_at or now,
        intervals=tuple(
            SolarForecastInterval(
                start + timedelta(minutes=15 * index),
                start + timedelta(minutes=15 * (index + 1)),
                100.0,
            )
            for index in range(4)
        ),
    )


def _battery(now: datetime) -> BatteryState:
    return BatteryState(
        soc_percent=50.0,
        usable_capacity_wh=10_000.0,
        max_charge_power_w=3000.0,
        max_discharge_power_w=3000.0,
        measured_at=now,
    )


def _service(
    clock: ManualClock, prices, solar, battery, **overrides
) -> PlanningService:
    consumption = BaselineConsumptionForecastProvider(400.0)
    optimizer = DynamicChargeOptimizer(PlanParameters())
    values = {
        "device_id": DEVICE,
        "clock": clock,
        "optimizer": optimizer,
        # One shared time source: the scheduler judges plan expiry against the same injected clock
        # the service plans with, so no wall-clock leakage can expire/accept a plan out of step.
        "scheduler": InMemoryPlanScheduler(clock),
        "prices": prices,
        "solar": solar,
        "consumption": consumption,
        "battery": battery,
        "horizon": HORIZON,
        "plan_ttl": timedelta(minutes=30),
        "recompute_interval_seconds": 300.0,
        "spot_max_age_seconds": 3600.0,
        "solar_max_age_seconds": 3600.0,
        "consumption_max_age_seconds": 3600.0,
        "battery_max_age_seconds": 300.0,
    }
    values.update(overrides)
    return PlanningService(**values)


@pytest.mark.parametrize("missing_source", ["solar", "consumption"])
async def test_forecast_gap_does_not_become_zero_energy(missing_source: str) -> None:
    clock = _live_clock()
    now = clock.now()
    solar = _solar(now)
    final_slot_end = planning_start(now) + HORIZON
    consumption = BaselineConsumptionForecastProvider(400.0).forecast(
        now, final_slot_end - now
    )
    if missing_source == "solar":
        solar = replace(solar, intervals=solar.intervals[:1] + solar.intervals[2:])
    else:
        consumption = replace(
            consumption, intervals=consumption.intervals[:1] + consumption.intervals[2:]
        )
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(solar),
        _BatterySource(_battery(now)),
        consumption=_ConsumptionSource(consumption),
    )

    assert await service.recompute() is None
    assert service.current_view() is None


async def test_missing_last_full_slot_at_non_boundary_time_yields_no_plan() -> None:
    clock = _live_clock()
    now = clock.now()
    assert now != planning_start(now)
    solar = _solar(now)
    solar = replace(solar, intervals=solar.intervals[:-1])
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(solar),
        _BatterySource(_battery(now)),
    )

    assert await service.recompute() is None
    assert service.current_view() is None


async def test_pv_surplus_is_not_planned_as_grid_charge() -> None:
    clock = _live_clock()
    now = clock.now()
    base_solar = _solar(now)
    solar = replace(
        base_solar,
        intervals=tuple(
            replace(slot, energy_wh=400.0) for slot in base_solar.intervals
        ),
    )
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(solar),
        _BatterySource(_battery(now)),
    )

    plan = await service.recompute()

    assert plan is not None
    assert all(
        slot.action
        not in (EnergyAction.CHARGE, EnergyAction.DISCHARGE, EnergyAction.HOLD)
        for slot in plan.slots
    )


async def test_recompute_builds_and_stores_a_plan_with_data_age() -> None:
    clock = _live_clock()
    now = clock.now()
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        _BatterySource(_battery(now)),
    )

    plan = await service.recompute()

    assert plan is not None
    view = service.current_view()
    assert view is not None
    assert view.plan is plan
    assert view.data_age.spot_seconds == 0.0
    assert view.data_age.battery_seconds == 0.0
    # The service tightens validity to min(created+ttl, now+horizon, earliest input expiry). Here
    # the battery max age (300 s) is the tightest term: measured_at==now, so the battery expires at
    # now+300 s, before the 30-min ttl, the 1-h horizon and the 1-h forecast ages.
    assert plan.valid_until == now + timedelta(seconds=300)


async def test_with_expiry_preserves_all_plan_metadata() -> None:
    # Clamping valid_until must not reset the optimizer's plan metadata (policy, reserve_*, target_*,
    # power, energy, assumptions, limiting_reasons, basis) back to defaults; it is a copy-with-one-
    # field-changed via dataclasses.replace.
    clock = _live_clock()
    now = clock.now()
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        _BatterySource(_battery(now)),
    )
    optimizer = DynamicChargeOptimizer(PlanParameters())
    final_slot_end = planning_start(now) + HORIZON
    consumption = BaselineConsumptionForecastProvider(400.0).forecast(
        now, final_slot_end - now
    )
    planning_input = PlanningInput(
        now=now,
        battery=_battery(now),
        prices=_prices(now),
        solar_forecast=_solar(now),
        consumption_forecast=consumption,
        horizon=HORIZON,
        forecast_basis=FORECAST_BASIS,
    )
    original = optimizer.plan(planning_input)

    clamped = service._with_expiry(
        original, now, _prices(now), _solar(now), consumption, _battery(now)
    )

    # Only valid_until may differ; every other field is carried over unchanged.
    assert clamped == replace(original, valid_until=clamped.valid_until)


async def test_spot_price_age_limits_plan_even_when_snapshot_valid_until_is_later() -> (
    None
):
    clock = _live_clock()
    now = clock.now()
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        _BatterySource(_battery(now)),
        spot_max_age_seconds=120.0,
        battery_max_age_seconds=3600.0,
    )

    plan = await service.recompute()
    assert plan is not None
    assert plan.valid_until == now + timedelta(seconds=120)


async def test_stale_or_missing_required_input_yields_no_plan() -> None:
    clock = _live_clock()
    now = clock.now()

    # Missing battery -> no plan.
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        _BatterySource(None),
    )
    assert await service.recompute() is None
    assert service.current_view() is None

    # Freshness is now interval coverage, not fetched_at/created_at age: a snapshot fetched 2 h ago
    # whose 4x15-min intervals still cover the 1-h horizon builds a plan under the DEFAULT 3600 s
    # config, and its valid_until tracks delivery coverage (the last interval end) rather than being
    # clamped into the past by fetched_at + spot_max_age.
    aged_prices = _prices(now, fetched_at=now - timedelta(hours=2))
    service = _service(
        clock,
        _PricesSource(aged_prices),
        _SolarSource(_solar(now)),
        _BatterySource(_battery(now)),
    )
    plan = await service.recompute()
    assert plan is not None
    # The old fetched_at no longer clamps validity into the past: spot/solar expiry is governed by
    # delivery/forecast coverage capped at now+max_age, so valid_until is strictly in the future.
    assert plan.valid_until > now

    # Prices whose intervals do not span the horizon -> no plan.
    short_prices = replace(_prices(now), intervals=_prices(now).intervals[:1])
    service = _service(
        clock,
        _PricesSource(short_prices),
        _SolarSource(_solar(now)),
        _BatterySource(_battery(now)),
    )
    assert await service.recompute() is None

    # Mirror for solar: an aged but horizon-covering forecast (daily-refresh contract) builds a plan,
    # and its created_at age likewise does not clamp valid_until into the past.
    aged_solar = _solar(now, created_at=now - timedelta(hours=2))
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(aged_solar),
        _BatterySource(_battery(now)),
    )
    plan = await service.recompute()
    assert plan is not None
    assert plan.valid_until > now

    # Solar whose intervals do not span the horizon -> no plan.
    short_solar = replace(_solar(now), intervals=_solar(now).intervals[:1])
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(short_solar),
        _BatterySource(_battery(now)),
    )
    assert await service.recompute() is None


async def test_absent_prices_source_yields_no_plan() -> None:
    clock = _live_clock()
    now = clock.now()
    service = _service(
        clock,
        _PricesSource(None),
        _SolarSource(_solar(now)),
        _BatterySource(_battery(now)),
    )
    assert await service.recompute() is None
    # Data age for the absent input is reported as None.
    view_age = service._last_age
    assert view_age.spot_seconds is None


async def test_successful_plan_then_stale_recompute_leaves_no_plan() -> None:
    # Item 1: a successful plan followed by a recompute that finds a required input missing must
    # leave NO plan available afterward: both current_view() and active_slot() report nothing.
    clock = _live_clock()
    now = clock.now()
    battery = _BatterySource(_battery(now))
    # Keep the battery age cap wide so plan validity spans the first slot and active_slot() can hit.
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        battery,
        battery_max_age_seconds=3600.0,
    )

    plan = await service.recompute()
    assert plan is not None
    assert service.current_view() is not None
    covered_instant = plan.slots[0].start
    assert (
        plan.valid_until > covered_instant
    )  # the first slot is within the plan's validity
    assert service.scheduler.active_slot(covered_instant) is plan.slots[0]

    # The next recompute finds the battery missing (stale/missing input) -> hard no-plan branch.
    battery.state = None
    assert await service.recompute() is None
    assert service.current_view() is None
    assert service.scheduler.current_plan() is None
    # active_slot() yields nothing for any instant the old plan used to cover.
    assert service.scheduler.active_slot(covered_instant) is None


async def test_failed_recompute_does_not_partially_overwrite_an_old_snapshot() -> None:
    # Item 1: the data age / forecast-quality of a FAILED recompute must never attach to a surviving
    # old plan. A hard no-plan branch clears plan, data age and forecast quality together, so there
    # is no mixed generation where an old plan carries a newer failed cycle's ages.
    clock = _live_clock()
    now = clock.now()
    prices = _PricesSource(_prices(now))
    service = _service(
        clock, prices, _SolarSource(_solar(now)), _BatterySource(_battery(now))
    )

    plan = await service.recompute()
    assert plan is not None
    first_view = service.current_view()
    assert first_view is not None
    assert first_view.data_age.spot_seconds is not None

    # Next cycle: prices disappear. The old plan must not survive annotated with the new ages.
    prices.snapshot_value = None
    assert await service.recompute() is None
    assert service.current_view() is None
    # The snapshot was cleared atomically, not partially overwritten onto the old plan.
    assert service._last_age == service._last_age.__class__(None, None, None, None)
    assert service._forecast_quality is None


async def test_expired_plan_is_reported_absent() -> None:
    clock = _live_clock()
    now = clock.now()
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        _BatterySource(_battery(now)),
    )

    plan = await service.recompute()
    assert plan is not None
    assert service.current_view() is not None

    # current_view reads through InMemoryPlanScheduler.current_plan(), which discards an expired
    # plan against the injected clock the scheduler shares with the service. Advance that one clock
    # past the plan's validity and the plan is reported absent (no wall-clock leakage).
    clock.advance((plan.valid_until - clock.now()).total_seconds() + 1)
    assert service.current_view() is None


async def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def test_loop_picks_up_a_changed_input_within_one_interval() -> None:
    clock = _live_clock()
    now = clock.now()
    prices = _PricesSource(_prices(now))
    solar = _SolarSource(_solar(now))
    battery = _BatterySource(_battery(now))
    # A long interval proves the pick-up comes from the private wake, not from a timer tick.
    service = _service(clock, prices, solar, battery, recompute_interval_seconds=3600.0)

    task = asyncio.create_task(service.run())
    try:
        await _wait_for(lambda: service.current_view() is not None)

        # Drop a required input, then wake the loop via its OWN private event (never a shared one).
        battery.state = None
        service.request_recompute()
        # Within one interval (here: immediately, via the wake) the loop re-reads inputs and sees
        # the dropped battery -> the recorded data age for that input becomes None.
        await _wait_for(lambda: service._last_age.battery_seconds is None)

        # The no-plan path invalidates the stored plan at once: a changed input is never charged on
        # stale data and the old plan is not left to linger until its own valid_until.
        assert service.scheduler.current_plan() is None
        assert service.current_view() is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class _CountingBattery(_BatterySource):
    def __init__(self, state: BatteryState | None) -> None:
        super().__init__(state)
        self.calls = 0

    def current(self, device_id: str) -> BatteryState | None:
        self.calls += 1
        return super().current(device_id)


async def test_device_not_connected_yet_is_pending_and_replans_once_ready(
    caplog,
) -> None:
    clock = _live_clock()
    now = clock.now()
    ready = {"value": False}
    battery = _CountingBattery(_battery(now))
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        battery,
        device_ready=lambda _device_id: ready["value"],
    )

    with caplog.at_level("INFO"):
        assert await service.recompute() is None
    # Start race: no battery read, no "missing input" verdict, no stored plan.
    assert battery.calls == 0
    assert "pending" in caplog.text and "No plan" not in caplog.text
    assert service.current_view() is None

    ready["value"] = True
    assert await service.recompute() is not None
    assert battery.calls == 1


async def test_pending_loop_retries_faster_than_the_recompute_interval() -> None:
    clock = _live_clock()
    now = clock.now()
    ready = {"value": False}
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        _BatterySource(_battery(now)),
        device_ready=lambda _device_id: ready["value"],
        recompute_interval_seconds=3600.0,
    )
    waits: list[float] = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(awaitable, timeout):
        waits.append(timeout)
        service.request_recompute()  # end the wait at once; only the requested timeout matters
        return await real_wait_for(awaitable, timeout)

    task = None
    with patch("app.optimizer.service.asyncio.wait_for", recording_wait_for):
        task = asyncio.create_task(service.run())
        try:
            await _wait_for(lambda: len(waits) >= 1)
            assert waits[0] == 5.0
            ready["value"] = True
            await _wait_for(lambda: service.current_view() is not None)
            await _wait_for(lambda: waits[-1] == 3600.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


def test_missing_capacity_after_readiness_still_reports_not_configured(caplog) -> None:
    from app.dispatch.battery_facts import NoVerifiedCapacity
    from app.dispatch.models import DeviceLimits
    from app.optimizer.service import ReadingsBatteryStateProvider

    class _Limits:
        def device_limits(self, device_id: str):
            return DeviceLimits(max_charge_power_w=3000.0, max_discharge_power_w=3000.0)

    provider = ReadingsBatteryStateProvider(
        readings=None,
        dispatch=_Limits(),
        clock=_live_clock(),
        usable_capacity_wh=None,
        max_age_seconds=300.0,
        capacity_source=NoVerifiedCapacity(),
    )
    with caplog.at_level("INFO"):
        assert provider.current(DEVICE) is None
    assert (
        "usable battery capacity unavailable: no expert override and no verified automatic source"
        in caplog.text
    )


# --- runtime consumption policy equals the Dry-Run policy (baseline / fallback, never a blocker) ---


def _stored_source(monkeypatch, history, baseline: float = 620.0):
    from app.optimizer.adapters import StoredConsumptionForecastProvider
    from app.optimizer.runtime_sources import StoredConsumptionForecastSource
    from app.weather.models import WeatherConfig

    monkeypatch.setattr(
        StoredConsumptionForecastProvider, "_load_history", lambda self, now: history
    )
    return StoredConsumptionForecastSource(
        object(), DEVICE, lambda: WeatherConfig(), baseline
    )


async def test_runtime_planner_without_history_plans_with_low_confidence_baseline(
    monkeypatch,
) -> None:
    clock = _live_clock()
    now = clock.now()
    source = _stored_source(monkeypatch, [])
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        _BatterySource(_battery(now)),
        consumption=source,
    )

    plan = await service.recompute()

    assert plan is not None
    view = service.current_view()
    assert (view.forecast_basis, view.forecast_confidence) == (
        "bootstrap_baseline",
        "low",
    )
    # The contract values are enums, independent of the free-form assumptions text.
    from app.optimizer.models import ForecastBasis, ForecastConfidence

    assert view.forecast_basis is ForecastBasis.BOOTSTRAP_BASELINE
    assert view.forecast_confidence is ForecastConfidence.LOW
    assert "620 W" in plan.assumptions.forecast_basis
    assert plan.assumptions.forecast_basis not in {m.value for m in ForecastBasis}


async def test_runtime_planner_with_thin_history_uses_fallback_with_low_confidence(
    monkeypatch,
) -> None:
    from app.optimizer.consumption import HistoryBucket

    clock = _live_clock()
    now = clock.now()
    start = now - timedelta(days=1)
    history = [
        HistoryBucket(
            start + timedelta(minutes=15 * i),
            start + timedelta(minutes=15 * (i + 1)),
            100.0,
            10,
            900.0,
        )
        for i in range(3)
    ]
    source = _stored_source(monkeypatch, history)
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        _BatterySource(_battery(now)),
        consumption=source,
    )

    assert await service.recompute() is not None
    view = service.current_view()
    assert (view.forecast_basis, view.forecast_confidence) == (
        "history_fallback",
        "low",
    )


async def test_runtime_planner_without_battery_still_yields_no_plan(
    monkeypatch,
) -> None:
    clock = _live_clock()
    now = clock.now()
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        _BatterySource(None),
        consumption=_stored_source(monkeypatch, []),
    )

    assert await service.recompute() is None
    assert service.current_view() is None


def _runtime_with_status(state, last_success_at=None, error=None):
    from types import SimpleNamespace

    def device_status(_device_id):
        if error is not None:
            raise error
        return SimpleNamespace(state=state, last_success_at=last_success_at)

    return SimpleNamespace(gateway=SimpleNamespace(device_status=device_status))


def test_device_started_maps_gateway_state_to_readiness() -> None:
    from app.errors import UnknownDevice
    from app.gateway.base import DeviceState
    from app.optimizer.runtime_sources import _device_started

    now = datetime.now(UTC)
    assert _device_started(_runtime_with_status(DeviceState.STARTING), "main") is False
    # A completed transaction proves the link before the first heartbeat tick.
    assert (
        _device_started(_runtime_with_status(DeviceState.STARTING, now), "main") is True
    )
    for state in (
        DeviceState.OK,
        DeviceState.DEGRADED,
        DeviceState.UNREACHABLE,
        DeviceState.MAINTENANCE,
    ):
        assert _device_started(_runtime_with_status(state), "main") is True
    assert (
        _device_started(
            _runtime_with_status(None, error=UnknownDevice(device_id="x")), "main"
        )
        is True
    )


async def test_raising_readiness_check_does_not_leave_pending_set() -> None:
    clock = _live_clock()
    now = clock.now()
    state = {"raise": False}

    def ready(_device_id: str) -> bool:
        if state["raise"]:
            raise RuntimeError("boom")
        return False

    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        _BatterySource(_battery(now)),
        device_ready=ready,
    )
    assert await service.recompute() is None
    assert service._pending is True
    state["raise"] = True
    with pytest.raises(RuntimeError):
        await service.recompute()
    assert service._pending is False


class _Modes:
    """A mutable mode map behind a real EnergyActivity; a missing device is Off, as in production."""

    def __init__(self, **modes: EnergyMode) -> None:
        self.modes = dict(modes)
        self.activity = EnergyActivity(
            lambda device_id: self.modes.get(device_id, EnergyMode.OFF),
            lambda: ("main", "second"),
        )

    def set(self, device_id: str, mode: EnergyMode) -> None:
        self.modes[device_id] = mode
        self.activity.notify()


class _SpyOptimizer(DynamicChargeOptimizer):
    def __init__(self) -> None:
        super().__init__(PlanParameters())
        self.calls = 0

    def plan(self, planning_input):
        self.calls += 1
        return super().plan(planning_input)


def _gated_service(clock, modes: _Modes, device_id: str = DEVICE):
    now = clock.now()
    spy = _SpyOptimizer()
    service = _service(
        clock,
        _PricesSource(_prices(now)),
        _SolarSource(_solar(now)),
        _BatterySource(_battery(now)),
        device_id=device_id,
        optimizer=spy,
        activity=modes.activity,
        recompute_interval_seconds=3600.0,
    )
    return service, spy


async def _settle(cycles: int = 20) -> None:
    for _ in range(cycles):
        await asyncio.sleep(0)


async def test_planning_rests_without_a_stored_mode_and_follows_mode_changes() -> None:
    clock = _live_clock()
    modes = _Modes()  # fresh install: no stored mode, which is Off
    service, spy = _gated_service(clock, modes)
    task = asyncio.create_task(service.run())
    try:
        await _settle()
        service.request_recompute()
        assert await service.recompute() is None
        await _settle()
        assert spy.calls == 0 and service.current_view() is None

        modes.set(DEVICE, EnergyMode.MANUAL)  # Off -> Manual wakes the loop without a timeout
        await _wait_for(lambda: service.current_view() is not None)
        assert spy.calls >= 1
        assert service.scheduler.current_plan() is not None

        calls = spy.calls
        modes.set(DEVICE, EnergyMode.OFF)  # Manual -> Off invalidates the plan at once
        await _wait_for(lambda: service.current_view() is None)
        assert service.scheduler.current_plan() is None and service.scheduler.active_slot(clock.now()) is None
        service.request_recompute()
        await _settle()
        assert spy.calls == calls, "no optimizer call after the switch to Off"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_planning_is_per_device_while_one_other_inverter_is_active() -> None:
    clock = _live_clock()
    modes = _Modes(second=EnergyMode.EXTERNAL)  # main is Off, second is External
    off_service, off_spy = _gated_service(clock, modes, "main")
    on_service, on_spy = _gated_service(clock, modes, "second")
    tasks = [asyncio.create_task(off_service.run()), asyncio.create_task(on_service.run())]
    try:
        await _wait_for(lambda: on_service.current_view() is not None)
        await _settle()
        assert off_spy.calls == 0 and off_service.current_view() is None
        assert on_spy.calls >= 1
        assert modes.activity.any_energy_active()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
