#!/usr/bin/env python3
#
# tests/test_optimizer_models.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Invariant coverage for the pure energy-optimizer contracts."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.energy.models import TARGET_SOC_MAX_PERCENT as EXECUTOR_TARGET_SOC_MAX_PERCENT
from app.energy.models import TARGET_SOC_MIN_PERCENT as EXECUTOR_TARGET_SOC_MIN_PERCENT
from app.optimizer.models import (
    REASON_MAX_LENGTH,
    TARGET_SOC_MAX_PERCENT,
    TARGET_SOC_MIN_PERCENT,
    BatteryState,
    ConsumptionForecast,
    ConsumptionForecastInterval,
    EnergyAction,
    EnergyPlan,
    EnergyPlanSlot,
    PlanningInput,
    SolarForecast,
    SolarForecastInterval,
)
from app.spotmarket.models import SpotMarketProvider, SpotPriceInterval, SpotPriceSnapshot

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _battery(**overrides) -> BatteryState:
    values = {
        "soc_percent": 50.0,
        "usable_capacity_wh": 9600.0,
        "max_charge_power_w": 3000.0,
        "max_discharge_power_w": 3000.0,
        "measured_at": NOW,
    }
    values.update(overrides)
    return BatteryState(**values)


def _prices() -> SpotPriceSnapshot:
    return SpotPriceSnapshot(
        provider=SpotMarketProvider.ENERGY_CHARTS,
        bidding_zone="DE-LU",
        currency="EUR",
        fetched_at=NOW,
        intervals=(SpotPriceInterval(NOW, NOW + timedelta(minutes=15), Decimal("0.2")),),
    )


def _solar(**overrides) -> SolarForecast:
    values = {
        "created_at": NOW,
        "intervals": (SolarForecastInterval(NOW, NOW + timedelta(hours=1), 500.0),),
    }
    values.update(overrides)
    return SolarForecast(**values)


def _consumption(**overrides) -> ConsumptionForecast:
    values = {
        "created_at": NOW,
        "intervals": (ConsumptionForecastInterval(NOW, NOW + timedelta(hours=1), 600.0),),
    }
    values.update(overrides)
    return ConsumptionForecast(**values)


def _slot(**overrides) -> EnergyPlanSlot:
    values = {
        "start": NOW,
        "end": NOW + timedelta(hours=1),
        "action": EnergyAction.CHARGE,
        "target_soc_percent": 80.0,
        "max_power_w": 1500.0,
        "reason": "low spot price",
    }
    values.update(overrides)
    return EnergyPlanSlot(**values)


def test_valid_models_support_variable_interval_lengths_and_incomplete_horizons() -> None:
    solar = SolarForecast(
        created_at=NOW,
        intervals=(
            SolarForecastInterval(NOW, NOW + timedelta(minutes=7), 50.0),
            SolarForecastInterval(NOW + timedelta(minutes=21), NOW + timedelta(hours=2), 700.0),
        ),
    )
    planning_input = PlanningInput(
        now=NOW,
        battery=_battery(),
        prices=_prices(),
        solar_forecast=solar,
        consumption_forecast=_consumption(),
        horizon=timedelta(days=1),
    )

    assert planning_input.solar_forecast is solar
    assert planning_input.horizon == timedelta(days=1)


@pytest.mark.parametrize("field", ["battery", "prices", "solar_forecast", "consumption_forecast"])
def test_planning_input_rejects_wrong_nested_type(field: str) -> None:
    values = {
        "now": NOW,
        "battery": _battery(),
        "prices": _prices(),
        "solar_forecast": _solar(),
        "consumption_forecast": _consumption(),
        "horizon": timedelta(hours=1),
    }
    values[field] = "invalid"
    with pytest.raises(ValueError, match=field):
        PlanningInput(**values)


@pytest.mark.parametrize("forecast_type", [SolarForecast, ConsumptionForecast])
def test_forecasts_reject_invalid_single_interval(forecast_type) -> None:
    with pytest.raises(ValueError, match="intervals"):
        forecast_type(NOW, ("invalid",))


def test_energy_plan_rejects_invalid_single_slot() -> None:
    with pytest.raises(ValueError, match="slots"):
        EnergyPlan(NOW, NOW + timedelta(hours=1), ("invalid",))


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (lambda: _battery(usable_capacity_wh=True), "usable_capacity_wh"),
        (lambda: SolarForecastInterval(NOW, NOW + timedelta(hours=1), True), "energy_wh"),
        (lambda: _slot(max_power_w=True), "max_power_w"),
        (lambda: _slot(target_soc_percent=True), "target_soc_percent"),
    ],
)
def test_numeric_fields_reject_booleans(factory, field: str) -> None:
    with pytest.raises(ValueError, match=field):
        factory()


@pytest.mark.parametrize("soc", [float("nan"), float("inf"), -0.1, 100.1])
def test_battery_state_rejects_invalid_measured_soc(soc: float) -> None:
    with pytest.raises(ValueError):
        _battery(soc_percent=soc)


