#!/usr/bin/env python3
#
# tests/test_optimizer_greedy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Tests for the deterministic greedy energy optimizer."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.limits import MAX_POWER_W, TARGET_SOC_MAX_PERCENT, TARGET_SOC_MIN_PERCENT
from app.optimizer.greedy import GreedyEnergyOptimizer
from app.optimizer.models import (
    BatteryState,
    ConsumptionForecast,
    ConsumptionForecastInterval,
    EnergyAction,
    EnergyPlan,
    PlanningInput,
    SolarForecast,
    SolarForecastInterval,
)
from app.spotmarket.models import SpotMarketProvider, SpotPriceInterval, SpotPriceSnapshot

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
HORIZON = timedelta(hours=1)
_SLOTS = 4  # one hour of 15-minute grid slots


def _optimizer(**overrides) -> GreedyEnergyOptimizer:
    values = {"cheap_percentile": 25.0, "expensive_percentile": 75.0, "min_soc": 5.0, "max_soc": 95.0}
    values.update(overrides)
    return GreedyEnergyOptimizer(**values)


def _battery(**overrides) -> BatteryState:
    values = {
        "soc_percent": 50.0,
        "usable_capacity_wh": 10_000.0,
        "max_charge_power_w": 3000.0,
        "max_discharge_power_w": 3000.0,
        "measured_at": NOW,
    }
    values.update(overrides)
    return BatteryState(**values)


def _prices(per_slot: list[Decimal | None]) -> SpotPriceSnapshot:
    intervals = tuple(
        SpotPriceInterval(
            NOW + timedelta(minutes=15 * index),
            NOW + timedelta(minutes=15 * (index + 1)),
            price,
        )
        for index, price in enumerate(per_slot)
        if price is not None
    )
    return SpotPriceSnapshot(
        provider=SpotMarketProvider.ENERGY_CHARTS,
        bidding_zone="DE-LU",
        currency="EUR",
        fetched_at=NOW,
        intervals=intervals,
    )


def _solar(per_slot_wh: list[float]) -> SolarForecast:
    return SolarForecast(
        created_at=NOW,
        intervals=tuple(
            SolarForecastInterval(
                NOW + timedelta(minutes=15 * index), NOW + timedelta(minutes=15 * (index + 1)), wh
            )
            for index, wh in enumerate(per_slot_wh)
        ),
    )


def _consumption(per_slot_wh: list[float]) -> ConsumptionForecast:
    return ConsumptionForecast(
        created_at=NOW,
        intervals=tuple(
            ConsumptionForecastInterval(
                NOW + timedelta(minutes=15 * index), NOW + timedelta(minutes=15 * (index + 1)), wh
            )
            for index, wh in enumerate(per_slot_wh)
        ),
    )


def _planning_input(prices, solar, consumption, battery=None) -> PlanningInput:
    return PlanningInput(
        now=NOW,
        battery=battery or _battery(),
        prices=prices,
        solar_forecast=solar,
        consumption_forecast=consumption,
        horizon=HORIZON,
    )


def test_plan_returns_a_valid_slot_per_grid_cell() -> None:
    plan = _optimizer().plan(
        _planning_input(
            _prices([Decimal("0.20")] * _SLOTS),
            _solar([0.0] * _SLOTS),
            _consumption([150.0] * _SLOTS),
        )
    )
    assert isinstance(plan, EnergyPlan)
    assert len(plan.slots) == _SLOTS
    assert plan.created_at == NOW
    assert plan.valid_until == NOW + HORIZON


def test_cheap_window_charges_from_grid() -> None:
    # First slot is far below the 25th percentile; later slots are expensive.
    prices = _prices([Decimal("0.05"), Decimal("0.30"), Decimal("0.31"), Decimal("0.32")])
    plan = _optimizer().plan(
        _planning_input(prices, _solar([0.0] * _SLOTS), _consumption([100.0] * _SLOTS))
    )
    first = plan.slots[0]
    assert first.action is EnergyAction.CHARGE
    assert "charge from grid" in first.reason
    assert first.target_soc_percent == pytest.approx(95.0)
    assert first.max_power_w == pytest.approx(3000.0)


