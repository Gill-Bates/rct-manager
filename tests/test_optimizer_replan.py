#!/usr/bin/env python3
#
# tests/test_optimizer_replan.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The pure replan trigger contract: significant changes fire, insignificant ones do not."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.optimizer.models import (
    BatteryState,
    ConsumptionForecast,
    ConsumptionForecastInterval,
    PlanningInput,
    SolarForecast,
    SolarForecastInterval,
)
from app.optimizer.planner import DynamicChargeOptimizer, PlanParameters
from app.optimizer.replan import (
    TRIGGER_DESCRIPTIONS,
    ReplanObservation,
    ReplanThresholds,
    ReplanTriggerCode,
    evaluate_replan,
    expected_soc_percent,
)
from app.spotmarket.models import SpotMarketProvider, SpotPriceInterval, SpotPriceSnapshot

NOW = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)
SLOT = timedelta(minutes=15)
SLOTS = 16


def _planning_input(static_w: float = 3000.0, live_w: float | None = None) -> PlanningInput:
    prices = SpotPriceSnapshot(
        provider=SpotMarketProvider.ENERGY_CHARTS, bidding_zone="DE-LU", currency="EUR", fetched_at=NOW,
        intervals=tuple(
            SpotPriceInterval(NOW + SLOT * i, NOW + SLOT * (i + 1), Decimal("0.20") if i else Decimal("0.05"))
            for i in range(SLOTS)
        ),
    )  # fmt: skip
    solar = SolarForecast(NOW, tuple(SolarForecastInterval(NOW + SLOT * i, NOW + SLOT * (i + 1), 0.0) for i in range(SLOTS)))
    consumption = ConsumptionForecast(
        NOW, tuple(ConsumptionForecastInterval(NOW + SLOT * i, NOW + SLOT * (i + 1), 500.0) for i in range(SLOTS))
    )
    battery = BatteryState(20.0, 10_000.0, static_w, 3000.0, NOW, live_charge_limit_w=live_w)
    return PlanningInput(NOW, battery, prices, solar, consumption, timedelta(hours=4))


def _plan(**kwargs):
    params = PlanParameters(target_soc_percent=60.0, target_time=NOW + timedelta(hours=3))
    planning_input = _planning_input(**kwargs)
    return DynamicChargeOptimizer(params).plan(planning_input), planning_input


def _observation(plan, planning_input, now=None, **overrides) -> ReplanObservation:
    prices, solar, consumption = DynamicChargeOptimizer.forecast_series(planning_input)
    base = {
        "now": now or NOW + SLOT, "soc_percent": expected_soc_percent(plan.basis, now or NOW + SLOT),
        "permitted_charge_power_w": plan.basis.permitted_charge_power_w, "prices": prices, "solar_wh": solar,
        "consumption_wh": consumption,
        "executed_charge_wh": {c.start: c.terminal_energy_wh for c in plan.basis.planned_charges},
    }  # fmt: skip
    return ReplanObservation(**{**base, **overrides})


def _codes(decision) -> set[ReplanTriggerCode]:
    return {trigger.code for trigger in decision.triggers}


def test_an_unchanged_situation_needs_no_replan() -> None:
    plan, planning_input = _plan()
    assert plan.basis.planned_charges
    decision = evaluate_replan(plan.basis, _observation(plan, planning_input))
    assert decision.replan_required is False and decision.triggers == ()


def test_soc_deviation_triggers_only_beyond_the_threshold() -> None:
    plan, planning_input = _plan()
    expected = expected_soc_percent(plan.basis, NOW + SLOT)
    small = _observation(plan, planning_input, soc_percent=expected + 3.0)
    large = _observation(plan, planning_input, soc_percent=expected - 8.0)
    assert evaluate_replan(plan.basis, small).replan_required is False
    assert _codes(evaluate_replan(plan.basis, large)) == {ReplanTriggerCode.SOC_DEVIATION}


def test_a_lower_verified_live_limit_than_planned_triggers_a_replan() -> None:
    plan, planning_input = _plan(static_w=3000.0)
    reduced = _observation(plan, planning_input, permitted_charge_power_w=1000.0)
    decision = evaluate_replan(plan.basis, reduced)
    assert ReplanTriggerCode.CHARGE_LIMIT_CHANGED in _codes(decision)
    assert "reduced" in decision.triggers[0].detail
    # The replanned input reflects the live limit as the new, lower permitted power.
    replanned, _ = _plan(static_w=3000.0, live_w=1000.0)
    assert replanned.power.permitted_charge_power_w == 1000.0
    assert evaluate_replan(replanned.basis, _observation(replanned, _planning_input(live_w=1000.0))).triggers == ()


def test_an_insignificant_limit_change_does_not_trigger() -> None:
    plan, planning_input = _plan()
    nearly_same = _observation(plan, planning_input, permitted_charge_power_w=plan.basis.permitted_charge_power_w - 20.0)
    assert evaluate_replan(plan.basis, nearly_same).replan_required is False


