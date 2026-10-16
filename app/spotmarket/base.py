#!/usr/bin/env python3
#
# app/spotmarket/base.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The spot-price provider port and its error hierarchy.

A provider is read-only: it fetches external day-ahead prices and returns a neutral snapshot. It is
never allowed to raise an unexpected exception into a caller — every failure path maps to one of the
:class:`SpotMarketError` subclasses, so a later tariff engine (and the admin preview endpoint) can
treat a dead external API as "no data", never as a crash. A spot-market failure must have no effect
on the inverter control path; keeping the failure modes closed here is what guarantees that.
"""

from typing import Protocol

from app.spotmarket.models import SpotMarketConfig, SpotPriceSnapshot


class SpotMarketError(Exception):
    """Base class for every spot-market provider failure."""


class SpotMarketConnectionError(SpotMarketError):
    """The external API could not be reached (network error, timeout, DNS, refused)."""


class SpotMarketResponseError(SpotMarketError):
    """The external API answered, but the response was unusable (bad status, malformed or
    inconsistent payload). Prices are never guessed or interpolated to paper over such a response.
    """


class SpotMarketNotAvailableYet(SpotMarketError):
    """The next-day auction has not published its price curve yet."""


class SpotPriceProvider(Protocol):
    """What every spot-price source implements. Synchronous by design: external reads use blocking
    ``urllib`` and run off the event loop via a thread, rather than introducing an async HTTP client.
    """

    def fetch_day_ahead(self, config: SpotMarketConfig) -> SpotPriceSnapshot:
        """Fetch day-ahead prices for the configured bidding zone.

        Returns a normalised snapshot (prices in EUR/kWh, timezone-aware timestamps). Raises
        :class:`SpotMarketNotAvailableYet` before publication, or a connection/response error on
        failure; it does not return partial or fabricated data.
        """
        ...

    def fetch_today(self, config: SpotMarketConfig) -> SpotPriceSnapshot:
        """Fetch the current local day's complete quarter-hour price curve."""
        ...

    def fetch_current(self, config: SpotMarketConfig) -> SpotPriceSnapshot:
        """Fetch the current interval and its validity from the dedicated endpoint."""
        ...

    def fetch_yesterday(self, config: SpotMarketConfig, timezone: str | None) -> SpotPriceSnapshot:
        """Fetch exactly the previous local day in the timezone returned by ``fetch_today``."""
        ...
