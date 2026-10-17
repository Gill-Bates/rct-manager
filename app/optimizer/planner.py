#!/usr/bin/env python3
#
# app/optimizer/planner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The pure Dry-Run energy optimizer.

This module is the pure optimization algorithm. It takes a fully-materialised
:class:`~app.optimizer.models.PlanningInput` and returns an
:class:`~app.optimizer.models.EnergyPlan`. It never touches hardware, a store, the gateway, dispatch
or an executor; the architecture-isolation test asserts it imports none of them. Data acquisition
and domain adaptation live in ``app.optimizer.adapters``.

Scope v1 (Dry-Run): plan grid charging into cheap windows only, enough to keep the battery from
undercutting its required reserve given forecast consumption minus forecast PV. It deliberately does
NOT plan grid export, speculative discharge-to-grid, or arbitrage cycling for small price spreads.
Every central assumption is a named constant or an explicit field of :class:`PlanParameters`; there
is no hidden tuning.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.clock import require_aware
from app.limits import TARGET_SOC_MAX_PERCENT, TARGET_SOC_MIN_PERCENT
from app.optimizer.models import (
    DEFAULT_ASSUMED_CHARGE_EFFICIENCY,
    BatteryState,
    ChargeEfficiency,
    ChargePowerEnvelope,
    ConsumptionForecast,
    EfficiencySource,
    EnergyAction,
    EnergyPlan,
    EnergyPlanSlot,
    LimitingReason,
    LimitingReasonCode,
    PlanAssumptions,
    PlanBasis,
    PlanEnergy,
    PlannedCharge,
    PlanningInput,
    PlanPolicy,
    SlotSeries,
    SolarForecast,
)

# The planning grid resolution. Prices, PV and consumption are all quarter-hourly.
SLOT = timedelta(minutes=15)
_SLOT_HOURS = SLOT.total_seconds() / 3600.0
# How long a produced Dry-Run plan is considered valid before it should be recomputed. A preview is
# recomputed lazily on request, so this only bounds staleness of a cached plan.
PLAN_VALIDITY = timedelta(minutes=30)
# Numerical slack (Wh) below which an energy deficit is treated as none, so floating-point dust does
# not create a spurious one-watt-hour charge slot.
_ENERGY_EPSILON_WH = 1.0
# Physical contract of the charge setpoint. The dispatch executor writes the signed W value to the
# inverter's battery-power register; the object catalog does not say whether that is measured at
# the battery terminals or on the AC side, so this stays an unverified assumption.
POWER_REFERENCE = "power_mng_battery_power_extern is interpreted provisionally as battery-side (unverified)"
PREFERRED_RANGE_STATUS = (
    "unverified; a soft preference of the gentle policy, not a hard planning constraint"
)
BATTERY_INTERNAL_LOSSES = "not modelled: terminal energy equals stored energy, usable capacity is in terminal Wh"


def planning_start(now: datetime) -> datetime:
    """The instant the plan's first full quarter-hour slot begins.

    ``now`` is floored to the quarter-hour that contains it; planning begins at the *following*
    boundary unless ``now`` already sits exactly on a boundary. A partial current slot is dropped so
    the first slot is always a complete quarter hour. This is the single alignment rule the planner
    builds its grid from (:meth:`DynamicChargeOptimizer._build_grid`); the adapter imports it so the
    coverage checks anchor on the exact same instant the planner will actually start from, and the
    two cannot drift.
    """
    aligned = now - timedelta(
        minutes=now.minute % 15, seconds=now.second, microseconds=now.microsecond
    )
    return aligned if aligned == now else aligned + SLOT


@dataclass(frozen=True, slots=True)
class PlanParameters:
    """The explicit policy knobs of the Dry-Run optimizer. No value is hidden in the algorithm.

    ``reserve_soc_percent`` is the SoC the plan must never let the simulated battery fall below,
    along the whole trajectory; ``max_soc_percent`` the ceiling it must never charge above. Both
    default to the shared product SoC window so the Dry-Run plan cannot propose a target the executor
    contract would reject.

    ``target_soc_percent`` at ``target_time`` is an optional planning goal, distinct from the reserve.
    ``preferred_charge_power_min_w``/``_max_w`` are a *confirmed* preferred power range (both or
    neither); nothing here derives a gentler power from a few measurements. ``charge_efficiency`` is
    a constant; without a configured or verified value the documented assumption applies.
    """

    reserve_soc_percent: float = TARGET_SOC_MIN_PERCENT
    max_soc_percent: float = TARGET_SOC_MAX_PERCENT
    policy: PlanPolicy = PlanPolicy.GENTLE
    target_soc_percent: float | None = None
    target_time: datetime | None = None
    preferred_charge_power_min_w: float | None = None
    preferred_charge_power_max_w: float | None = None
    charge_efficiency: ChargeEfficiency = DEFAULT_ASSUMED_CHARGE_EFFICIENCY

    def __post_init__(self) -> None:
        for name in ("reserve_soc_percent", "max_soc_percent"):
            value = getattr(self, name)
            if (
                not math.isfinite(value)
                or not TARGET_SOC_MIN_PERCENT <= value <= TARGET_SOC_MAX_PERCENT
            ):
                raise ValueError(f"{name} must be within the product SoC window")
        if self.reserve_soc_percent >= self.max_soc_percent:
            raise ValueError("reserve_soc_percent must be below max_soc_percent")
        if not isinstance(self.policy, PlanPolicy):
            raise TypeError("policy must be a PlanPolicy")
        if not isinstance(self.charge_efficiency, ChargeEfficiency):
            raise TypeError("charge_efficiency must be a ChargeEfficiency")
        if (self.target_soc_percent is None) != (self.target_time is None):
            raise ValueError("target_soc_percent and target_time must be set together")
        if self.target_soc_percent is not None:
            target = self.target_soc_percent
            if (
                not math.isfinite(target)
                or not TARGET_SOC_MIN_PERCENT <= target <= TARGET_SOC_MAX_PERCENT
            ):
                raise ValueError(
                    "target_soc_percent must be within the product SoC window"
                )
            require_aware(self.target_time, "target_time")
        low, high = self.preferred_charge_power_min_w, self.preferred_charge_power_max_w
        if (low is None) != (high is None):
            raise ValueError(
                "the preferred charge power range needs both a minimum and a maximum"
            )
        invalid = (
            low is not None
            and high is not None
            and (
                not (math.isfinite(low) and math.isfinite(high))
                or low <= 0
                or high < low
            )
        )
        if invalid:
            raise ValueError(
                "the preferred charge power range must be finite with 0 < min <= max"
            )