def test_forecast_changes_trigger_per_source_beyond_their_tolerance() -> None:
    plan, planning_input = _plan()
    prices, solar, consumption = DynamicChargeOptimizer.forecast_series(planning_input)

    def bump(series, delta, index=4):
        return tuple((start, (value or 0.0) + delta if i == index else value) for i, (start, value) in enumerate(series))

    cases = [
        ("prices", bump(prices, 0.005), bump(prices, 0.05), ReplanTriggerCode.PRICE_FORECAST_CHANGED),
        ("solar_wh", bump(solar, 100.0), bump(solar, 900.0), ReplanTriggerCode.SOLAR_FORECAST_CHANGED),
        ("consumption_wh", bump(consumption, 100.0), bump(consumption, 900.0), ReplanTriggerCode.CONSUMPTION_FORECAST_CHANGED),
    ]  # fmt: skip
    for field, small, large, code in cases:
        quiet = evaluate_replan(plan.basis, _observation(plan, planning_input, **{field: small}))
        loud = evaluate_replan(plan.basis, _observation(plan, planning_input, **{field: large}))
        assert quiet.replan_required is False, field
        assert _codes(loud) == {code}, field


def test_a_missed_charge_slot_triggers_once_its_time_has_passed() -> None:
    plan, planning_input = _plan()
    first = plan.basis.planned_charges[0]
    later = first.end + SLOT
    executed = {c.start: c.terminal_energy_wh for c in plan.basis.planned_charges}
    on_track = _observation(plan, planning_input, now=later, executed_charge_wh=executed)
    assert ReplanTriggerCode.MISSED_CHARGE_SLOT not in _codes(evaluate_replan(plan.basis, on_track))
    missed = _observation(plan, planning_input, now=later, executed_charge_wh={})
    assert ReplanTriggerCode.MISSED_CHARGE_SLOT in _codes(evaluate_replan(plan.basis, missed))
    # A slot that has not ended yet cannot have been missed.
    early = _observation(plan, planning_input, now=first.start, executed_charge_wh={})
    assert ReplanTriggerCode.MISSED_CHARGE_SLOT not in _codes(evaluate_replan(plan.basis, early))


def test_thresholds_are_configurable_and_every_trigger_is_described() -> None:
    plan, planning_input = _plan()
    expected = expected_soc_percent(plan.basis, NOW + SLOT)
    observation = _observation(plan, planning_input, soc_percent=expected + 3.0)
    strict = replace(ReplanThresholds(), soc_deviation_percent=2.0)
    assert evaluate_replan(plan.basis, observation, strict).replan_required is True
    assert set(TRIGGER_DESCRIPTIONS) == set(ReplanTriggerCode)


def test_expected_soc_interpolates_along_the_trajectory() -> None:
    plan, _ = _plan()
    (t0, s0), (t1, s1) = plan.basis.trajectory[0], plan.basis.trajectory[1]
    middle = t0 + (t1 - t0) / 2
    assert expected_soc_percent(plan.basis, middle) == pytest.approx((s0 + s1) / 2)
    assert expected_soc_percent(plan.basis, t0 - SLOT) == s0
    assert expected_soc_percent(plan.basis, plan.basis.trajectory[-1][0] + SLOT) == plan.basis.trajectory[-1][1]


def _drop_future_slots(series, keep: int):
    return tuple(series[:keep])


def test_a_lost_future_slot_triggers_even_when_the_shared_slots_are_unchanged() -> None:
    plan, planning_input = _plan()
    prices, solar, consumption = DynamicChargeOptimizer.forecast_series(planning_input)
    assert len(prices) > 10
    cases = [
        ("prices", prices, ReplanTriggerCode.PRICE_FORECAST_CHANGED),
        ("solar_wh", solar, ReplanTriggerCode.SOLAR_FORECAST_CHANGED),
        ("consumption_wh", consumption, ReplanTriggerCode.CONSUMPTION_FORECAST_CHANGED),
    ]
    for field, series, code in cases:
        shortened = _observation(plan, planning_input, **{field: _drop_future_slots(series, 10)})
        assert code in _codes(evaluate_replan(plan.basis, shortened)), field


def test_a_new_slot_inside_the_plan_span_triggers_but_one_beyond_it_does_not() -> None:
    plan, planning_input = _plan()
    prices, _, _ = DynamicChargeOptimizer.forecast_series(planning_input)
    gapped = tuple(item for i, item in enumerate(prices) if i != 5)
    plan_with_gap = replace(plan.basis, prices=gapped)
    assert ReplanTriggerCode.PRICE_FORECAST_CHANGED in _codes(
        evaluate_replan(plan_with_gap, _observation(plan, planning_input))
    )
    beyond = prices + ((prices[-1][0] + SLOT, 0.2),)
    assert ReplanTriggerCode.PRICE_FORECAST_CHANGED not in _codes(
        evaluate_replan(plan.basis, _observation(plan, planning_input, prices=beyond))
    )


def test_slots_in_the_past_never_trigger() -> None:
    plan, planning_input = _plan()
    prices, solar, consumption = DynamicChargeOptimizer.forecast_series(planning_input)
    later = NOW + SLOT * 4
    kwargs = {
        "prices": prices[4:], "solar_wh": solar[4:], "consumption_wh": consumption[4:],
        "executed_charge_wh": {c.start: c.terminal_energy_wh for c in plan.basis.planned_charges},
    }  # fmt: skip
    decision = evaluate_replan(plan.basis, _observation(plan, planning_input, now=later, **kwargs))
    assert not (
        _codes(decision)
        & {
            ReplanTriggerCode.PRICE_FORECAST_CHANGED,
            ReplanTriggerCode.SOLAR_FORECAST_CHANGED,
            ReplanTriggerCode.CONSUMPTION_FORECAST_CHANGED,
        }
    )
