#!/usr/bin/env python3
#
# app/spotmarket/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Spot-market price providers: a separate configuration domain next to the Energy Manager.

A provider is a pure data source. It fetches day-ahead prices from an external market service and
returns a vendor-neutral snapshot of :class:`~app.spotmarket.models.SpotPriceInterval` values. It
never touches the inverter, the dispatch layer or the :class:`~app.energy.manager.EnergyManager`: a
later tariff engine will read these prices and may call the Energy Manager, which stays the single
owner of every battery action. See the Energy Manager module docstring for that boundary.
"""

from app.spotmarket.base import (
    SpotMarketConnectionError,
    SpotMarketError,
    SpotMarketNotAvailableYet,
    SpotMarketResponseError,
    SpotPriceProvider,
)
from app.spotmarket.models import (
    FeeRegion,
    PriceUnit,
    SpotMarketConfig,
    SpotMarketProvider,
    SpotPriceInterval,
    SpotPriceSnapshot,
)

__all__ = [
    "FeeRegion",
    "PriceUnit",
    "SpotMarketConfig",
    "SpotMarketConnectionError",
    "SpotMarketError",
    "SpotMarketNotAvailableYet",
    "SpotMarketProvider",
    "SpotMarketResponseError",
    "SpotPriceInterval",
    "SpotPriceProvider",
    "SpotPriceSnapshot",
]
