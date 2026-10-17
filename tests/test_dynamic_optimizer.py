#!/usr/bin/env python3
#
# tests/test_dynamic_optimizer.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Deterministic behaviour of the pure Dry-Run dynamic charging optimizer."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.limits import TARGET_SOC_MAX_PERCENT, TARGET_SOC_MIN_PERCENT
from app.optimizer.models import (
    BatteryState,
    ConsumptionForecast,
    ConsumptionForecastInterval,
    EnergyAction,
    PlanningInput,
    SolarForecast,
    SolarForecastInterval,
)
from app.optimizer.planner import DynamicChargeOptimizer, PlanParameters
from app.spotmarket.models import (
    SpotMarketProvider,
    SpotPriceInterval,
    SpotPriceSnapshot,
)

NOW = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)
SLOT = timedelta(minutes=15)


def _battery(
    soc: float = 50.0,
    capacity: float = 10_000.0,
    charge: float = 3000.0,
    discharge: float = 3000.0,
) -> BatteryState:
    return BatteryState(soc, capacity, charge, discharge, NOW)


def _prices(values: list[float], start: datetime = NOW) -> SpotPriceSnapshot:
    intervals = tuple(
        SpotPriceInterval(start + SLOT * i, start + SLOT * (i + 1), Decimal(str(value)))
        for i, value in enumerate(values)
    )
    return SpotPriceSnapshot(
        provider=SpotMarketProvider.ENERGY_CHARTS,
        bidding_zone="DE-LU",
        currency="EUR",
        fetched_at=NOW,
        intervals=intervals,
    )


def _consumption(per_slot: list[float], start: datetime = NOW) -> ConsumptionForecast:
    intervals = tuple(
        ConsumptionForecastInterval(start + SLOT * i, start + SLOT * (i + 1), value)
        for i, value in enumerate(per_slot)
    )
    return ConsumptionForecast(created_at=NOW, intervals=intervals)


def _solar(per_slot: list[float], start: datetime = NOW) -> SolarForecast:
    intervals = tuple(
        SolarForecastInterval(start + SLOT * i, start + SLOT * (i + 1), value)
        for i, value in enumerate(per_slot)
    )
    return SolarForecast(created_at=NOW, intervals=intervals)


def _plan(battery, prices, solar, consumption, hours: float = 2.0):
    return DynamicChargeOptimizer().plan(
        PlanningInput(NOW, battery, prices, solar, consumption, timedelta(hours=hours))
    )


def test_grid_is_end_bounded_and_substitutable_with_the_shared_grid() -> None:
    # Item 5: a horizon that is not an exact multiple of 15 min must yield NO slot whose end exceeds
    # now + horizon for BOTH optimizers. The planner's own grid and the shared align.build_grid (used
    # by the greedy optimizer) must agree at the horizon edge, full 15-minute slots only.
    from app.optimizer.align import build_grid

    now = datetime(2026, 1, 15, 10, 7, tzinfo=UTC)  # off-grid now
    horizon = timedelta(
        minutes=50
    )  # not a multiple of 15; last full slot must not overrun
    limit = now + horizon

    planner_grid = DynamicChargeOptimizer._build_grid(now, horizon)
    shared_grid = build_grid(now, horizon)

    # Neither grid emits a slot whose end exceeds now + horizon.
    assert planner_grid, "a sub-horizon that still fits full slots must not be empty"
    assert all(slot.end <= limit for slot in planner_grid)
    assert all(end <= limit for _start, end in shared_grid)
    # Both anchor at the next quarter-hour boundary and run the same full-slot span.
    assert (planner_grid[0].start, planner_grid[-1].end) == (
        shared_grid[0][0],
        shared_grid[-1][1],
    )
    assert len(planner_grid) == len(shared_grid)


def _charge_slots(plan):
    return [slot for slot in plan.slots if slot.action is EnergyAction.CHARGE]


def test_1_battery_full_and_enough_pv_plans_no_grid_charging() -> None:
    plan = _plan(
        _battery(soc=90.0),
        _prices([0.2] * 8),
        _solar([800.0] * 8),
        _consumption([100.0] * 8),
    )
    assert _charge_slots(plan) == []


def test_2_a_deficit_is_charged_as_late_as_permitted_not_in_the_cheap_window() -> None:
    # The spike at slot 7 draws 750 Wh; at 10 % (1000 Wh) with a 700 Wh reserve only 300 Wh are left
    # above reserve. The reserve is secured as late as permitted: in the spike slot itself, even
    # though slot 1 is far cheaper (price never moves charge into an earlier slot).
    prices = _prices([0.9, 0.1, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9])
    consumption = _consumption([0, 0, 0, 0, 0, 0, 0, 4000])
    plan = _plan(_battery(soc=10.0), prices, _solar([0.0] * 8), consumption)
    charge = _charge_slots(plan)
    assert len(charge) == 1
    assert charge[0].start == NOW + SLOT * 7 and charge[0].end == NOW + SLOT * 8
    assert "as late as permitted" in charge[0].reason


def test_3_a_cheaper_earlier_slot_loses_against_a_later_permitted_slot() -> None:
    # Slot 2 is the cheapest and 0.75 h earlier than the first slot the sustained load makes
    # necessary. It must stay unused: the cheaper price does not justify charging earlier.
    prices = _prices([0.9, 0.5, 0.1, 0.3, 0.9, 0.9, 0.9, 0.9])
    consumption = _consumption([600, 600, 600, 600, 600, 600, 600, 600])
    plan = _plan(
        _battery(soc=40.0, charge=2000.0), prices, _solar([0.0] * 8), consumption
    )
    charge_starts = [slot.start for slot in _charge_slots(plan)]
    assert charge_starts
    assert min(charge_starts) > NOW + SLOT * 2
    # Two hours earlier is never chosen for price alone.
    far = _plan(
        _battery(soc=10.0), _prices([0.9] * 8 + [0.1] + [0.9] * 8), _solar([0.0] * 17),
        _consumption([0] * 16 + [4000]), hours=4.25,
    )  # fmt: skip
    assert all(slot.start >= NOW + SLOT * 15 for slot in _charge_slots(far))


