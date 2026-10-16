#!/usr/bin/env python3
#
# app/optimizer/greedy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""A deterministic, pure greedy energy optimizer.

:class:`GreedyEnergyOptimizer` turns a complete :class:`PlanningInput` snapshot into an
:class:`EnergyPlan` of reasoned, non-executing slots. It is a pure function of its input: it reads
no clock other than ``planning_input.now``, performs no I/O, and never touches hardware.

The optimizer resamples the three series onto one common 15-minute UTC grid (see
:mod:`app.optimizer.align`) and then sweeps the grid once, forward in time, choosing exactly one
action per slot under a fixed total branch precedence (first match wins):

1. **PV surplus** (``residual_wh < 0`` and battery below the target-window top) -> ``CHARGE``.
2. **Cheap grid window** (slot price ``<=`` the cheap threshold and battery below the top) ->
   ``CHARGE`` from grid.
3. **Expensive grid window** (slot price ``>=`` the expensive threshold and SoC above the reserve
   floor) -> ``DISCHARGE`` to load.
4. Otherwise ``HOLD`` (price neither cheap nor expensive, no actionable surplus), or ``IDLE`` when
   the slot has no price signal at all (a price gap).

Because branch 1 outranks 2 and 2 outranks 3, overlapping conditions and a degenerate flat price
curve always resolve to exactly one branch. Charging fills as early as safely possible and
discharging covers load as late as possible, so equal prices never produce order-dependent output.
``EXPORT`` is never emitted; ``AUTO`` is reserved for the handback path and is not a planned slot.

SoC bounds come only from :data:`TARGET_SOC_MIN_PERCENT` / :data:`TARGET_SOC_MAX_PERCENT` (the same
constants and the same ``max(7, ...)`` / ``min(97, ...)`` form as ``EnergyManager._window()``)
intersected with the operator ``DispatchConfig`` window. Power comes only from
``min(battery limit, MAX_POWER_W)`` — no charge ceiling is ever synthesized from a hardware
register. Every emitted ``CHARGE`` / ``DISCHARGE`` slot carries a concrete in-window
``target_soc_percent`` and a positive-finite ``max_power_w`` (never ``None``); ``HOLD`` / ``IDLE``
carry both as ``None``.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.limits import MAX_POWER_W, TARGET_SOC_MAX_PERCENT, TARGET_SOC_MIN_PERCENT
from app.optimizer.align import align_energy, align_prices, build_grid
from app.optimizer.models import (
    REASON_MAX_LENGTH,
    EnergyAction,
    EnergyPlan,
    EnergyPlanSlot,
    PlanningInput,
)

# Prices are rendered to four decimal places of EUR/kWh in reasons for a stable, bounded string.
_PRICE_QUANT = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class _SocWindow:
    """The effective target-SoC window, in percent."""

    low: float
    high: float


def _reason(text: str) -> str:
    """Return a bounded, ASCII-only reason, truncated defensively to ``REASON_MAX_LENGTH``.

    The templates in this module are short by construction; the truncation and the ASCII encode are
    a defensive guarantee so a pathological number can never produce a reason the dataclass rejects.
    """
    ascii_text = text.encode("ascii", "replace").decode("ascii")
    return ascii_text[:REASON_MAX_LENGTH]


def _format_price(price: Decimal) -> str:
    """Render a EUR/kWh price with fixed 4-decimal precision, ASCII-only."""
    return str(price.quantize(_PRICE_QUANT, rounding=ROUND_HALF_UP))


def _percentile(sorted_values: list[Decimal], percentile: float) -> Decimal:
    """Linear-interpolated percentile over a non-empty, ascending ``sorted_values`` list.

    ``percentile`` is in ``0..100``. The computation is deterministic and uses :class:`Decimal`
    arithmetic throughout so a tie against the threshold is exact, not float-fuzzy.
    """
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = Decimal(str(percentile)) / Decimal(100) * Decimal(len(sorted_values) - 1)
    lower_index = int(rank)
    if lower_index >= len(sorted_values) - 1:
        return sorted_values[-1]
    fraction = rank - Decimal(lower_index)
    lower = sorted_values[lower_index]
    upper = sorted_values[lower_index + 1]
    return lower + (upper - lower) * fraction


