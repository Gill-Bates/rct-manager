#!/usr/bin/env python3
#
# app/optimizer/replan.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Replan trigger detection for the Dry-Run plan: a pure function and a documented contract.

A plan is only valid for the situation it was computed from. This module decides, from the plan's
:class:`~app.optimizer.models.PlanBasis` and a fresh :class:`ReplanObservation`, whether that
situation has changed enough to require a new plan. It never plans, executes or touches hardware;
there is deliberately no automatic executor behind it.

Triggers (each has a threshold in :class:`ReplanThresholds`; insignificant changes do not fire):

* ``soc_deviation``: the measured SoC differs from the planned trajectory at ``now``.
* ``charge_limit_changed``: the verified charge limit differs from the one planned with. A reduction
  matters most: the executor always applies the lower verified live limit, so a plan that assumed
  more power can arrive late unnoticed (``meet_deadline`` in particular).
* ``price_forecast_changed`` / ``solar_forecast_changed`` / ``consumption_forecast_changed``: the
  forecast moved by more than its tolerance over the slots still ahead.
* ``missed_charge_slot``: a planned charge slot has ended and delivered clearly less than planned.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from itertools import pairwise

from app.clock import require_aware
from app.optimizer.models import PlanBasis, SlotSeries


class ReplanTriggerCode(StrEnum):
    SOC_DEVIATION = "soc_deviation"
    CHARGE_LIMIT_CHANGED = "charge_limit_changed"
    PRICE_FORECAST_CHANGED = "price_forecast_changed"
    SOLAR_FORECAST_CHANGED = "solar_forecast_changed"
    CONSUMPTION_FORECAST_CHANGED = "consumption_forecast_changed"
    MISSED_CHARGE_SLOT = "missed_charge_slot"


TRIGGER_DESCRIPTIONS: dict[ReplanTriggerCode, str] = {
    ReplanTriggerCode.SOC_DEVIATION: "Measured SoC deviates from the planned trajectory",
    ReplanTriggerCode.CHARGE_LIMIT_CHANGED: "Verified charge power limit changed",
    ReplanTriggerCode.PRICE_FORECAST_CHANGED: "Price forecast changed",
    ReplanTriggerCode.SOLAR_FORECAST_CHANGED: "PV forecast changed",
    ReplanTriggerCode.CONSUMPTION_FORECAST_CHANGED: "Consumption forecast changed",
    ReplanTriggerCode.MISSED_CHARGE_SLOT: "A planned charge slot was not carried out as planned",
}


@dataclass(frozen=True, slots=True)
class ReplanThresholds:
    """Tolerances below which a change is insignificant."""

    soc_deviation_percent: float = 5.0
    charge_limit_change_w: float = 50.0
    price_change_eur_per_kwh: float = 0.01
    forecast_change_fraction: float = 0.10
    forecast_change_floor_wh: float = 250.0
    missed_slot_fraction: float = 0.5


DEFAULT_REPLAN_THRESHOLDS = ReplanThresholds()


@dataclass(frozen=True, slots=True)
class ReplanObservation:
    """The current situation, in the same terms the plan's basis uses.

    ``permitted_charge_power_w`` is ``min(static limit, verified fresh live limit)`` now.
    ``executed_charge_wh`` maps a planned charge slot's start to the battery-side energy actually
    delivered in it; a slot missing from the mapping counts as nothing delivered.
    """

    now: datetime
    soc_percent: float
    permitted_charge_power_w: float
    prices: SlotSeries
    solar_wh: SlotSeries
    consumption_wh: SlotSeries
    executed_charge_wh: Mapping[datetime, float]

    def __post_init__(self) -> None:
        require_aware(self.now, "now")


@dataclass(frozen=True, slots=True)
class ReplanTrigger:
    code: ReplanTriggerCode
    detail: str


@dataclass(frozen=True, slots=True)
class ReplanDecision:
    triggers: tuple[ReplanTrigger, ...]

    @property
    def replan_required(self) -> bool:
        return bool(self.triggers)