@dataclass(slots=True)
class _Slot:
    start: datetime
    end: datetime
    consumption_wh: float = 0.0
    solar_wh: float = 0.0
    price: float | None = (
        None  # relative ranking only (day-ahead, not full household price)
    )
    charge_wh: float = 0.0  # commanded grid-charge energy the plan places here
    action: EnergyAction = EnergyAction.IDLE
    reason: str = ""
    for_reserve: bool = False  # charge placed to cover a reserve deficit
    for_target: bool = False  # charge placed to reach the target SoC
    floor_extra_wh: float = (
        0.0  # commanded energy beyond the need, from the preferred minimum
    )


@dataclass(frozen=True, slots=True)
class _Limits:
    """The per-slot energy limits every trajectory walk shares (reserve/max SoC and the power caps)."""

    reserve_wh: float
    max_wh: float
    max_charge_wh: float
    max_discharge_wh: float
    # Confirmed preferred minimum per slot (0 when no range is confirmed: partial power is free).
    min_command_wh: float = 0.0


@dataclass(slots=True)
class _Outcome:
    """What the charge placement could not do, for the plan's flags and limiting reasons."""

    reserve_shortfall_wh: float = 0.0
    target_shortfall_wh: float = 0.0
    reasons: list[LimitingReason] = field(default_factory=list)