def test_expensive_window_discharges_to_load() -> None:
    # Last slot sits at/above the 75th percentile with battery energy to spend.
    prices = _prices([Decimal("0.10"), Decimal("0.11"), Decimal("0.12"), Decimal("0.40")])
    plan = _optimizer().plan(
        _planning_input(prices, _solar([0.0] * _SLOTS), _consumption([100.0] * _SLOTS))
    )
    last = plan.slots[-1]
    assert last.action is EnergyAction.DISCHARGE
    assert "discharge to load" in last.reason
    # The effective floor is max(7, min_soc) = 7.0 (the hard product reserve dominates min_soc=5).
    assert last.target_soc_percent == pytest.approx(7.0)
    assert last.max_power_w == pytest.approx(3000.0)


def test_pv_surplus_charges_before_any_grid_consideration() -> None:
    # Solar exceeds load in slot 0 even though its price is the most expensive -> PV-surplus charge.
    prices = _prices([Decimal("0.40"), Decimal("0.10"), Decimal("0.11"), Decimal("0.12")])
    solar = _solar([1000.0, 0.0, 0.0, 0.0])
    consumption = _consumption([100.0, 100.0, 100.0, 100.0])
    plan = _optimizer().plan(_planning_input(prices, solar, consumption))
    first = plan.slots[0]
    assert first.action is EnergyAction.CHARGE
    assert first.reason.startswith("PV surplus")
    assert "charge battery" in first.reason


def test_price_gap_yields_idle_and_names_the_gap() -> None:
    prices = _prices([Decimal("0.20"), None, Decimal("0.20"), Decimal("0.20")])
    plan = _optimizer().plan(
        _planning_input(prices, _solar([0.0] * _SLOTS), _consumption([100.0] * _SLOTS))
    )
    gap_slot = plan.slots[1]
    assert gap_slot.action is EnergyAction.IDLE
    assert gap_slot.target_soc_percent is None
    assert gap_slot.max_power_w is None
    assert "no price" in gap_slot.reason


def test_missing_solar_is_treated_as_zero_pv() -> None:
    # Solar covers only slot 0 (50 Wh < 100 Wh load, so no surplus even there); the uncovered slots
    # get 0 PV, never an invented negative residual. No slot is a PV-surplus charge.
    prices = _prices([Decimal("0.20")] * _SLOTS)
    solar = SolarForecast(
        created_at=NOW,
        intervals=(SolarForecastInterval(NOW, NOW + timedelta(minutes=15), 50.0),),
    )
    plan = _optimizer().plan(_planning_input(prices, solar, _consumption([100.0] * _SLOTS)))
    assert not any(slot.reason.startswith("PV surplus") for slot in plan.slots)


def test_soc_at_top_does_not_charge_in_a_cheap_window() -> None:
    prices = _prices([Decimal("0.05")] * _SLOTS)
    battery = _battery(soc_percent=95.0)
    plan = _optimizer().plan(
        _planning_input(prices, _solar([0.0] * _SLOTS), _consumption([100.0] * _SLOTS), battery)
    )
    # At the window top a cheap slot cannot charge; it falls through to HOLD (flat curve, no
    # genuine expensive window), never DISCHARGE.
    assert all(slot.action is EnergyAction.HOLD for slot in plan.slots)


def test_soc_at_floor_does_not_discharge_in_an_expensive_window() -> None:
    # The most expensive slot comes first, so no prior cheap slot can lift SoC above the floor. With
    # the battery pinned at the effective floor (7.0), the expensive first slot cannot discharge
    # below the reserve floor.
    prices = _prices([Decimal("0.40"), Decimal("0.12"), Decimal("0.11"), Decimal("0.10")])
    battery = _battery(soc_percent=7.0)
    plan = _optimizer().plan(
        _planning_input(prices, _solar([0.0] * _SLOTS), _consumption([100.0] * _SLOTS), battery)
    )
    assert plan.slots[0].action is not EnergyAction.DISCHARGE


def test_targets_stay_inside_the_effective_soc_window() -> None:
    prices = _prices([Decimal("0.05"), Decimal("0.20"), Decimal("0.20"), Decimal("0.40")])
    plan = _optimizer(min_soc=20.0, max_soc=80.0).plan(
        _planning_input(prices, _solar([0.0] * _SLOTS), _consumption([100.0] * _SLOTS))
    )
    for slot in plan.slots:
        if slot.target_soc_percent is not None:
            assert 20.0 <= slot.target_soc_percent <= 80.0
            assert TARGET_SOC_MIN_PERCENT <= slot.target_soc_percent <= TARGET_SOC_MAX_PERCENT