class GreedyEnergyOptimizer:
    """A pure, deterministic greedy :class:`EnergyOptimizer`.

    ``cheap_percentile`` / ``expensive_percentile`` (0..100, cheap < expensive, as Settings already
    validates) select the two price thresholds from the horizon's own price distribution.
    ``min_soc`` / ``max_soc`` are the operator ``DispatchConfig`` window (default 5/95); the
    effective window is ``[max(7, min_soc), min(97, max_soc)]``.
    """

    def __init__(
        self,
        *,
        cheap_percentile: float,
        expensive_percentile: float,
        min_soc: float,
        max_soc: float,
    ) -> None:
        if not 0 <= cheap_percentile <= 100 or not 0 <= expensive_percentile <= 100:
            raise ValueError("percentiles must be between 0 and 100")
        if cheap_percentile >= expensive_percentile:
            raise ValueError("cheap_percentile must be smaller than expensive_percentile")
        self._cheap_percentile = cheap_percentile
        self._expensive_percentile = expensive_percentile
        # Same literal form as EnergyManager._window(), so a change to one site diverges visibly
        # from the other instead of silently.
        self._window = _SocWindow(
            low=max(TARGET_SOC_MIN_PERCENT, min_soc),
            high=min(TARGET_SOC_MAX_PERCENT, max_soc),
        )
        if self._window.low >= self._window.high:
            raise ValueError("the effective SoC window is empty")

    def plan(self, planning_input: PlanningInput) -> EnergyPlan:
        """Build an :class:`EnergyPlan` from a complete snapshot. Pure; reads only the given ``now``."""
        if not isinstance(planning_input, PlanningInput):
            raise ValueError("planning_input must be a PlanningInput")
        now = planning_input.now
        battery = planning_input.battery
        grid = build_grid(now, planning_input.horizon)
        if not grid:
            return EnergyPlan(
                created_at=now,
                valid_until=now + planning_input.horizon,
                slots=(),
            )

        prices = align_prices(planning_input.prices, grid)
        solar = align_energy(planning_input.solar_forecast, grid)
        consumption = align_energy(planning_input.consumption_forecast, grid)

        cheap, expensive = self._thresholds(prices)
        max_charge_w = min(battery.max_charge_power_w, MAX_POWER_W)
        max_discharge_w = min(battery.max_discharge_power_w, MAX_POWER_W)
        charge_target = _round_percent(self._window.high)
        discharge_target = _round_percent(self._window.low)

        # soc tracks the planned trajectory so the branch guards (below top / above floor) and the
        # per-slot target reflect the running plan, not just the measured start SoC.
        soc = battery.soc_percent
        capacity_wh = battery.usable_capacity_wh
        slot_hours = (grid[0][1] - grid[0][0]).total_seconds() / 3600.0

        slots: list[EnergyPlanSlot] = []
        for index, (slot_start, slot_end) in enumerate(grid):
            residual_wh = consumption[index] - solar[index]
            price = prices[index]
            action, target, power, soc = self._decide(
                residual_wh=residual_wh,
                price=price,
                cheap=cheap,
                expensive=expensive,
                soc=soc,
                capacity_wh=capacity_wh,
                slot_hours=slot_hours,
                max_charge_w=max_charge_w,
                max_discharge_w=max_discharge_w,
                charge_target=charge_target,
                discharge_target=discharge_target,
            )
            slots.append(
                EnergyPlanSlot(
                    start=slot_start,
                    end=slot_end,
                    action=action,
                    target_soc_percent=target,
                    max_power_w=power,
                    reason=self._reason_for(action, residual_wh, price, cheap, expensive),
                )
            )

        self._assert_post_condition(slots)
        return EnergyPlan(created_at=now, valid_until=now + planning_input.horizon, slots=tuple(slots))

    def _thresholds(self, prices: tuple[Decimal | None, ...]) -> tuple[Decimal | None, Decimal | None]:
        known = sorted(price for price in prices if price is not None)
        if not known:
            return None, None
        return (
            _percentile(known, self._cheap_percentile),
            _percentile(known, self._expensive_percentile),
        )

    def _decide(
        self,
        *,
        residual_wh: float,
        price: Decimal | None,
        cheap: Decimal | None,
        expensive: Decimal | None,
        soc: float,
        capacity_wh: float,
        slot_hours: float,
        max_charge_w: float,
        max_discharge_w: float,
        charge_target: float,
        discharge_target: float,
    ) -> tuple[EnergyAction, float | None, float | None, float]:
        """Return ``(action, target_soc, max_power_w, next_soc)`` for one slot under the fixed order."""
        below_top = soc < self._window.high
        above_floor = soc > self._window.low

        # Branch 1: PV surplus (solar exceeds load) and there is headroom to store it.
        if residual_wh < 0 and below_top:
            next_soc = _advance_soc(soc, max_charge_w, slot_hours, capacity_wh, self._window.high, charging=True)
            return EnergyAction.CHARGE, charge_target, max_charge_w, next_soc

        # Branch 2: cheap grid window and headroom to charge.
        if price is not None and cheap is not None and price <= cheap and below_top:
            next_soc = _advance_soc(soc, max_charge_w, slot_hours, capacity_wh, self._window.high, charging=True)
            return EnergyAction.CHARGE, charge_target, max_charge_w, next_soc

        # Branch 3: expensive grid window and energy above the reserve floor to spend. A degenerate
        # (flat) price curve collapses cheap==expensive and carries no genuine expensive window, so
        # a flat slot that is already full falls through to HOLD rather than discharging — the
        # flat-curve determinism the design pins (cheap-charge-then-HOLD, never both on a slot).
        if (
            price is not None
            and expensive is not None
            and cheap is not None
            and cheap < expensive
            and price >= expensive
            and above_floor
        ):
            next_soc = _advance_soc(soc, max_discharge_w, slot_hours, capacity_wh, self._window.low, charging=False)
            return EnergyAction.DISCHARGE, discharge_target, max_discharge_w, next_soc

        # Branch 4: no actionable price/surplus. IDLE on a price gap, otherwise HOLD.
        action = EnergyAction.IDLE if price is None else EnergyAction.HOLD
        return action, None, None, soc

    def _reason_for(
        self,
        action: EnergyAction,
        residual_wh: float,
        price: Decimal | None,
        cheap: Decimal | None,
        expensive: Decimal | None,
    ) -> str:
        if action is EnergyAction.CHARGE and residual_wh < 0:
            return _reason(f"PV surplus {round(-residual_wh)} Wh -> charge battery")
        if action is EnergyAction.CHARGE:
            return _reason(f"spot {_format_price(price)} <= cheap {_format_price(cheap)} -> charge from grid")
        if action is EnergyAction.DISCHARGE:
            return _reason(
                f"spot {_format_price(price)} >= expensive {_format_price(expensive)} -> discharge to load"
            )
        if action is EnergyAction.IDLE:
            return _reason("no price for interval -> idle")
        if price is not None:
            return _reason(f"spot {_format_price(price)} in mid band -> hold")
        return _reason("no actionable signal -> hold")

    @staticmethod
    def _assert_post_condition(slots: list[EnergyPlanSlot]) -> None:
        for slot in slots:
            if slot.action in {EnergyAction.CHARGE, EnergyAction.DISCHARGE}:
                # The dataclass does not catch a None max_power_w on charge/discharge; the optimizer
                # must. target_soc_percent presence/range is already guaranteed by the dataclass.
                assert slot.max_power_w is not None, "charge/discharge slot must carry max_power_w"
                assert TARGET_SOC_MIN_PERCENT <= slot.target_soc_percent <= TARGET_SOC_MAX_PERCENT


def _round_percent(value: float) -> float:
    """Clamp a percent into the hard product window and round to a stable 0.1% grid."""
    bounded = min(max(value, TARGET_SOC_MIN_PERCENT), TARGET_SOC_MAX_PERCENT)
    return round(bounded, 1)


def _advance_soc(
    soc: float,
    power_w: float,
    slot_hours: float,
    capacity_wh: float,
    limit_percent: float,
    *,
    charging: bool,
) -> float:
    """Advance the planned SoC by one slot, clamped at the window edge.

    This is a planning estimate used only to keep the branch guards and the running trajectory
    consistent; the real device SoC is re-read every recompute. ``power_w`` is the slot power and
    ``capacity_wh`` the operator-declared usable capacity.
    """
    delta_percent = (power_w * slot_hours) / capacity_wh * 100.0 if capacity_wh > 0 else 0.0
    if charging:
        return min(limit_percent, soc + delta_percent)
    return max(limit_percent, soc - delta_percent)