class DynamicChargeOptimizer:
    """A concrete :class:`~app.optimizer.base.EnergyOptimizer` for the first Dry-Run version."""

    def __init__(self, parameters: PlanParameters | None = None) -> None:
        self._params = parameters or PlanParameters()

    @staticmethod
    def _step_soc(
        soc_wh: float,
        *,
        charge_wh: float,
        consumption_wh: float,
        solar_wh: float,
        reserve_wh: float,
        max_wh: float,
        max_charge_wh: float,
        max_discharge_wh: float,
    ) -> float:
        """Advance the battery one slot, honouring both power limits and the SoC window.

        Two branches match the authoritative dispatch executor
        (:mod:`app.dispatch.strategy`), where ``CHARGE_FROM_GRID`` commands a *constant* battery
        charge power that house load does not reduce:

        * **Explicit CHARGE slot** (``charge_wh > _ENERGY_EPSILON_WH``) — the battery is charged by
          the commanded grid energy, bounded only by the charge-power ceiling (``max_charge_wh``)
          and the max-SoC headroom. Household demand is served from the grid in reality, so it does
          not offset the commanded charge, and PV surplus is not added on top of a commanded charge.
        * **AUTO/IDLE slot** — the net battery flow is ``solar_wh - consumption_wh``: PV charges and
          household demand discharges. A net inflow is clamped to ``max_charge_wh`` and the max-SoC
          headroom; a net outflow to ``max_discharge_wh`` (the discharge-power ceiling) and the
          reserve floor. Both caps apply to the simulated trajectory, so a large PV surplus cannot
          store an unreachable amount of energy in one slot.
        """
        if charge_wh > _ENERGY_EPSILON_WH:
            charge_in = min(charge_wh, max_charge_wh, max(0.0, max_wh - soc_wh))
            return soc_wh + charge_in
        net_auto_wh = solar_wh - consumption_wh
        if net_auto_wh >= 0.0:
            charge_in = min(net_auto_wh, max_charge_wh, max(0.0, max_wh - soc_wh))
            return soc_wh + charge_in
        discharge = min(-net_auto_wh, max_discharge_wh, max(0.0, soc_wh - reserve_wh))
        return soc_wh - discharge

    def _step_slot(self, soc_wh: float, slot: "_Slot", limits: "_Limits") -> float:
        """Advance one slot's SoC through the single authoritative transition (:meth:`_step_soc`).

        Every site that walks the battery trajectory — deficit detection, charge placement and the
        merge projection — routes through this one call so none of them can re-derive the arithmetic
        and silently disagree with the others.
        """
        return self._step_soc(
            soc_wh,
            charge_wh=slot.charge_wh,
            consumption_wh=slot.consumption_wh,
            solar_wh=slot.solar_wh,
            reserve_wh=limits.reserve_wh,
            max_wh=limits.max_wh,
            max_charge_wh=limits.max_charge_wh,
            max_discharge_wh=limits.max_discharge_wh,
        )

    def _soc_entering(
        self, slots: list["_Slot"], index: int, start_wh: float, limits: "_Limits"
    ) -> float:
        """SoC entering ``slots[index]`` after walking every earlier slot through :meth:`_step_slot`."""
        soc_wh = start_wh
        for position in range(index):
            soc_wh = self._step_slot(soc_wh, slots[position], limits)
        return soc_wh

    def plan(self, planning_input: PlanningInput) -> EnergyPlan:
        """Plan grid charging for the horizon.

        Strict priority: hard limits, then the reserve along the whole trajectory, then the policy's
        target/deadline behaviour, then the preferred power range, then reaching a high SoC as late as
        permitted, then few power changes, then price. Reserve and target charge are both placed as late
        as the reserve and the limits permit; price never moves charge into an earlier slot.
        """
        now = planning_input.now
        battery = planning_input.battery
        slots = self._build_grid(now, planning_input.horizon)
        self._fill_forecasts(
            slots, planning_input.consumption_forecast, planning_input.solar_forecast
        )
        self._fill_prices(slots, planning_input)
        capacity_wh = battery.usable_capacity_wh
        limits = self._limits(battery)
        start_wh = min(
            limits.max_wh, max(0.0, battery.soc_percent / 100.0 * capacity_wh)
        )
        envelope = self._envelope(battery)
        outcome = self._plan_charging(slots, start_wh, limits, envelope, capacity_wh)
        self._label(slots)
        plan_slots = self._merge(slots, battery, capacity_wh)
        return self._build_plan(
            planning_input, slots, plan_slots, outcome, envelope, limits, start_wh
        )

    def _limits(self, battery: BatteryState) -> _Limits:
        """The shared trajectory limits; the charge ceiling is the permitted (hard) power."""
        capacity_wh = battery.usable_capacity_wh
        return _Limits(
            reserve_wh=self._params.reserve_soc_percent / 100.0 * capacity_wh,
            max_wh=self._params.max_soc_percent / 100.0 * capacity_wh,
            max_charge_wh=battery.permitted_charge_power_w * _SLOT_HOURS,
            max_discharge_wh=battery.max_discharge_power_w * _SLOT_HOURS,
            min_command_wh=(self._envelope(battery).preferred_min_w or 0.0)
            * _SLOT_HOURS,
        )

    def _envelope(self, battery: BatteryState) -> ChargePowerEnvelope:
        """Permitted power (hard) and the confirmed preferred range clamped into it."""
        permitted = battery.permitted_charge_power_w
        low, high = (
            self._params.preferred_charge_power_min_w,
            self._params.preferred_charge_power_max_w,
        )
        if low is not None and high is not None:
            high = min(high, permitted)
            low = min(low, high)
        return ChargePowerEnvelope(
            static_charge_limit_w=battery.max_charge_power_w,
            live_charge_limit_w=battery.live_charge_limit_w,
            live_limit_available=battery.live_charge_limit_w is not None,
            permitted_charge_power_w=permitted,
            preferred_min_w=low,
            preferred_max_w=high,
        )

    @staticmethod
    def forecast_series(
        planning_input: PlanningInput,
    ) -> tuple[SlotSeries, SlotSeries, SlotSeries]:
        """Per-slot prices, solar and consumption exactly as the planner sees them (for replanning)."""
        planner = DynamicChargeOptimizer()
        slots = planner._build_grid(planning_input.now, planning_input.horizon)
        planner._fill_forecasts(
            slots, planning_input.consumption_forecast, planning_input.solar_forecast
        )
        planner._fill_prices(slots, planning_input)
        return (
            tuple((slot.start, slot.price) for slot in slots),
            tuple((slot.start, slot.solar_wh) for slot in slots),
            tuple((slot.start, slot.consumption_wh) for slot in slots),
        )

    @staticmethod
    def _build_grid(now: datetime, horizon: timedelta) -> list[_Slot]:
        """The quarter-hour grid over the horizon, starting at the next full quarter hour.

        ``now`` is floored to the quarter-hour that contains it; planning begins at the *following*
        boundary so the first slot is always a complete quarter hour. A partial first slot would be
        charged with a full quarter-hour of energy (constant :data:`_SLOT_HOURS`), producing a plan
        the battery cannot physically reach, so the already-elapsed remainder is dropped instead. The
        boundary is :func:`planning_start`, shared with the adapter's coverage anchoring.

        The horizon upper bound is END-bounded, the canonical rule shared with
        :func:`app.optimizer.align.build_grid`: only full 15-minute slots whose ``end`` does not
        exceed ``now + horizon`` are emitted, so the last slot never overruns the requested horizon
        and the two :class:`EnergyOptimizer` implementations are substitutable at the horizon edge.
        """
        first = planning_start(now)
        slots: list[_Slot] = []
        start = first
        horizon_end = now + horizon
        while start + SLOT <= horizon_end:
            slots.append(_Slot(start=start, end=start + SLOT))
            start += SLOT
        return slots

    @staticmethod
    def _overlap_energy(
        slot: _Slot, start: datetime, end: datetime, energy_wh: float
    ) -> float:
        """The share of a forecast interval's energy that falls inside ``slot`` (time-proportional)."""
        lo = max(slot.start, start)
        hi = min(slot.end, end)
        if hi <= lo:
            return 0.0
        interval_seconds = (end - start).total_seconds()
        if interval_seconds <= 0:
            return 0.0
        return energy_wh * ((hi - lo).total_seconds() / interval_seconds)

    def _fill_forecasts(
        self, slots: list[_Slot], consumption: ConsumptionForecast, solar: SolarForecast
    ) -> None:
        for slot in slots:
            slot.consumption_wh = sum(
                self._overlap_energy(
                    slot, interval.start, interval.end, interval.energy_wh
                )
                for interval in consumption.intervals
            )
            slot.solar_wh = sum(
                self._overlap_energy(
                    slot, interval.start, interval.end, interval.energy_wh
                )
                for interval in solar.intervals
            )

    @staticmethod
    def _fill_prices(slots: list[_Slot], planning_input: PlanningInput) -> None:
        for slot in slots:
            price = next(
                (
                    float(interval.price_eur_per_kwh)
                    for interval in planning_input.prices.intervals
                    if interval.start <= slot.start < interval.end
                ),
                None,
            )
            slot.price = price

    def _plan_charging(
        self,
        slots: list[_Slot],
        start_wh: float,
        limits: _Limits,
        envelope: ChargePowerEnvelope,
        capacity_wh: float,
    ) -> _Outcome:
        """Run the strictly ordered placement phases and record what stayed unmet.

        1. Reserve along the whole trajectory, inside the preferred range first and raised to the
           hard limit only when the reserve cannot be secured otherwise (both policies).
        2. The optional target SoC at the target time: ``gentle`` stays inside the preferred range,
           ``meet_deadline`` raises power up to the hard limit when that is not enough.
        Every placement is checked against a re-simulation of the complete trajectory.
        """
        params = self._params
        outcome = _Outcome()
        hard_cap = limits.max_charge_wh
        # The preferred range is unverified and soft: it caps the first attempt only, and no minimum
        # power is invented from it (a partial-power slot is allowed).
        pref_cap = (
            hard_cap
            if envelope.preferred_max_w is None
            else envelope.preferred_max_w * _SLOT_HOURS
        )
        can_raise = hard_cap > pref_cap + _ENERGY_EPSILON_WH

        self._cover_reserve(slots, start_wh, limits, pref_cap)
        if can_raise and self._simulate_deficits(slots, start_wh, limits):
            self._cover_reserve(slots, start_wh, limits, hard_cap)
            if any(slot.charge_wh > pref_cap + _ENERGY_EPSILON_WH for slot in slots):
                outcome.reasons.append(
                    LimitingReason(
                        LimitingReasonCode.PREFERRED_RANGE_EXCEEDED_FOR_RESERVE,
                        "charge power raised above the preferred range to secure the reserve",
                    )
                )

        if params.target_soc_percent is not None and params.target_time is not None:
            self._plan_target(
                slots, start_wh, limits, capacity_wh, pref_cap, can_raise, outcome
            )
            self._prune_redundant_reserve_charge(slots, start_wh, limits, capacity_wh)
        self._smooth_charge(
            slots, start_wh, limits, pref_cap if can_raise else hard_cap, capacity_wh
        )

        deficits = self._simulate_deficits(slots, start_wh, limits)
        outcome.reserve_shortfall_wh = sum(needed for _, needed in deficits)
        if deficits:
            outcome.reasons.append(
                LimitingReason(
                    LimitingReasonCode.RESERVE_NOT_SECURABLE,
                    f"{outcome.reserve_shortfall_wh:.0f} Wh of forecast demand cannot be covered above the reserve",
                )
            )
        extra_wh = sum(
            slot.floor_extra_wh for slot in slots if slot.charge_wh > _ENERGY_EPSILON_WH
        )
        if extra_wh > _ENERGY_EPSILON_WH:
            outcome.reasons.append(
                LimitingReason(
                    LimitingReasonCode.PREFERRED_MINIMUM_EXTRA_ENERGY,
                    f"{extra_wh:.0f} Wh charged beyond the need to respect the confirmed preferred minimum power",
                )
            )
        self._add_power_reasons(envelope, outcome)
        return outcome

    def _plan_target(
        self,
        slots: list[_Slot],
        start_wh: float,
        limits: _Limits,
        capacity_wh: float,
        pref_cap: float,
        can_raise: bool,
        outcome: _Outcome,
    ) -> None:
        """Charge as late as permitted to reach the target SoC at the target time, if it can be."""
        params = self._params
        assert params.target_soc_percent is not None and params.target_time is not None
        index = -1
        for position, slot in enumerate(slots):
            if slot.end <= params.target_time:
                index = position
        if slots and params.target_time > slots[-1].end:
            outcome.reasons.append(
                LimitingReason(
                    LimitingReasonCode.TARGET_BEYOND_HORIZON,
                    "target time lies beyond the planning horizon; the target is checked at the horizon end",
                )
            )
        target_wh = params.target_soc_percent / 100.0 * capacity_wh
        shortfall = self._cover_target(
            slots, start_wh, limits, pref_cap, index, target_wh
        )
        raised = False
        if (
            shortfall > _ENERGY_EPSILON_WH
            and params.policy is PlanPolicy.MEET_DEADLINE
            and can_raise
        ):
            before = any(
                slot.charge_wh > pref_cap + _ENERGY_EPSILON_WH for slot in slots
            )
            shortfall = self._cover_target(
                slots, start_wh, limits, limits.max_charge_wh, index, target_wh
            )
            raised = not before and any(
                slot.charge_wh > pref_cap + _ENERGY_EPSILON_WH for slot in slots
            )
        if raised:
            outcome.reasons.append(
                LimitingReason(
                    LimitingReasonCode.PREFERRED_RANGE_EXCEEDED_FOR_DEADLINE,
                    "charge power raised above the preferred range to reach the target in time",
                )
            )
        outcome.target_shortfall_wh = shortfall
        if shortfall <= _ENERGY_EPSILON_WH:
            return
        if target_wh > limits.max_wh + _ENERGY_EPSILON_WH:
            code, detail = (
                LimitingReasonCode.TARGET_ABOVE_MAX_SOC,
                "target SoC is above the maximum SoC",
            )
        elif params.policy is PlanPolicy.GENTLE and can_raise:
            code = LimitingReasonCode.TARGET_UNREACHABLE_IN_PREFERRED_RANGE
            detail = "target not reachable inside the preferred power range; policy meet_deadline may raise power"
        else:
            code = LimitingReasonCode.TARGET_UNREACHABLE_AT_HARD_LIMIT
            detail = "target not reachable under the currently known power limits"
        outcome.reasons.append(LimitingReason(code, detail))

    def _prune_redundant_reserve_charge(
        self, slots: list[_Slot], start_wh: float, limits: _Limits, capacity_wh: float
    ) -> None:
        """Drop reserve-only charge slots the target charge made unnecessary.

        The reserve is placed first (it outranks the target), so a later target charge can leave an
        earlier reserve charge redundant. A slot is removed, earliest first, only
        when a re-simulation shows neither the reserve nor the target got worse without it.
        """
        params = self._params
        assert params.target_soc_percent is not None and params.target_time is not None
        index = max(
            (i for i, slot in enumerate(slots) if slot.end <= params.target_time),
            default=-1,
        )
        target_wh = params.target_soc_percent / 100.0 * capacity_wh

        def state() -> tuple[float, float]:
            shortfall = max(
                0.0, target_wh - self._soc_entering(slots, index + 1, start_wh, limits)
            )
            return self._deficit_total(slots, start_wh, limits), shortfall

        candidates = [
            i for i, slot in enumerate(slots)
            if slot.for_reserve and not slot.for_target and slot.charge_wh > _ENERGY_EPSILON_WH
        ]  # fmt: skip
        candidates.sort()
        for i in candidates:
            deficit, shortfall = state()
            previous = slots[i].charge_wh
            slots[i].charge_wh = 0.0
            new_deficit, new_shortfall = state()
            if (
                new_deficit > deficit + _ENERGY_EPSILON_WH
                or new_shortfall > shortfall + _ENERGY_EPSILON_WH
            ):
                slots[i].charge_wh = previous

    def _smooth_charge(
        self,
        slots: list[_Slot],
        start_wh: float,
        limits: _Limits,
        bound_wh: float,
        capacity_wh: float,
    ) -> None:
        """Fold a charge slot into its later neighbour to save a power change, when that is harmless.

        Few power changes rank below the reserve, the policy and timing, so a move is kept only if a
        re-simulation of the whole trajectory shows ALL of: no new reserve deficit, no worse target,
        no energy lost (final SoC not lower), and nothing charged earlier (energy only moves later).
        The neighbour must stay within its bound and the hard limit. A small leftover slot that the
        neighbour cannot absorb stays, because it is needed for the planned energy.
        """
        params = self._params
        index = -1
        target_wh = 0.0
        if params.target_soc_percent is not None and params.target_time is not None:
            index = max(
                (i for i, slot in enumerate(slots) if slot.end <= params.target_time),
                default=-1,
            )
            target_wh = params.target_soc_percent / 100.0 * capacity_wh

        def state() -> tuple[float, float, float]:
            shortfall = (
                max(
                    0.0,
                    target_wh - self._soc_entering(slots, index + 1, start_wh, limits),
                )
                if index >= 0
                else 0.0
            )
            final = self._soc_entering(slots, len(slots), start_wh, limits)
            return self._deficit_total(slots, start_wh, limits), shortfall, final

        changed = True
        while changed:
            changed = False
            for i in range(len(slots) - 1):
                early, late = slots[i], slots[i + 1]
                if (
                    early.charge_wh <= _ENERGY_EPSILON_WH
                    or late.charge_wh <= _ENERGY_EPSILON_WH
                ):
                    continue
                if abs(early.charge_wh - late.charge_wh) <= _ENERGY_EPSILON_WH:
                    continue
                merged = early.charge_wh + late.charge_wh
                if (
                    merged
                    > min(limits.max_charge_wh, max(bound_wh, late.charge_wh)) + 1e-9
                ):
                    continue
                deficit, shortfall, final = state()
                before = (
                    early.charge_wh,
                    late.charge_wh,
                    early.for_reserve,
                    early.for_target,
                )
                early.charge_wh, late.charge_wh = 0.0, merged
                new_deficit, new_shortfall, new_final = state()
                ok = (
                    new_deficit <= deficit + _ENERGY_EPSILON_WH
                    and new_shortfall <= shortfall + _ENERGY_EPSILON_WH
                    and new_final >= final - _ENERGY_EPSILON_WH
                )
                if ok:
                    early.floor_extra_wh = 0.0
                    late.for_reserve = late.for_reserve or before[2]
                    late.for_target = late.for_target or before[3]
                    changed = True
                else:
                    early.charge_wh, late.charge_wh = before[0], before[1]

    def _add_power_reasons(
        self, envelope: ChargePowerEnvelope, outcome: _Outcome
    ) -> None:
        """Surface the power-limit facts the plan relied on, so none of them stays implicit."""
        params = self._params
        if not envelope.live_limit_available:
            outcome.reasons.append(
                LimitingReason(
                    LimitingReasonCode.LIVE_LIMIT_UNAVAILABLE,
                    f"no verified live charge limit; the static limit of {envelope.static_charge_limit_w:.0f} W "
                    "applies (a missing limit is not unlimited)",
                )
            )
        elif envelope.permitted_charge_power_w < envelope.static_charge_limit_w:
            outcome.reasons.append(
                LimitingReason(
                    LimitingReasonCode.LIVE_LIMIT_BELOW_STATIC,
                    f"verified live limit of {envelope.permitted_charge_power_w:.0f} W is below the static limit",
                )
            )
        if params.preferred_charge_power_max_w is None:
            outcome.reasons.append(
                LimitingReason(
                    LimitingReasonCode.PREFERRED_RANGE_NOT_CONFIRMED,
                    "no confirmed preferred charge power range; the whole permitted range is used",
                )
            )
        elif (
            envelope.preferred_max_w is not None
            and envelope.preferred_max_w < params.preferred_charge_power_max_w
        ):
            outcome.reasons.append(
                LimitingReason(
                    LimitingReasonCode.PREFERRED_RANGE_CLAMPED,
                    "the preferred range was reduced to the permitted charge power",
                )
            )

    def _cover_reserve(
        self, slots: list[_Slot], start_wh: float, limits: _Limits, cap_wh: float
    ) -> None:
        """Place needed grid-charge energy into the latest permitted slots before each deficit.

        First simulate the no-grid-charge SoC trajectory (PV surplus charges, deficit discharges but
        never below the reserve). Wherever the battery would hit the reserve floor the net energy
        the house still needs becomes a deficit at that slot. That deficit is then met by placing
        grid-charge energy into the latest priced slots that lie at or before it, bounded by the
        per-slot command ceiling ``cap_wh`` and by the max-SoC headroom so the plan can never
        overcharge.
        """
        # Iteratively resolve deficits: a charge placed for one deficit raises the SoC the later
        # simulation starts the next deficit from, so re-simulate until no new deficit appears or no
        # eligible cheap slot has headroom left.
        for _ in range(len(slots) + 1):
            deficits = self._simulate_deficits(slots, start_wh, limits)
            if not deficits:
                return
            progressed = False
            for deficit_index, needed_wh in deficits:
                placed = self._place_charge(
                    slots, deficit_index, needed_wh, start_wh, limits, cap_wh=cap_wh
                )
                progressed = progressed or placed > _ENERGY_EPSILON_WH
            if not progressed:
                return

    def _cover_target(
        self,
        slots: list[_Slot],
        start_wh: float,
        limits: _Limits,
        cap_wh: float,
        index: int,
        target_wh: float,
    ) -> float:
        """Fill the energy missing at slot ``index`` latest-slot-first; return what is still missing.

        The gain of every placement is read from a re-simulation of the complete trajectory, and a
        placement that does not raise the target-time SoC or that worsens the reserve is reverted.
        """

        def target_soc() -> float:
            return self._soc_entering(slots, index + 1, start_wh, limits)

        baseline = self._deficit_total(slots, start_wh, limits)

        def reserve_kept() -> bool:
            return (
                self._deficit_total(slots, start_wh, limits)
                <= baseline + _ENERGY_EPSILON_WH
            )

        for _ in range(len(slots) + 1):
            shortfall = target_wh - target_soc()
            if shortfall <= _ENERGY_EPSILON_WH:
                return 0.0
            placed = self._place_charge(
                slots,
                index,
                shortfall,
                start_wh,
                limits,
                cap_wh=cap_wh,
                objective=target_soc,
                guard=reserve_kept,
            )
            if placed <= _ENERGY_EPSILON_WH:
                break
        return max(0.0, target_wh - target_soc())

    def _deficit_total(
        self, slots: list[_Slot], start_wh: float, limits: _Limits
    ) -> float:
        return sum(
            needed for _, needed in self._simulate_deficits(slots, start_wh, limits)
        )

    def _simulate_deficits(
        self, slots: list[_Slot], start_wh: float, limits: _Limits
    ) -> list[tuple[int, float]]:
        """Walk the trajectory and report ``(slot_index, extra_wh_needed)`` wherever it hits reserve.

        A deficit is the household energy the battery cannot supply from its own reserve margin, i.e.
        the amount by which net demand would push SoC below the reserve. That is what grid charging
        placed earlier can repair, by raising the SoC the battery enters the slot with.

        The SoC itself is advanced by the single authoritative transition (:meth:`_step_slot`), so
        this walk cannot disagree with the merge projection or the placement walk. The transition
        decides how an explicit CHARGE slot behaves: the executor commands a *constant* battery
        charge power there (``app.dispatch.strategy``), so house load is served from the grid and
        never discharges the battery during a CHARGE slot — hence a CHARGE slot can never produce a
        reserve deficit and is skipped for deficit detection.

        For an AUTO/IDLE slot the demand charged against the reserve is bounded by the discharge-power
        ceiling (``max_discharge_wh``): load above that ceiling is served from the grid in reality and
        never touches the battery. Capping removes a spurious deficit (the planner no longer
        pre-charges energy the battery could not discharge in the window) and never hides a real one,
        since the capped demand is always at most the raw net demand.
        """
        soc_wh = start_wh
        deficits: list[tuple[int, float]] = []
        for index, slot in enumerate(slots):
            if slot.charge_wh > _ENERGY_EPSILON_WH:
                # Explicit CHARGE slot: constant commanded battery charge; house load is served from
                # the grid and does not discharge the pack. No reserve deficit can arise here.
                soc_wh = self._step_slot(soc_wh, slot, limits)
                continue
            net_wh = slot.consumption_wh - slot.solar_wh
            if net_wh >= 0.0:
                battery_demand_wh = min(net_wh, limits.max_discharge_wh)
                available = max(0.0, soc_wh - limits.reserve_wh)
                if battery_demand_wh > available + _ENERGY_EPSILON_WH:
                    deficits.append((index, battery_demand_wh - available))
            # Advance through the same transition the deficit was measured against.
            soc_wh = self._step_slot(soc_wh, slot, limits)
        return deficits

    def _place_charge(
        self,
        slots: list[_Slot],
        deficit_index: int,
        needed_wh: float,
        start_wh: float,
        limits: _Limits,
        *,
        cap_wh: float | None = None,
        objective: Callable[[], float] | None = None,
        guard: Callable[[], bool] | None = None,
    ) -> float:
        """Fill ``needed_wh`` of *additional stored* energy into the latest permitted slots at/before the deficit.

        ``needed_wh`` is a shortfall in stored energy, so a slot only repairs it by the energy it
        actually adds to the battery. Because a commanded CHARGE *replaces* the slot's AUTO/PV
        charging rather than adding to it (:meth:`_step_soc`), the benefit of commanding charge in a
        slot is strictly its **marginal** gain over what AUTO would already have stored:
        ``SoC(with the command) - SoC(AUTO)``. Only that difference reduces the remaining deficit.
        A slot whose forecast PV already drives the battery to its charge-power ceiling or the
        max-SoC headroom yields zero marginal gain — commanding charge there would only curtail free
        PV — so it is skipped. The commanded grid energy stored in ``slot.charge_wh`` is the full
        setpoint the executor would receive (PV replaced), which may exceed the marginal gain; the
        returned value is the marginal stored energy, i.e. the actual deficit repair.

        ``cap_wh`` is the per-slot command ceiling (the preferred maximum or the hard limit). Working
        backwards from the deficit, the latest permitted slot is used first so the SoC stays low as
        long as the limits allow; a cheaper earlier slot never wins over a later eligible slot
        (:meth:`_latest_first`). With ``objective`` the real gain is read from a re-simulation of the
        whole trajectory after each placement, and a placement without gain, or one ``guard``
        rejects, is reverted.
        """
        cap_wh = limits.max_charge_wh if cap_wh is None else cap_wh
        candidates = [
            index
            for index in range(deficit_index + 1)
            if slots[index].price is not None and slots[index].charge_wh < cap_wh
        ]
        candidates = self._latest_first(candidates)
        remaining = needed_wh
        placed_total = 0.0
        for index in candidates:
            if remaining <= _ENERGY_EPSILON_WH:
                break
            slot = slots[index]
            soc_before = self._soc_entering(slots, index, start_wh, limits)
            # Every quantity below is a real SoC read from the single transition (_step_soc): the
            # AUTO baseline, the SoC the charge already placed here reaches, and the SoC a full
            # commanded charge would reach. The marginal benefit of commanding charge is strictly
            # SoC(command) - SoC(AUTO), never a re-derived formula.
            auto_after = self._soc_with_command(slot, soc_before, 0.0, limits)
            current_after = self._soc_with_command(
                slot, soc_before, slot.charge_wh, limits
            )
            max_after = self._soc_with_command(slot, soc_before, cap_wh, limits)
            current_marginal = max(0.0, current_after - auto_after)
            available_marginal = max(0.0, (max_after - auto_after) - current_marginal)
            if available_marginal <= _ENERGY_EPSILON_WH:
                continue
            wanted = min(available_marginal, remaining)
            # Switching a slot from AUTO to CHARGE is a discontinuous mode change: in a CHARGE slot
            # the house is served from the grid, so even a tiny command first undoes the AUTO
            # discharge before adding net storage. The marginal benefit can therefore jump past
            # ``wanted`` at the first positive command. So search for the smallest executable command
            # whose ACTUAL _step_soc marginal reaches ``wanted`` and reduce ``remaining`` by the
            # realized marginal (which may exceed ``wanted``) — never by the command.
            command, realized, extra = self._command_for_marginal(
                slot, soc_before, auto_after, current_marginal, wanted, limits, cap_wh
            )
            if realized <= _ENERGY_EPSILON_WH:
                continue
            if objective is not None:
                previous = slot.charge_wh
                before = objective()
                slot.charge_wh = command
                realized = objective() - before
                if realized <= _ENERGY_EPSILON_WH or (
                    guard is not None and not guard()
                ):
                    slot.charge_wh = previous
                    continue
                slot.for_target = True
                slot.floor_extra_wh = extra
            else:
                slot.charge_wh = command
                slot.floor_extra_wh = extra
                slot.for_reserve = True
            remaining -= realized
            placed_total += realized
        return placed_total

    @staticmethod
    def _latest_first(candidates: list[int]) -> list[int]:
        """Place every later eligible slot before any earlier one."""
        return sorted(candidates, reverse=True)

    def _soc_with_command(
        self, slot: _Slot, soc_before: float, command_wh: float, limits: _Limits
    ) -> float:
        """SoC after ``slot`` when the plan commands ``command_wh`` of grid charge (0 == AUTO/IDLE).

        A single call into the authoritative :meth:`_step_soc`, so AUTO and commanded trajectories
        are measured with the exact transition the rest of the planner uses.
        """
        return self._step_soc(
            soc_before,
            charge_wh=command_wh,
            consumption_wh=slot.consumption_wh,
            solar_wh=slot.solar_wh,
            reserve_wh=limits.reserve_wh,
            max_wh=limits.max_wh,
            max_charge_wh=limits.max_charge_wh,
            max_discharge_wh=limits.max_discharge_wh,
        )

    def _command_for_marginal(
        self,
        slot: _Slot,
        soc_before: float,
        auto_after: float,
        current_marginal: float,
        wanted: float,
        limits: _Limits,
        cap_wh: float | None = None,
    ) -> tuple[float, float, float]:
        """Smallest executable command whose TRUE marginal gain over AUTO reaches ``wanted`` more.

        Returns ``(command_wh, realized_additional_marginal)``. In a CHARGE slot the executor
        commands a constant battery charge power and the house is served from the grid, so flipping a
        slot from AUTO to CHARGE both avoids whatever AUTO would have discharged
        (``discharge_recovered``) and stores the commanded intake on top. The marginal gain over AUTO
        is therefore ``discharge_recovered + min(command, ceiling, headroom)`` — monotonic in the
        command but with a jump of ``discharge_recovered`` at the first positive command. So the
        marginal cannot land exactly on ``wanted`` when the jump alone already overshoots it.

        We size the stored intake, not the command-to-marginal inverse, and then read the realized
        marginal back through :meth:`_step_soc` so the number that reduces the deficit is the real
        state difference, never the command. The intake stores the part of ``wanted`` the mode flip
        does not already recover; when the flip alone covers ``wanted`` the intake is sized to the
        deficit anyway so the command is a meaningful charge, and the realized (over-)gain is counted
        honestly. The command is clamped to the charge-power ceiling and max-SoC headroom.
        """
        cap_wh = limits.max_charge_wh if cap_wh is None else cap_wh
        ceiling_room = min(cap_wh, max(0.0, limits.max_wh - soc_before))
        # The command is recomputed for the whole slot (it overwrites any charge placed in an earlier
        # outer-loop pass), so it must reach the TOTAL target marginal: the gain already secured plus
        # the newly wanted gain.
        target_marginal = current_marginal + wanted
        # A commanded charge stores intake from ``soc_before``; AUTO already moved the SoC to
        # ``auto_after``. To reach ``target_marginal`` of stored energy ON TOP of AUTO the command
        # must store ``(auto_after - soc_before) + target_marginal`` from ``soc_before``. With PV
        # this is larger than the marginal (AUTO already banked some PV); with a net house load it is
        # smaller and can go non-positive, which only means the AUTO->CHARGE mode flip already
        # over-covers the target on its own (the house stops discharging the pack). In that case
        # command a charge sized to the target so the slot still carries a real, executable setpoint
        # rather than a near-zero token, and count the realized (over-)gain honestly.
        intake = (auto_after - soc_before) + target_marginal
        if intake <= _ENERGY_EPSILON_WH:
            intake = target_marginal
        command = max(0.0, min(intake, ceiling_room))
        extra = 0.0
        if 0.0 < command < limits.min_command_wh:
            # A confirmed preferred minimum: take the smallest permitted command at or above it. The
            # surplus over the need is reported, and no earlier slot is used because of it.
            extra = limits.min_command_wh - command
            command = min(limits.min_command_wh, cap_wh)
        realized_after = self._soc_with_command(slot, soc_before, command, limits)
        realized = max(0.0, (realized_after - auto_after) - current_marginal)
        return command, realized, extra

    def _label(self, slots: list[_Slot]) -> None:
        for slot in slots:
            if slot.charge_wh > _ENERGY_EPSILON_WH:
                slot.action = EnergyAction.CHARGE
                if slot.for_target and not slot.for_reserve:
                    slot.reason = "charge as late as permitted for target SoC"
                elif slot.price is not None and slot.price < 0:
                    slot.reason = "negative spot price, forecast deficit"
                else:
                    slot.reason = "forecast deficit, charged as late as permitted"
            elif slot.price is None:
                slot.action = EnergyAction.IDLE
                slot.reason = "no spot price for this slot"
            elif slot.solar_wh >= slot.consumption_wh:
                slot.action = EnergyAction.IDLE
                slot.reason = "forecast PV covers demand"
            else:
                slot.action = EnergyAction.IDLE
                slot.reason = "battery reserve covers demand"

    def _merge(
        self, slots: list[_Slot], battery: BatteryState, capacity_wh: float
    ) -> list[EnergyPlanSlot]:
        """Collapse adjacent slots with the same action/reason/target and emit plan slots.

        A CHARGE run carries the target SoC the plan reaches by the end of the run and the charge
        power it uses; an IDLE run carries neither. The target is clamped into the product SoC window
        so the EnergyPlanSlot invariants hold.

        Adjacent CHARGE slots are merged only when they share the same commanded charge *power* (as
        well as action and reason): the emitted slot reports exactly one power, so collapsing slots
        planned at different powers into a single run would misreport the optimizer decision — a
        high-power window would appear to extend over a lower-power one. Slots with different
        commanded power stay separate, each carrying its own truthful power. IDLE runs merge freely.

        A CHARGE run also reports its energy in the four separate domains. The charge efficiency is
        applied here exactly once: grid energy is the battery-terminal energy divided by it.
        """
        limits = self._limits(battery)
        efficiency = self._params.charge_efficiency.value
        merged: list[EnergyPlanSlot] = []
        index = 0
        running_wh = min(
            limits.max_wh, max(0.0, battery.soc_percent / 100.0 * capacity_wh)
        )
        while index < len(slots):
            run_start = index
            action = slots[index].action
            reason = slots[index].reason
            run_charge = slots[index].charge_wh
            while (
                index < len(slots)
                and slots[index].action == action
                and slots[index].reason == reason
                and (
                    action is not EnergyAction.CHARGE
                    or abs(slots[index].charge_wh - run_charge) <= _ENERGY_EPSILON_WH
                )
            ):
                index += 1
            run = slots[run_start:index]
            start = run[0].start
            end = run[-1].end
            # Advance the running SoC across the whole run with the same per-slot step the planner
            # used, so consumption and PV inside a CHARGE block count toward the reported target too.
            commanded_wh = stored_wh = 0.0
            for slot in run:
                before_wh = running_wh
                running_wh = self._step_slot(running_wh, slot, limits)
                if slot.charge_wh > _ENERGY_EPSILON_WH:
                    commanded_wh += slot.charge_wh
                    stored_wh += max(0.0, running_wh - before_wh)
            if action is EnergyAction.CHARGE:
                target = self._clamp_target(running_wh / capacity_wh * 100.0)
                # Every slot in a CHARGE run now carries the same commanded power (merge gate above),
                # so the reported power is that single truthful value, never a maximum across
                # differently-powered slots.
                peak_power = max(slot.charge_wh for slot in run) / _SLOT_HOURS
                merged.append(
                    EnergyPlanSlot(
                        start=start,
                        end=end,
                        action=EnergyAction.CHARGE,
                        target_soc_percent=target,
                        max_power_w=min(battery.permitted_charge_power_w, peak_power),
                        reason=reason,
                        energy=PlanEnergy(
                            commanded_charge_energy_wh=commanded_wh,
                            battery_terminal_energy_wh=stored_wh,
                            stored_energy_delta_wh=stored_wh,
                            grid_charge_energy_wh=stored_wh / efficiency,
                        ),
                    )
                )
            else:
                merged.append(
                    EnergyPlanSlot(
                        start=start,
                        end=end,
                        action=EnergyAction.IDLE,
                        target_soc_percent=None,
                        max_power_w=None,
                        reason=reason,
                    )
                )
        return merged

    def _efficiency_note(self) -> str:
        efficiency = self._params.charge_efficiency
        if efficiency.source is EfficiencySource.ASSUMED:
            return (
                f"{efficiency.value:.2f} is an explicit planning assumption, not a verified device property; "
                "used only for grid energy and cost, never for the SoC trajectory"
            )
        return (
            f"{efficiency.value:.2f} is a {efficiency.source.value} value; "
            "used only for grid energy and cost, never for the SoC trajectory"
        )

    def _build_plan(
        self,
        planning_input: PlanningInput,
        slots: list[_Slot],
        plan_slots: list[EnergyPlanSlot],
        outcome: _Outcome,
        envelope: ChargePowerEnvelope,
        limits: _Limits,
        start_wh: float,
    ) -> EnergyPlan:
        """Assemble the plan with its flags, energy totals, assumptions and replan basis."""
        now = planning_input.now
        capacity_wh = planning_input.battery.usable_capacity_wh
        params = self._params
        trajectory = [(now, start_wh / capacity_wh * 100.0)]
        planned: list[PlannedCharge] = []
        soc_wh = start_wh
        for slot in slots:
            before_wh = soc_wh
            soc_wh = self._step_slot(soc_wh, slot, limits)
            trajectory.append((slot.end, soc_wh / capacity_wh * 100.0))
            if slot.charge_wh > _ENERGY_EPSILON_WH:
                planned.append(
                    PlannedCharge(slot.start, slot.end, max(0.0, soc_wh - before_wh))
                )
        charge_slots = [slot for slot in plan_slots if slot.energy is not None]
        totals = PlanEnergy(
            commanded_charge_energy_wh=sum(
                slot.energy.commanded_charge_energy_wh for slot in charge_slots
            ),
            battery_terminal_energy_wh=sum(
                slot.energy.battery_terminal_energy_wh for slot in charge_slots
            ),
            stored_energy_delta_wh=sum(
                slot.energy.stored_energy_delta_wh for slot in charge_slots
            ),
            grid_charge_energy_wh=sum(
                slot.energy.grid_charge_energy_wh for slot in charge_slots
            ),
        )
        live = envelope.live_charge_limit_w
        assumptions = PlanAssumptions(
            charge_efficiency=params.charge_efficiency,
            power_reference=POWER_REFERENCE,
            power_reference_verified=False,
            live_limit_status=(
                f"verified live limit {live:.0f} W applies"
                if live is not None
                else "live limit not available and not faked; the static device limit applies (missing is not unlimited)"
            ),
            forecast_basis=planning_input.forecast_basis,
            battery_internal_losses=BATTERY_INTERNAL_LOSSES,
            charge_efficiency_note=self._efficiency_note(),
            preferred_range_status=PREFERRED_RANGE_STATUS,
        )
        prices, solar, consumption = (
            tuple((slot.start, slot.price) for slot in slots),
            tuple((slot.start, slot.solar_wh) for slot in slots),
            tuple((slot.start, slot.consumption_wh) for slot in slots),
        )
        return EnergyPlan(
            created_at=now,
            valid_until=now + PLAN_VALIDITY,
            slots=tuple(plan_slots),
            policy=params.policy,
            reserve_soc_percent=params.reserve_soc_percent,
            reserve_satisfied=outcome.reserve_shortfall_wh <= _ENERGY_EPSILON_WH,
            reserve_shortfall_wh=outcome.reserve_shortfall_wh,
            target_soc_percent=params.target_soc_percent,
            target_time=params.target_time,
            target_reachable=outcome.target_shortfall_wh <= _ENERGY_EPSILON_WH,
            unserved_target_energy_wh=outcome.target_shortfall_wh,
            power=envelope,
            energy=totals,
            assumptions=assumptions,
            limiting_reasons=tuple(outcome.reasons),
            basis=PlanBasis(
                soc_percent=start_wh / capacity_wh * 100.0,
                trajectory=tuple(trajectory),
                permitted_charge_power_w=envelope.permitted_charge_power_w,
                live_charge_limit_w=envelope.live_charge_limit_w,
                prices=prices,
                solar_wh=solar,
                consumption_wh=consumption,
                planned_charges=tuple(planned),
            ),
        )

    def _clamp_target(self, percent: float) -> float:
        return min(
            self._params.max_soc_percent, max(self._params.reserve_soc_percent, percent)
        )
