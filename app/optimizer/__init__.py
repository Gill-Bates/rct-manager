#!/usr/bin/env python3
#
# app/optimizer/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pure energy-planning contracts.

This package defines validated planning inputs and plans, but deliberately contains no optimizer
implementation and cannot command a battery. The Energy Manager remains the only executor.
"""

from app.optimizer.align import align_energy, align_prices, build_grid
from app.optimizer.base import (
    BatteryStateProvider,
    ConsumptionForecastProvider,
    EnergyOptimizer,
    PlanScheduler,
    SolarForecastProvider,
)
from app.optimizer.consumption import BaselineConsumptionForecastProvider
from app.optimizer.greedy import GreedyEnergyOptimizer
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
from app.optimizer.planner import DynamicChargeOptimizer, PlanParameters
from app.optimizer.scheduler import InMemoryPlanScheduler
from app.optimizer.service import (
    DataAge,
    PlanningService,
    PlanView,
    ReadingsBatteryStateProvider,
)

__all__ = [
    "BaselineConsumptionForecastProvider",
    "BatteryState",
    "BatteryStateProvider",
    "ConsumptionForecast",
    "ConsumptionForecastInterval",
    "ConsumptionForecastProvider",
    "DataAge",
    "DynamicChargeOptimizer",
    "EnergyAction",
    "EnergyOptimizer",
    "EnergyPlan",
    "EnergyPlanSlot",
    "GreedyEnergyOptimizer",
    "InMemoryPlanScheduler",
    "PlanParameters",
    "PlanScheduler",
    "PlanView",
    "PlanningInput",
    "PlanningService",
    "ReadingsBatteryStateProvider",
    "SolarForecast",
    "SolarForecastInterval",
    "SolarForecastProvider",
    "align_energy",
    "align_prices",
    "build_grid",
]