def test_3b_later_slot_wins_even_when_earlier_slot_is_cheaper() -> None:
    prices = _prices([0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.2, 0.5])
    consumption = _consumption([0, 0, 0, 0, 0, 0, 0, 4000])
    plan = _plan(_battery(soc=10.0), prices, _solar([0.0] * 8), consumption)
    charge = _charge_slots(plan)
    assert [slot.start for slot in charge] == [NOW + SLOT * 7]
    equal = _plan(
        _battery(soc=10.0), _prices([0.5] * 8), _solar([0.0] * 8), consumption
    )
    assert [slot.start for slot in _charge_slots(equal)] == [NOW + SLOT * 7]


def test_4_limited_charge_power_spreads_the_load_across_slots() -> None:
    # 1000 W charge power = 250 Wh/slot. A 1500 Wh deficit needs at least 6 slots.
    prices = _prices([0.1] * 12)
    consumption = _consumption([50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 50, 3000])
    plan = _plan(
        _battery(soc=30.0, charge=1000.0),
        prices,
        _solar([0.0] * 12),
        consumption,
        hours=3.0,
    )
    for slot in _charge_slots(plan):
        assert slot.max_power_w is not None
        assert slot.max_power_w <= 1000.0 + 1e-6


def test_5_max_soc_is_never_exceeded() -> None:
    prices = _prices(
        [-0.5] * 8
    )  # extremely cheap: the greedy allocator wants to charge a lot
    consumption = _consumption([3000] * 8)
    plan = _plan(
        _battery(soc=90.0, charge=5000.0), prices, _solar([0.0] * 8), consumption
    )
    for slot in plan.slots:
        if slot.target_soc_percent is not None:
            assert slot.target_soc_percent <= TARGET_SOC_MAX_PERCENT


def test_6_missing_solar_is_treated_as_zero_not_as_free_energy() -> None:
    # No PV at all (empty-but-present via tiny energy) must not suppress needed grid charging. The
    # battery can discharge at most 750 Wh/slot (3000 W), so a sustained load within that ceiling
    # drains it below the reserve over several slots, which is a genuine deficit to plan for.
    prices = _prices([0.1] * 8)
    consumption = _consumption([700, 700, 700, 700, 700, 700, 700, 700])
    plan_no_pv = _plan(_battery(soc=30.0), prices, _solar([0.0] * 8), consumption)
    assert _charge_slots(plan_no_pv), "a real deficit without PV must still be planned"


def test_9_a_negative_price_does_not_pull_charging_earlier() -> None:
    consumption = _consumption([600, 600, 600, 600, 600, 600, 600, 600])
    prices = _prices([0.5, 0.5, -0.2, 0.3, 0.5, 0.5, 0.5, 0.5])
    plan = _plan(
        _battery(soc=40.0, charge=2000.0), prices, _solar([0.0] * 8), consumption
    )
    assert all(slot.start > NOW + SLOT * 2 for slot in _charge_slots(plan))
    tie_prices = _prices([0.5, 0.5, 0.5, 0.5, 0.5, 0.5, -0.2, 0.5])
    tie = _plan(
        _battery(soc=10.0), tie_prices, _solar([0.0] * 8), _consumption([0, 0, 0, 0, 0, 0, 0, 4000])
    )  # fmt: skip
    assert [slot.start for slot in _charge_slots(tie)] == [NOW + SLOT * 7]