@pytest.mark.parametrize("field", ["usable_capacity_wh", "max_charge_power_w", "max_discharge_power_w"])
@pytest.mark.parametrize("value", [float("nan"), 0.0, -1.0])
def test_battery_state_rejects_invalid_capacity_and_power(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        _battery(**{field: value})


@pytest.mark.parametrize(
    "interval_type",
    [SolarForecastInterval, ConsumptionForecastInterval],
)
@pytest.mark.parametrize("energy_wh", [float("nan"), -0.1])
def test_forecast_intervals_reject_invalid_energy(interval_type, energy_wh: float) -> None:
    with pytest.raises(ValueError):
        interval_type(NOW, NOW + timedelta(minutes=15), energy_wh)


@pytest.mark.parametrize("interval_type", [SolarForecastInterval, ConsumptionForecastInterval])
def test_forecast_intervals_reject_naive_or_inverted_times(interval_type) -> None:
    with pytest.raises(ValueError):
        interval_type(NOW.replace(tzinfo=None), NOW + timedelta(minutes=15), 1.0)
    with pytest.raises(ValueError):
        interval_type(NOW, NOW, 1.0)


@pytest.mark.parametrize(
    ("forecast_type", "interval_type"),
    [(SolarForecast, SolarForecastInterval), (ConsumptionForecast, ConsumptionForecastInterval)],
)
def test_forecasts_reject_overlapping_or_unordered_intervals(forecast_type, interval_type) -> None:
    intervals = (
        interval_type(NOW + timedelta(hours=1), NOW + timedelta(hours=2), 1.0),
        interval_type(NOW, NOW + timedelta(hours=1, minutes=30), 1.0),
    )
    with pytest.raises(ValueError):
        forecast_type(NOW, intervals)


@pytest.mark.parametrize("horizon", [timedelta(), timedelta(seconds=-1)])
def test_planning_input_requires_positive_horizon(horizon: timedelta) -> None:
    with pytest.raises(ValueError):
        PlanningInput(NOW, _battery(), _prices(), _solar(), _consumption(), horizon)


def test_planning_input_requires_an_aware_current_time() -> None:
    with pytest.raises(ValueError):
        PlanningInput(NOW.replace(tzinfo=None), _battery(), _prices(), _solar(), _consumption(), timedelta(hours=1))


@pytest.mark.parametrize("action", [EnergyAction.CHARGE, EnergyAction.DISCHARGE])
def test_charge_and_discharge_require_a_target(action: EnergyAction) -> None:
    with pytest.raises(ValueError):
        _slot(action=action, target_soc_percent=None)


@pytest.mark.parametrize("action", [EnergyAction.IDLE, EnergyAction.HOLD, EnergyAction.AUTO])
def test_non_target_actions_forbid_a_target(action: EnergyAction) -> None:
    with pytest.raises(ValueError):
        _slot(action=action, target_soc_percent=80.0)


@pytest.mark.parametrize("action", [EnergyAction.IDLE, EnergyAction.HOLD, EnergyAction.AUTO])
def test_non_target_actions_forbid_power_limit(action: EnergyAction) -> None:
    with pytest.raises(ValueError, match="max_power_w"):
        _slot(action=action, target_soc_percent=None, max_power_w=1500.0)


@pytest.mark.parametrize("target", [3.0, 99.0, float("nan")])
def test_plan_slot_rejects_invalid_target_soc(target: float) -> None:
    with pytest.raises(ValueError):
        _slot(target_soc_percent=target)


@pytest.mark.parametrize("target", [TARGET_SOC_MIN_PERCENT, 50.0, TARGET_SOC_MAX_PERCENT])
def test_plan_slot_accepts_operational_target_soc_bounds(target: float) -> None:
    assert _slot(target_soc_percent=target).target_soc_percent == target


def test_optimizer_target_bounds_match_executor_contract() -> None:
    assert TARGET_SOC_MIN_PERCENT == EXECUTOR_TARGET_SOC_MIN_PERCENT
    assert TARGET_SOC_MAX_PERCENT == EXECUTOR_TARGET_SOC_MAX_PERCENT


@pytest.mark.parametrize("reason", [None, "", "   ", "x" * (REASON_MAX_LENGTH + 1), "not\nprintable", "umlaut ä"])
def test_plan_slot_rejects_invalid_reasons(reason: str | None) -> None:
    with pytest.raises(ValueError):
        _slot(reason=reason)  # type: ignore[arg-type]


def test_plan_slot_keeps_a_valid_reason_unchanged() -> None:
    assert _slot(reason="  low price  ").reason == "  low price  "


@pytest.mark.parametrize("power", [float("nan"), 0.0, -1.0])
def test_plan_slot_rejects_invalid_optional_power(power: float) -> None:
    with pytest.raises(ValueError):
        _slot(max_power_w=power)


def test_energy_action_members_are_closed_and_exact() -> None:
    assert [action.value for action in EnergyAction] == ["idle", "charge", "discharge", "hold", "auto"]


def test_energy_plan_allows_empty_slots_but_rejects_invalid_time_or_overlap() -> None:
    assert EnergyPlan(NOW, NOW + timedelta(hours=1), ()).slots == ()
    with pytest.raises(ValueError):
        EnergyPlan(NOW, NOW, ())
    with pytest.raises(ValueError):
        EnergyPlan(NOW, NOW + timedelta(hours=3), (_slot(), _slot(start=NOW + timedelta(minutes=30))))