def expected_soc_percent(basis: PlanBasis, now: datetime) -> float:
    """The planned SoC at ``now``, linearly interpolated along the plan's trajectory."""
    points = basis.trajectory
    if now <= points[0][0]:
        return points[0][1]
    for (t0, s0), (t1, s1) in pairwise(points):
        if now <= t1:
            fraction = (now - t0).total_seconds() / (t1 - t0).total_seconds()
            return s0 + (s1 - s0) * fraction
    return points[-1][1]


def _ahead(series: SlotSeries, now: datetime) -> dict[datetime, float | None]:
    return {start: value for start, value in series if start >= now}


def _slots_changed(old: dict[datetime, float | None], new: dict[datetime, float | None]) -> bool:
    """Whether the set of relevant future slots differs: a lost slot, or a new one inside the plan's span."""
    if not old:
        return False
    last = max(old)
    return old.keys() != {key for key in new if key <= last}


def _forecast_moved(planned: SlotSeries, observed: SlotSeries, now: datetime, thresholds: ReplanThresholds) -> bool:
    old, new = _ahead(planned, now), _ahead(observed, now)
    if _slots_changed(old, new):
        return True
    shared = old.keys() & new.keys()
    delta = sum(abs((new[key] or 0.0) - (old[key] or 0.0)) for key in shared)
    total = sum(old[key] or 0.0 for key in shared)
    return delta > max(thresholds.forecast_change_floor_wh, thresholds.forecast_change_fraction * total)


def _prices_moved(planned: SlotSeries, observed: SlotSeries, now: datetime, thresholds: ReplanThresholds) -> bool:
    old, new = _ahead(planned, now), _ahead(observed, now)
    if _slots_changed(old, new):
        return True
    for key in old.keys() & new.keys():
        before, after = old[key], new[key]
        if (before is None) != (after is None):
            return True
        if before is not None and after is not None and abs(after - before) > thresholds.price_change_eur_per_kwh:
            return True
    return False


def evaluate_replan(
    basis: PlanBasis, observation: ReplanObservation, thresholds: ReplanThresholds = DEFAULT_REPLAN_THRESHOLDS
) -> ReplanDecision:
    """Decide whether the plan behind ``basis`` must be recomputed. Pure: no I/O, no side effects."""
    now = observation.now
    triggers: list[ReplanTrigger] = []

    deviation = abs(observation.soc_percent - expected_soc_percent(basis, now))
    if deviation > thresholds.soc_deviation_percent:
        triggers.append(
            ReplanTrigger(ReplanTriggerCode.SOC_DEVIATION, f"SoC is {deviation:.1f} percentage points off the plan")
        )

    change = observation.permitted_charge_power_w - basis.permitted_charge_power_w
    if abs(change) > thresholds.charge_limit_change_w:
        direction = "reduced" if change < 0 else "raised"
        triggers.append(
            ReplanTrigger(
                ReplanTriggerCode.CHARGE_LIMIT_CHANGED,
                f"verified charge limit {direction} from {basis.permitted_charge_power_w:.0f} W "
                f"to {observation.permitted_charge_power_w:.0f} W",
            )
        )

    if _prices_moved(basis.prices, observation.prices, now, thresholds):
        triggers.append(ReplanTrigger(ReplanTriggerCode.PRICE_FORECAST_CHANGED, "price forecast moved"))
    if _forecast_moved(basis.solar_wh, observation.solar_wh, now, thresholds):
        triggers.append(ReplanTrigger(ReplanTriggerCode.SOLAR_FORECAST_CHANGED, "PV forecast moved"))
    if _forecast_moved(basis.consumption_wh, observation.consumption_wh, now, thresholds):
        triggers.append(ReplanTrigger(ReplanTriggerCode.CONSUMPTION_FORECAST_CHANGED, "consumption forecast moved"))

    missed = [
        charge
        for charge in basis.planned_charges
        if charge.end <= now
        and observation.executed_charge_wh.get(charge.start, 0.0)
        < thresholds.missed_slot_fraction * charge.terminal_energy_wh
    ]
    if missed:
        triggers.append(
            ReplanTrigger(
                ReplanTriggerCode.MISSED_CHARGE_SLOT,
                f"{len(missed)} planned charge slot(s) delivered less than planned",
            )
        )
    return ReplanDecision(tuple(triggers))