def test_flat_price_curve_is_cheap_charge_then_hold() -> None:
    # Low SoC + a tiny battery so the trajectory reaches the top within the horizon, then holds.
    prices = _prices([Decimal("0.15")] * _SLOTS)
    battery = _battery(soc_percent=90.0, usable_capacity_wh=1000.0)
    plan = _optimizer().plan(
        _planning_input(prices, _solar([0.0] * _SLOTS), _consumption([100.0] * _SLOTS), battery)
    )
    actions = [slot.action for slot in plan.slots]
    # Charges first, then holds; no slot ever discharges on a flat curve.
    assert EnergyAction.DISCHARGE not in actions
    assert actions[0] is EnergyAction.CHARGE
    assert actions[-1] is EnergyAction.HOLD
    # The charge prefix precedes the hold suffix (no interleaving).
    first_hold = actions.index(EnergyAction.HOLD)
    assert all(action is EnergyAction.CHARGE for action in actions[:first_hold])
    assert all(action is EnergyAction.HOLD for action in actions[first_hold:])


def test_no_charge_or_discharge_slot_has_a_none_power() -> None:
    prices = _prices([Decimal("0.05"), Decimal("0.20"), Decimal("0.21"), Decimal("0.40")])
    plan = _optimizer().plan(
        _planning_input(prices, _solar([200.0, 0.0, 0.0, 0.0]), _consumption([100.0] * _SLOTS))
    )
    for slot in plan.slots:
        if slot.action in {EnergyAction.CHARGE, EnergyAction.DISCHARGE}:
            assert slot.max_power_w is not None
            assert slot.max_power_w > 0


def test_hold_and_idle_slots_carry_no_target_or_power() -> None:
    prices = _prices([Decimal("0.20"), None, Decimal("0.20"), Decimal("0.20")])
    plan = _optimizer().plan(
        _planning_input(prices, _solar([0.0] * _SLOTS), _consumption([100.0] * _SLOTS))
    )
    for slot in plan.slots:
        if slot.action in {EnergyAction.HOLD, EnergyAction.IDLE}:
            assert slot.target_soc_percent is None
            assert slot.max_power_w is None


def test_power_never_exceeds_the_global_max_power() -> None:
    prices = _prices([Decimal("0.05")] * _SLOTS)
    battery = _battery(max_charge_power_w=MAX_POWER_W * 2, max_discharge_power_w=MAX_POWER_W * 2)
    plan = _optimizer().plan(
        _planning_input(prices, _solar([0.0] * _SLOTS), _consumption([100.0] * _SLOTS), battery)
    )
    for slot in plan.slots:
        if slot.max_power_w is not None:
            assert slot.max_power_w <= MAX_POWER_W


def test_never_emits_export_or_auto_as_a_planned_slot() -> None:
    prices = _prices([Decimal("0.05"), Decimal("0.20"), Decimal("0.21"), Decimal("0.40")])
    plan = _optimizer().plan(
        _planning_input(prices, _solar([500.0, 0.0, 0.0, 0.0]), _consumption([100.0] * _SLOTS))
    )
    for slot in plan.slots:
        assert slot.action in {EnergyAction.CHARGE, EnergyAction.DISCHARGE, EnergyAction.HOLD, EnergyAction.IDLE}


def test_constructor_rejects_crossed_percentiles() -> None:
    with pytest.raises(ValueError):
        GreedyEnergyOptimizer(cheap_percentile=75.0, expensive_percentile=25.0, min_soc=5.0, max_soc=95.0)


def test_reasons_are_bounded_printable_ascii() -> None:
    prices = _prices([Decimal("0.05"), Decimal("0.20"), None, Decimal("0.40")])
    plan = _optimizer().plan(
        _planning_input(prices, _solar([800.0, 0.0, 0.0, 0.0]), _consumption([100.0] * _SLOTS))
    )
    for slot in plan.slots:
        assert slot.reason
        assert len(slot.reason) <= 128
        assert slot.reason.isascii()
        assert "->" in slot.reason
