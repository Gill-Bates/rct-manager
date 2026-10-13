#!/usr/bin/env python3
#
# app/optimizer/base.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pure provider and planning contracts for the optimizer boundary."""

from datetime import datetime, timedelta
from typing import Protocol

from app.optimizer.models import (
    BatteryState,
    ConsumptionForecast,
    EnergyPlan,
    EnergyPlanSlot,
    PlanningInput,
    SolarForecast,
)


class SolarForecastProvider(Protocol):
    """Supplies a solar-production forecast; concrete retrieval belongs to a later package."""

    def forecast(self, now: datetime, horizon: timedelta) -> SolarForecast: ...


class ConsumptionForecastProvider(Protocol):
    """Supplies a household-consumption forecast; concrete retrieval belongs to a later package."""

    def forecast(self, now: datetime, horizon: timedelta) -> ConsumptionForecast: ...


class BatteryStateProvider(Protocol):
    """Supplies the current measured battery state; it never predicts or commands hardware."""

    def current(self) -> BatteryState: ...


class EnergyOptimizer(Protocol):
    """Builds a plan from a complete input snapshot; Package A supplies no implementation."""

    def plan(self, planning_input: PlanningInput) -> EnergyPlan: ...


class PlanScheduler(Protocol):
    """Stores and looks up plans only; it never executes their slots."""

    def set_plan(self, plan: EnergyPlan) -> None: ...

    def active_slot(self, now: datetime) -> EnergyPlanSlot | None: ...

    def current_plan(self) -> EnergyPlan | None: ...
