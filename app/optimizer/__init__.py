#!/usr/bin/env python3
#
# app/optimizer/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pure energy-planning contracts.

This package defines validated planning inputs and plans, but deliberately contains no optimizer
implementation and cannot command a battery. The Energy Manager remains the only executor.
"""

from app.optimizer.base import (
    BatteryStateProvider,
    ConsumptionForecastProvider,
    EnergyOptimizer,
    PlanScheduler,
    SolarForecastProvider,
)
from app.optimizer.models import (
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
from app.optimizer.scheduler import InMemoryPlanScheduler

__all__ = [
    "BatteryState",
    "BatteryStateProvider",
    "ConsumptionForecast",
    "ConsumptionForecastInterval",
    "ConsumptionForecastProvider",
    "EnergyAction",
    "EnergyOptimizer",
    "EnergyPlan",
    "EnergyPlanSlot",
    "InMemoryPlanScheduler",
    "PlanScheduler",
    "PlanningInput",
    "SolarForecast",
    "SolarForecastInterval",
    "SolarForecastProvider",
]