def test_10_a_dst_day_produces_the_right_slot_count_without_a_hardcoded_96() -> None:
    # Spring-forward local day: 23 h -> 92 fifteen-minute slots over the horizon.
    local_midnight = datetime(2026, 3, 29, 0, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    start = local_midnight.astimezone(UTC)
    next_midnight = (local_midnight + timedelta(days=1)).astimezone(UTC)
    horizon = next_midnight - start
    slots = int(horizon.total_seconds() // 900)
    prices = _prices([0.2] * slots, start=start)
    consumption = _consumption([100.0] * slots, start=start)
    solar = _solar([200.0] * slots, start=start)
    plan = DynamicChargeOptimizer().plan(
        PlanningInput(start, _battery(soc=80.0), prices, solar, consumption, horizon)
    )
    covered = sum(
        int((slot.end - slot.start).total_seconds() // 900) for slot in plan.slots
    )
    assert covered == 92


def test_11_pv_surplus_is_not_additionally_charged_from_the_grid() -> None:
    # PV always exceeds consumption, so the battery never needs grid energy.
    prices = _prices(
        [0.01] * 8
    )  # even very cheap prices must not trigger speculative charging
    consumption = _consumption([100.0] * 8)
    solar = _solar([1000.0] * 8)
    plan = _plan(_battery(soc=60.0), prices, solar, consumption)
    assert _charge_slots(plan) == []


def test_12_plan_slots_are_sorted_and_non_overlapping() -> None:
    prices = _prices([0.9, 0.1, 0.5, 0.2, 0.9, 0.9, 0.9, 0.9])
    consumption = _consumption([100, 100, 100, 100, 100, 100, 2000, 2000])
    plan = _plan(
        _battery(soc=40.0, charge=1500.0), prices, _solar([0.0] * 8), consumption
    )
    for earlier, later in zip(plan.slots, plan.slots[1:], strict=False):
        assert earlier.end <= later.start


def test_13_every_charge_slot_respects_the_target_soc_window() -> None:
    prices = _prices([0.1] * 8)
    consumption = _consumption([100, 100, 100, 100, 100, 100, 3000, 3000])
    plan = _plan(_battery(soc=20.0), prices, _solar([0.0] * 8), consumption)
    for slot in _charge_slots(plan):
        assert (
            TARGET_SOC_MIN_PERCENT <= slot.target_soc_percent <= TARGET_SOC_MAX_PERCENT
        )
        assert slot.max_power_w is not None and slot.max_power_w > 0


def test_plan_is_deterministic_for_identical_input() -> None:
    args = (
        _battery(soc=30.0),
        _prices([0.9, 0.1, 0.5, 0.2, 0.9, 0.9, 0.9, 0.9]),
        _solar([0.0] * 8),
        _consumption([100, 100, 100, 100, 100, 100, 2000, 2000]),
    )
    first = _plan(*args)
    second = _plan(*args)
    assert [
        (s.start, s.action, s.target_soc_percent, s.max_power_w, s.reason)
        for s in first.slots
    ] == [
        (s.start, s.action, s.target_soc_percent, s.max_power_w, s.reason)
        for s in second.slots
    ]


def test_reasons_are_printable_ascii_within_the_length_limit() -> None:
    from app.optimizer.models import REASON_MAX_LENGTH

    plan = _plan(
        _battery(soc=20.0),
        _prices([0.1] * 8),
        _solar([0.0] * 8),
        _consumption([100, 100, 100, 100, 100, 100, 3000, 3000]),
    )
    for slot in plan.slots:
        assert slot.reason
        assert len(slot.reason) <= REASON_MAX_LENGTH
        assert all(32 <= ord(ch) <= 126 for ch in slot.reason)


# --- review regression tests -------------------------------------------------------------------


def test_finding_1_charge_target_follows_battery_setpoint_not_grid_split() -> None:
    # The dispatch executor (app/dispatch/strategy.py) commands a CONSTANT battery charge power for
    # CHARGE_FROM_GRID; house load is served from the grid and does NOT reduce the commanded charge.
    # Start 50 % of 10 kWh = 5000 Wh; one slot with 500 Wh consumption and a forced 500 Wh grid
    # charge. The battery must rise by the full commanded 500 Wh -> ~55 %, not stay at 50 % (the old
    # grid-split model that wrongly netted house load against the commanded charge).
    reserve_at_half = PlanParameters(reserve_soc_percent=50.0, max_soc_percent=90.0)
    prices = _prices([0.1])  # one cheap slot forces the deficit charge
    consumption = _consumption([500.0])
    plan = DynamicChargeOptimizer(reserve_at_half).plan(
        PlanningInput(
            NOW,
            _battery(soc=50.0, charge=4000.0),
            prices,
            _solar([0.0]),
            consumption,
            SLOT,
        )
    )
    charge = _charge_slots(plan)
    assert charge
    assert charge[0].target_soc_percent == pytest.approx(55.0, abs=0.5)


def test_charge_slot_soc_gain_is_independent_of_house_load() -> None:
    # Same commanded charge energy, different in-slot consumption, must yield the same end-of-slot
    # CHARGE target: house load is served from the grid, never from the commanded battery charge.
    gain_low = DynamicChargeOptimizer._step_soc(
        5000.0,
        charge_wh=500.0,
        consumption_wh=0.0,
        solar_wh=0.0,
        reserve_wh=700.0,
        max_wh=9700.0,
        max_charge_wh=1000.0,
        max_discharge_wh=1000.0,
    )
    gain_high = DynamicChargeOptimizer._step_soc(
        5000.0,
        charge_wh=500.0,
        consumption_wh=5000.0,
        solar_wh=0.0,
        reserve_wh=700.0,
        max_wh=9700.0,
        max_charge_wh=1000.0,
        max_discharge_wh=1000.0,
    )
    assert gain_low == pytest.approx(5500.0)
    assert gain_high == pytest.approx(5500.0)


def test_finding_1_idle_then_charge_target_tracks_consumption_before_the_charge() -> (
    None
):
    # IDLE slots that drain the battery must lower the SoC the later CHARGE target is computed from,
    # so the target reflects the real trajectory and not an un-drained starting point.
    prices = _prices([0.9, 0.9, 0.1, 0.9, 0.9, 0.9, 0.9, 0.9])
    consumption = _consumption([800, 800, 100, 100, 100, 100, 2000, 2000])
    plan = _plan(
        _battery(soc=60.0, charge=3000.0), prices, _solar([0.0] * 8), consumption
    )
    for slot in _charge_slots(plan):
        assert (
            TARGET_SOC_MIN_PERCENT <= slot.target_soc_percent <= TARGET_SOC_MAX_PERCENT
        )


def test_finding_3_first_slot_starts_at_the_next_quarter_hour_when_now_is_mid_slot() -> (
    None
):
    # now = 12:14 (floors to 12:00). Planning must begin at 12:15, never place a partial 12:00-12:15
    # slot that would be charged with a full quarter hour of energy it cannot physically reach.
    now = datetime(2026, 1, 15, 12, 14, tzinfo=UTC)
    plan = DynamicChargeOptimizer().plan(
        PlanningInput(
            now,
            _battery(),
            _prices([0.2] * 8, start=datetime(2026, 1, 15, 12, 0, tzinfo=UTC)),
            _solar([0.0] * 8, start=datetime(2026, 1, 15, 12, 0, tzinfo=UTC)),
            _consumption([100.0] * 8, start=datetime(2026, 1, 15, 12, 0, tzinfo=UTC)),
            timedelta(hours=1),
        )
    )
    assert plan.slots
    # The already-elapsed 12:00-12:15 remainder is dropped: no plan slot starts before 12:15, and
    # every slot boundary is quarter-hour aligned (merged runs stay multiples of a quarter hour).
    assert plan.slots[0].start == datetime(2026, 1, 15, 12, 15, tzinfo=UTC)
    for slot in plan.slots:
        assert slot.start.minute % 15 == 0 and slot.start.second == 0
        assert slot.end.minute % 15 == 0 and slot.end.second == 0
        assert slot.start >= datetime(2026, 1, 15, 12, 15, tzinfo=UTC)


def test_finding_3_first_slot_is_kept_when_now_is_already_quarter_aligned() -> None:
    now = datetime(2026, 1, 15, 12, 15, tzinfo=UTC)
    plan = DynamicChargeOptimizer().plan(
        PlanningInput(
            now,
            _battery(),
            _prices([0.2] * 4, start=now),
            _solar([0.0] * 4, start=now),
            _consumption([100.0] * 4, start=now),
            timedelta(hours=1),
        )
    )
    assert plan.slots[0].start == now


def test_finding_4_huge_pv_surplus_cannot_store_more_than_the_charge_power_allows() -> (
    None
):
    # One slot with an enormous PV surplus. The SoC trajectory must clamp the stored energy to the
    # charge-power ceiling (1000 W -> 250 Wh/slot), so the end-of-slot SoC rises by at most that.
    end = DynamicChargeOptimizer._step_soc(
        5000.0,
        charge_wh=0.0,
        consumption_wh=0.0,
        solar_wh=100_000.0,
        reserve_wh=700.0,
        max_wh=9700.0,
        max_charge_wh=250.0,
        max_discharge_wh=250.0,
    )
    assert end == pytest.approx(5250.0)


def test_finding_4_discharge_is_clamped_to_the_discharge_power_in_the_trajectory() -> (
    None
):
    # A load far larger than the discharge-power ceiling drains the battery by at most that ceiling
    # in one slot; the rest is served from the grid in real time and does not drain the battery.
    end = DynamicChargeOptimizer._step_soc(
        5000.0,
        charge_wh=0.0,
        consumption_wh=100_000.0,
        solar_wh=0.0,
        reserve_wh=700.0,
        max_wh=9700.0,
        max_charge_wh=250.0,
        max_discharge_wh=250.0,
    )
    assert end == pytest.approx(4750.0)


def test_finding_4_pv_surplus_charge_target_reflects_the_power_clamp() -> None:
    # A continuous large PV surplus with a small charge-power ceiling: the plan plans no grid charge
    # (PV covers demand), and the merged idle trajectory never exceeds max SoC despite the surplus.
    prices = _prices([0.2] * 8)
    consumption = _consumption([100.0] * 8)
    solar = _solar([50_000.0] * 8)  # absurd surplus
    plan = _plan(
        _battery(soc=50.0, charge=1000.0, discharge=1000.0), prices, solar, consumption
    )
    assert _charge_slots(plan) == []
    for slot in plan.slots:
        if slot.target_soc_percent is not None:
            assert slot.target_soc_percent <= TARGET_SOC_MAX_PERCENT


def _limits(
    *, reserve_wh: float, max_wh: float, max_charge_wh: float, max_discharge_wh: float
):
    from app.optimizer.planner import _Limits

    return _Limits(
        reserve_wh=reserve_wh,
        max_wh=max_wh,
        max_charge_wh=max_charge_wh,
        max_discharge_wh=max_discharge_wh,
    )


def test_finding_2_deficit_capped_by_discharge_power() -> None:
    # 10 kW load for one slot = 2500 Wh demand, but the battery can only discharge 1 kW = 250 Wh in
    # that slot. The planner must only pre-charge the fraction the battery could actually deliver;
    # the excess load is served from the grid in reality and must not drive extra grid charging.
    optimizer = DynamicChargeOptimizer()
    start_wh = 5000.0
    limits = _limits(
        reserve_wh=700.0, max_wh=9700.0, max_charge_wh=250.0, max_discharge_wh=250.0
    )
    slots = optimizer._build_grid(NOW, SLOT * 2)
    slots[1].consumption_wh = 2500.0  # 10 kW load in a 15-min slot
    slots[1].price = 0.1
    slots[0].price = 0.1
    deficits = optimizer._simulate_deficits(slots, start_wh, limits)
    # Available above reserve entering slot 1 is 5000 - 700 = 4300 Wh, well above the 250 Wh the
    # battery could discharge, so there is NO deficit: nothing to pre-charge for un-dischargeable load.
    assert deficits == []


def test_finding_2_deficit_below_discharge_ceiling_behaves_as_before() -> None:
    # Net demand below the discharge ceiling but above the available reserve margin still reports the
    # true shortfall, so the guard does not suppress a genuine deficit.
    optimizer = DynamicChargeOptimizer()
    start_wh = 800.0  # only 100 Wh above the reserve floor
    limits = _limits(
        reserve_wh=700.0, max_wh=9700.0, max_charge_wh=1000.0, max_discharge_wh=1000.0
    )
    slots = optimizer._build_grid(NOW, SLOT * 2)
    slots[0].price = 0.1
    slots[1].price = 0.1
    slots[1].consumption_wh = 200.0
    deficits = optimizer._simulate_deficits(slots, start_wh, limits)
    # 200 Wh demand, 100 Wh available above reserve -> 100 Wh deficit at slot 1.
    assert len(deficits) == 1
    index, needed = deficits[0]
    assert index == 1
    assert needed == pytest.approx(100.0)


def test_finding_1_charge_slot_never_produces_a_reserve_deficit() -> None:
    # A slot carrying an explicit grid-charge command must walk ONLY the CHARGE transition in the
    # deficit sweep: the executor serves house load from the grid during a CHARGE slot, so even a
    # large in-slot consumption cannot discharge the pack and cannot create a reserve deficit here.
    # (The old sweep subtracted house load from the battery even on a charging slot.)
    optimizer = DynamicChargeOptimizer()
    start_wh = 800.0  # barely above the 700 Wh reserve
    limits = _limits(
        reserve_wh=700.0, max_wh=9700.0, max_charge_wh=1000.0, max_discharge_wh=1000.0
    )
    slots = optimizer._build_grid(NOW, SLOT * 2)
    slots[1].price = 0.1
    slots[1].consumption_wh = 5000.0  # huge house load...
    slots[1].charge_wh = 250.0  # ...but the slot is a commanded CHARGE slot
    deficits = optimizer._simulate_deficits(slots, start_wh, limits)
    assert deficits == []


def test_finding_3_grid_charge_not_placed_when_pv_saturates_charge_power() -> None:
    # A cheap positive-price slot whose forecast PV surplus already equals the charge-power ceiling.
    # Adding grid charge there would only curtail free PV without raising the SoC, so despite SoC
    # headroom and a later deficit, exactly 0 marginal grid charge may be placed in that slot.
    optimizer = DynamicChargeOptimizer()
    start_wh = 5000.0
    limits = _limits(
        reserve_wh=700.0, max_wh=9700.0, max_charge_wh=250.0, max_discharge_wh=250.0
    )
    slots = optimizer._build_grid(NOW, SLOT * 2)
    slots[0].price = 0.1
    slots[0].solar_wh = 250.0  # PV surplus saturates the charge-power ceiling
    slots[0].consumption_wh = 0.0
    placed = optimizer._place_charge(slots, 0, 200.0, start_wh, limits)
    assert placed == pytest.approx(0.0)
    assert slots[0].charge_wh == pytest.approx(0.0)


def test_finding_2_partial_pv_counts_only_the_marginal_stored_energy() -> None:
    # Unified marginal semantics: forecast PV already stores 125 Wh (half the 250 Wh ceiling) under
    # AUTO. Commanding charge REPLACES PV, so the real benefit is SoC(command) - SoC(AUTO). To add
    # 125 Wh of ADDITIONAL stored energy the commanded setpoint must be the full 250 Wh (the ceiling),
    # while only 125 Wh counts as deficit repair. So the commanded charge_wh (250) is strictly larger
    # than the marginal benefit returned (125): command-Wh != marginal-Wh.
    optimizer = DynamicChargeOptimizer()
    start_wh = 5000.0
    limits = _limits(
        reserve_wh=700.0, max_wh=9700.0, max_charge_wh=250.0, max_discharge_wh=250.0
    )
    slots = optimizer._build_grid(NOW, SLOT * 2)
    slots[0].price = 0.1
    slots[0].solar_wh = 125.0  # half the ceiling already banked by AUTO
    slots[0].consumption_wh = 0.0
    placed = optimizer._place_charge(slots, 0, 250.0, start_wh, limits)
    assert placed == pytest.approx(125.0)  # only the ADDITIONAL stored energy
    assert slots[0].charge_wh == pytest.approx(
        250.0
    )  # the full commanded setpoint (PV replaced)


def test_finding_2_net_house_load_mode_flip_counts_realized_not_commanded_energy() -> (
    None
):
    # The net-load discontinuity: AUTO would discharge 100 Wh in the candidate slot (house load, no
    # PV). Flipping the slot to CHARGE serves the house from the grid, so even a modest command both
    # avoids that 100 Wh discharge AND stores the commanded intake on top. The marginal benefit over
    # AUTO therefore JUMPS past the small reserve shortfall and cannot land exactly on it. The planner
    # must count the TRUE realized marginal (via _step_soc), not the commanded Wh.
    optimizer = DynamicChargeOptimizer()
    start_wh = 5000.0
    limits = _limits(
        reserve_wh=700.0, max_wh=9700.0, max_charge_wh=1000.0, max_discharge_wh=1000.0
    )
    slots = optimizer._build_grid(NOW, SLOT * 2)
    slots[0].price = 0.1
    slots[0].consumption_wh = 100.0  # AUTO discharges 100 Wh here
    slots[0].solar_wh = 0.0
    placed = optimizer._place_charge(
        slots, 0, 50.0, start_wh, limits
    )  # only 50 Wh reserve needed
    # command stores 50 Wh from soc_before -> SoC 5050; AUTO would have been 4900 -> realized 150.
    assert slots[0].charge_wh == pytest.approx(50.0)
    assert placed == pytest.approx(
        150.0
    )  # realized marginal overshoots the 50 Wh want, honestly
    assert placed > 50.0 + 1e-6  # command-Wh (50) != marginal-Wh (150)


def test_finding_3_merge_never_reports_power_above_any_constituent_slot() -> None:
    # Adjacent CHARGE slots planned at different powers must not collapse into one run that inherits
    # the strongest slot's power across the whole span (the old merge keyed only on action+reason and
    # reported the run's peak). Construct a spread deficit over cheap slots with a limited charge
    # power so the per-slot commanded energy varies, then assert every emitted CHARGE slot's power is
    # one its own constituent slots actually planned -- never a maximum borrowed from a neighbour.
    prices = _prices([0.1, 0.1, 0.1, 0.1, 0.9, 0.9, 0.9, 0.9])
    consumption = _consumption([200, 200, 200, 200, 200, 200, 200, 3000])
    plan = _plan(
        _battery(soc=25.0, charge=2000.0), prices, _solar([0.0] * 8), consumption
    )
    charge = _charge_slots(plan)
    assert charge
    for slot in charge:
        # A merged CHARGE slot spans N quarter-hours; the reported power times its duration may not
        # exceed what a constant commanded charge at that power stores, i.e. the power is a single
        # truthful value for the whole emitted slot (no 400 W window reported as 2000 W).
        assert slot.max_power_w is not None
        assert 0.0 < slot.max_power_w <= 2000.0 + 1e-6


def test_finding_3_adjacent_charge_slots_with_different_power_stay_separate() -> None:
    # Directly drive the merge: two adjacent CHARGE slots with the same reason but different commanded
    # power must emit as two slots, each carrying its own power -- not one merged slot at the higher
    # power. A single IDLE tail keeps the plan well-formed.
    optimizer = DynamicChargeOptimizer()
    battery = _battery(soc=50.0, capacity=10_000.0, charge=4000.0, discharge=4000.0)
    slots = optimizer._build_grid(NOW, SLOT * 3)
    for slot in slots:
        slot.price = 0.1
    slots[0].charge_wh = 100.0  # 400 W
    slots[1].charge_wh = 500.0  # 2000 W
    optimizer._label(slots)
    merged = optimizer._merge(slots, battery, battery.usable_capacity_wh)
    charge = [slot for slot in merged if slot.action is EnergyAction.CHARGE]
    assert len(charge) == 2
    powers = sorted(slot.max_power_w for slot in charge)
    assert powers == pytest.approx([400.0, 2000.0])
    # Neither emitted slot borrows the other's power: the 400 W slot stays 400 W.
    assert min(powers) == pytest.approx(400.0)


def test_finding_9_plan_respects_effective_executor_window() -> None:
    # With the executor cap narrowed to 95 %, no plan slot's target may exceed 95 % even when a
    # deep deficit and extremely cheap prices tempt the greedy allocator to charge higher.
    from app.energy.soc_window import effective_target_window

    reserve, cap = effective_target_window(5.0, 95.0)
    params = PlanParameters(reserve_soc_percent=reserve, max_soc_percent=cap)
    prices = _prices([-0.5] * 8)
    consumption = _consumption([3000] * 8)
    plan = DynamicChargeOptimizer(params).plan(
        PlanningInput(
            NOW,
            _battery(soc=90.0, charge=5000.0),
            prices,
            _solar([0.0] * 8),
            consumption,
            timedelta(hours=2),
        )
    )
    for slot in plan.slots:
        if slot.target_soc_percent is not None:
            assert slot.target_soc_percent <= 95.0


def test_effective_target_window_matches_manager() -> None:
    from app.energy.models import TargetSocWindow
    from app.energy.soc_window import effective_target_window

    for min_soc, max_soc in [(5.0, 95.0), (0.0, 100.0), (10.0, 80.0), (7.0, 97.0)]:
        lower, upper = effective_target_window(min_soc, max_soc)
        expected = TargetSocWindow(
            min=max(TARGET_SOC_MIN_PERCENT, min_soc),
            max=min(TARGET_SOC_MAX_PERCENT, max_soc),
        )
        assert (lower, upper) == (expected.min, expected.max)


# ---- Charge policy, reserve vs. target, power envelope and energy domains ----

from app.optimizer.models import (
    ChargeEfficiency,
    EfficiencySource,
    LimitingReasonCode,
    PlanPolicy,
)

_CODES = LimitingReasonCode


def _slot_count(hours: float) -> int:
    return int(hours * 4)


def _policy_plan(
    params: PlanParameters,
    *,
    soc: float = 30.0,
    charge: float = 4000.0,
    live: float | None = None,
    consumption: list[float] | None = None,
    prices: SpotPriceSnapshot | None = None,
    hours: float = 8.0,
):
    n = _slot_count(hours)
    battery = BatteryState(soc, 10_000.0, charge, 3000.0, NOW, live_charge_limit_w=live)
    planning_input = PlanningInput(
        NOW, battery, prices or _prices([0.2] * n), _solar([0.0] * n),
        _consumption(consumption or [100.0] * n), timedelta(hours=hours),
    )  # fmt: skip
    return DynamicChargeOptimizer(params).plan(planning_input)


def _codes(plan) -> set[LimitingReasonCode]:
    return {reason.code for reason in plan.limiting_reasons}


def _preferred(policy: PlanParameters | None = None, **kwargs) -> PlanParameters:
    return PlanParameters(
        preferred_charge_power_min_w=500.0, preferred_charge_power_max_w=1000.0,
        target_soc_percent=90.0, target_time=NOW + timedelta(hours=3), **kwargs,
    )  # fmt: skip


def _min_soc_percent(plan) -> float:
    return min(soc for _, soc in plan.basis.trajectory)


def test_gentle_stays_in_the_preferred_range_and_reports_an_unreachable_target() -> (
    None
):
    plan = _policy_plan(_preferred(policy=PlanPolicy.GENTLE))
    charge = _charge_slots(plan)
    assert charge
    assert all(
        500.0 - 1e-6 <= slot.max_power_w <= 1000.0 + 1e-6 for slot in charge
    )  # confirmed range
    assert plan.policy is PlanPolicy.GENTLE
    assert plan.target_reachable is False and plan.unserved_target_energy_wh > 0
    assert plan.reserve_satisfied is True
    assert _min_soc_percent(plan) >= TARGET_SOC_MIN_PERCENT - 1e-6
    assert _CODES.TARGET_UNREACHABLE_IN_PREFERRED_RANGE in _codes(plan)


def test_meet_deadline_raises_power_to_the_hard_limit_and_reaches_the_target() -> None:
    params = PlanParameters(
        policy=PlanPolicy.MEET_DEADLINE, preferred_charge_power_min_w=500.0, preferred_charge_power_max_w=1000.0,
        target_soc_percent=90.0, target_time=NOW + timedelta(hours=3),
    )  # fmt: skip
    plan = _policy_plan(params)
    charge = _charge_slots(plan)
    assert plan.target_reachable is True and plan.unserved_target_energy_wh == 0.0
    assert max(slot.max_power_w for slot in charge) > 1000.0
    assert all(
        slot.max_power_w <= 4000.0 + 1e-6 for slot in charge
    )  # the hard limit is never exceeded
    assert _CODES.PREFERRED_RANGE_EXCEEDED_FOR_DEADLINE in _codes(plan)
    assert any(soc >= 90.0 - 1e-6 for _, soc in plan.basis.trajectory)


def test_meet_deadline_stays_in_the_preferred_range_when_that_is_enough() -> None:
    params = PlanParameters(
        policy=PlanPolicy.MEET_DEADLINE, preferred_charge_power_min_w=500.0, preferred_charge_power_max_w=1000.0,
        target_soc_percent=45.0, target_time=NOW + timedelta(hours=3),
    )  # fmt: skip
    plan = _policy_plan(params)
    assert plan.target_reachable is True
    assert all(slot.max_power_w <= 1000.0 + 1e-6 for slot in _charge_slots(plan))
    assert _CODES.PREFERRED_RANGE_EXCEEDED_FOR_DEADLINE not in _codes(plan)


def test_reserve_dip_before_the_target_is_covered_first_and_the_target_charge_stays_late() -> (
    None
):
    n = _slot_count(10.0)
    consumption = [50.0] * n
    consumption[16] = 4000.0  # 02:00 night spike, battery barely above reserve
    prices = _prices([0.9, 0.9, 0.1] + [0.5] * (n - 3))
    target_time = NOW + timedelta(hours=9)
    params = PlanParameters(target_soc_percent=80.0, target_time=target_time)
    plan = _policy_plan(
        params, soc=12.0, consumption=consumption, prices=prices, hours=10.0
    )
    assert plan.reserve_satisfied is True and plan.target_reachable is True
    assert _min_soc_percent(plan) >= TARGET_SOC_MIN_PERCENT - 1e-6
    charge = _charge_slots(plan)
    dip = NOW + timedelta(hours=4)  # 02:00
    assert any(
        slot.end <= dip for slot in charge
    ), "the night dip needs charge before it"
    assert charge[-1].end == target_time
    # High SoC is reached as late as permitted: the battery is still low 1.5 h before the target time.
    soc_before_final_run = dict(plan.basis.trajectory)[
        target_time - timedelta(hours=1, minutes=30)
    ]
    assert soc_before_final_run < 35.0


def test_reserve_and_target_flags_are_reported_independently() -> None:
    n = _slot_count(8.0)
    consumption = [50.0] * n
    consumption[2] = (
        4000.0  # dip while no price exists yet, so nothing can be charged before it
    )
    late_prices = _prices([0.2] * (n - 8), start=NOW + SLOT * 8)
    params = PlanParameters(
        target_soc_percent=80.0, target_time=NOW + timedelta(hours=7)
    )
    plan = _policy_plan(params, soc=10.0, consumption=consumption, prices=late_prices)
    assert plan.reserve_satisfied is False and plan.reserve_shortfall_wh > 0
    assert plan.target_reachable is True and plan.unserved_target_energy_wh == 0.0
    assert _CODES.RESERVE_NOT_SECURABLE in _codes(plan)
    # The opposite combination is the gentle scenario: reserve kept, target not reachable.
    gentle = _policy_plan(_preferred(policy=PlanPolicy.GENTLE))
    assert gentle.reserve_satisfied is True and gentle.target_reachable is False


def test_no_target_means_a_vacuously_reachable_target() -> None:
    plan = _policy_plan(PlanParameters(), soc=90.0)
    assert plan.target_soc_percent is None and plan.target_time is None
    assert plan.target_reachable is True and plan.unserved_target_energy_wh == 0.0


def test_a_target_above_the_maximum_soc_is_reported_not_forced() -> None:
    params = PlanParameters(
        max_soc_percent=80.0,
        target_soc_percent=90.0,
        target_time=NOW + timedelta(hours=6),
    )
    plan = _policy_plan(params, charge=6000.0)
    assert plan.target_reachable is False
    assert _CODES.TARGET_ABOVE_MAX_SOC in _codes(plan)
    assert max(soc for _, soc in plan.basis.trajectory) <= 80.0 + 1e-6


def test_grid_charge_energy_is_stored_energy_divided_by_the_efficiency_exactly_once() -> (
    None
):
    efficiency = ChargeEfficiency(0.8, EfficiencySource.CONFIGURED)
    params = PlanParameters(
        charge_efficiency=efficiency, target_soc_percent=70.0, target_time=NOW + timedelta(hours=6)
    )  # fmt: skip
    plan = _policy_plan(params)
    energy = plan.energy
    assert energy.stored_energy_delta_wh > 0
    assert energy.grid_charge_energy_wh == pytest.approx(
        energy.stored_energy_delta_wh / 0.8
    )
    assert energy.battery_terminal_energy_wh == pytest.approx(
        energy.stored_energy_delta_wh
    )
    assert energy.stored_energy_delta_wh == pytest.approx(
        sum(c.terminal_energy_wh for c in plan.basis.planned_charges)
    )
    for slot in _charge_slots(plan):
        assert slot.energy.grid_charge_energy_wh == pytest.approx(
            slot.energy.stored_energy_delta_wh / 0.8
        )
    assert plan.assumptions.charge_efficiency == efficiency
    # The efficiency changes the grid term only, never the simulated SoC trajectory.
    reference = _policy_plan(
        PlanParameters(target_soc_percent=70.0, target_time=NOW + timedelta(hours=6))
    )
    assert reference.basis.trajectory == plan.basis.trajectory


def test_without_a_configured_efficiency_an_explicit_assumption_is_reported() -> None:
    plan = _policy_plan(
        PlanParameters(target_soc_percent=70.0, target_time=NOW + timedelta(hours=6))
    )
    assumed = plan.assumptions.charge_efficiency
    assert assumed.source is EfficiencySource.ASSUMED and 0 < assumed.value < 1
    assert plan.energy.grid_charge_energy_wh == pytest.approx(
        plan.energy.stored_energy_delta_wh / assumed.value
    )
    assert plan.assumptions.power_reference_verified is False
    assert plan.assumptions.forecast_basis
    assumptions = plan.assumptions
    assert "provisionally as battery-side" in assumptions.power_reference
    assert "not faked" in assumptions.live_limit_status
    assert (
        "explicit planning assumption, not a verified device property"
        in assumptions.charge_efficiency_note
    )
    assert (
        "unverified" in assumptions.preferred_range_status
        and "not a hard" in assumptions.preferred_range_status
    )


def test_missing_live_limit_keeps_the_static_limit_and_is_visible() -> None:
    params = PlanParameters(
        target_soc_percent=90.0, target_time=NOW + timedelta(hours=6)
    )
    plan = _policy_plan(params, charge=2000.0)
    assert (
        plan.power.live_limit_available is False
        and plan.power.live_charge_limit_w is None
    )
    assert (
        plan.power.static_charge_limit_w == 2000.0
        and plan.power.permitted_charge_power_w == 2000.0
    )
    assert max(slot.max_power_w for slot in _charge_slots(plan)) <= 2000.0 + 1e-6
    assert _CODES.LIVE_LIMIT_UNAVAILABLE in _codes(plan)


def test_a_lower_live_limit_wins_over_the_static_limit() -> None:
    params = PlanParameters(
        target_soc_percent=90.0, target_time=NOW + timedelta(hours=6)
    )
    plan = _policy_plan(params, charge=3000.0, live=1000.0)
    assert (
        plan.power.live_limit_available is True
        and plan.power.permitted_charge_power_w == 1000.0
    )
    assert max(slot.max_power_w for slot in _charge_slots(plan)) <= 1000.0 + 1e-6
    assert _CODES.LIVE_LIMIT_BELOW_STATIC in _codes(plan)
    # A live limit above the static one never raises the permitted power.
    higher = _policy_plan(params, charge=3000.0, live=9000.0)
    assert higher.power.permitted_charge_power_w == 3000.0


def test_preferred_range_is_clamped_into_the_permitted_power() -> None:
    params = PlanParameters(
        preferred_charge_power_min_w=800.0, preferred_charge_power_max_w=5000.0
    )
    plan = _policy_plan(params, charge=2000.0, live=900.0)
    assert plan.power.preferred_max_w == 900.0 and plan.power.preferred_min_w == 800.0
    assert _CODES.PREFERRED_RANGE_CLAMPED in _codes(plan)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_soc_percent": 80.0},
        {"target_time": NOW},
        {"target_soc_percent": 99.0, "target_time": NOW},
        {
            "target_soc_percent": 80.0,
            "target_time": datetime(2026, 1, 16, 6, 0),  # noqa: DTZ001
        },
        {"preferred_charge_power_min_w": 500.0},
        {"preferred_charge_power_min_w": 900.0, "preferred_charge_power_max_w": 500.0},
        {"preferred_charge_power_min_w": 0.0, "preferred_charge_power_max_w": 500.0},
    ],
)
def test_plan_parameters_reject_inconsistent_policy_inputs(kwargs) -> None:
    with pytest.raises(ValueError):
        PlanParameters(**kwargs)


@pytest.mark.parametrize("value", [0.0, -0.1, 1.01, float("nan")])
def test_charge_efficiency_must_be_a_fraction(value) -> None:
    with pytest.raises(ValueError):
        ChargeEfficiency(value, EfficiencySource.CONFIGURED)


def test_policy_planning_is_deterministic_and_fast_over_a_long_horizon() -> None:
    params = PlanParameters(
        policy=PlanPolicy.MEET_DEADLINE, preferred_charge_power_min_w=500.0, preferred_charge_power_max_w=1000.0,
        target_soc_percent=90.0, target_time=NOW + timedelta(hours=30),
    )  # fmt: skip
    first = _policy_plan(params, hours=36.0)
    assert first == _policy_plan(params, hours=36.0)


def _smoothing_fixture(
    early: float,
    late: float,
    *,
    consumption_first: float = 0.0,
    start_wh: float = 5000.0,
):
    optimizer = DynamicChargeOptimizer()
    limits = _limits(
        reserve_wh=700.0, max_wh=9700.0, max_charge_wh=500.0, max_discharge_wh=750.0
    )
    slots = optimizer._build_grid(NOW, SLOT * 4)
    for slot in slots:
        slot.price = 0.2
    slots[0].charge_wh, slots[1].charge_wh = early, late
    slots[0].consumption_wh = consumption_first
    return optimizer, slots, limits, start_wh


def test_smoothing_folds_a_partial_slot_into_its_later_neighbour_when_everything_holds() -> (
    None
):
    optimizer, slots, limits, start_wh = _smoothing_fixture(100.0, 300.0)
    final_before = optimizer._soc_entering(slots, len(slots), start_wh, limits)
    optimizer._smooth_charge(slots, start_wh, limits, limits.max_charge_wh, 10_000.0)
    assert slots[0].charge_wh == 0.0 and slots[1].charge_wh == pytest.approx(
        400.0
    )  # energy preserved
    assert optimizer._soc_entering(
        slots, len(slots), start_wh, limits
    ) == pytest.approx(final_before)
    assert optimizer._simulate_deficits(slots, start_wh, limits) == []
    assert slots[1].charge_wh <= limits.max_charge_wh  # hard limit kept


def test_smoothing_keeps_a_small_leftover_slot_that_the_neighbour_cannot_absorb() -> (
    None
):
    optimizer, slots, limits, start_wh = _smoothing_fixture(
        100.0, 500.0
    )  # neighbour already at the limit
    optimizer._smooth_charge(slots, start_wh, limits, limits.max_charge_wh, 10_000.0)
    assert (slots[0].charge_wh, slots[1].charge_wh) == (100.0, 500.0)


def test_smoothing_is_refused_when_it_would_open_a_reserve_deficit() -> None:
    # The first slot's charge keeps the house off a battery that sits at the reserve.
    optimizer, slots, limits, start_wh = _smoothing_fixture(
        100.0, 300.0, consumption_first=300.0, start_wh=700.0
    )
    optimizer._smooth_charge(slots, start_wh, limits, limits.max_charge_wh, 10_000.0)
    assert (slots[0].charge_wh, slots[1].charge_wh) == (100.0, 300.0)


def test_efficiency_is_not_applied_to_the_soc_trajectory_a_second_time() -> None:
    # No load and no PV: the SoC rise across the charge slots is exactly the battery-side energy,
    # and only the grid energy is that energy divided by 0.9.
    zero = [0.0] * _slot_count(8.0)
    params = PlanParameters(
        target_soc_percent=60.0, target_time=NOW + timedelta(hours=3)
    )
    plan = _policy_plan(params, consumption=zero)
    battery_wh = sum(
        slot.energy.commanded_charge_energy_wh for slot in _charge_slots(plan)
    )
    soc_delta_wh = (
        (plan.basis.trajectory[-1][1] - plan.basis.trajectory[0][1]) / 100.0 * 10_000.0
    )
    assert plan.assumptions.charge_efficiency.value == 0.90
    assert soc_delta_wh == pytest.approx(battery_wh) == pytest.approx(3000.0)
    assert plan.energy.stored_energy_delta_wh == pytest.approx(battery_wh)
    assert plan.energy.grid_charge_energy_wh == pytest.approx(battery_wh / 0.9)
    lossless = _policy_plan(
        PlanParameters(
            target_soc_percent=60.0, target_time=NOW + timedelta(hours=3),
            charge_efficiency=ChargeEfficiency(1.0, EfficiencySource.CONFIGURED),
        ),
        consumption=zero,
    )  # fmt: skip
    assert lossless.basis.trajectory == plan.basis.trajectory


def _small_need_plan(params: PlanParameters):
    # 13.5 % (1350 Wh) with a 700 Wh reserve and a 750 Wh spike: a deficit of only ~100 Wh.
    consumption = [0.0] * 7 + [4000.0]
    battery = BatteryState(13.5, 10_000.0, 3000.0, 3000.0, NOW)
    planning_input = PlanningInput(
        NOW, battery, _prices([0.5] * 8), _solar([0.0] * 8), _consumption(consumption), timedelta(hours=2)
    )  # fmt: skip
    return DynamicChargeOptimizer(params).plan(planning_input)


def test_without_a_confirmed_range_a_partial_power_slot_is_allowed() -> None:
    plan = _small_need_plan(PlanParameters())
    (slot,) = _charge_slots(plan)
    assert (
        slot.start == NOW + SLOT * 7 and slot.max_power_w < 500.0
    )  # no minimum power is invented
    assert _CODES.PREFERRED_MINIMUM_EXTRA_ENERGY not in _codes(plan)


@pytest.mark.parametrize("policy", [PlanPolicy.GENTLE, PlanPolicy.MEET_DEADLINE])
def test_a_confirmed_range_never_gets_a_slot_below_its_minimum(policy) -> None:
    params = PlanParameters(
        policy=policy, preferred_charge_power_min_w=500.0, preferred_charge_power_max_w=1000.0
    )  # fmt: skip
    plan = _small_need_plan(params)
    (slot,) = _charge_slots(plan)
    assert slot.start == NOW + SLOT * 7  # the minimum does not pull charging earlier
    assert slot.max_power_w == pytest.approx(500.0)
    assert _CODES.PREFERRED_MINIMUM_EXTRA_ENERGY in _codes(
        plan
    )  # the surplus is reported
