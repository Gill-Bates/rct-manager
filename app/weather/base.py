#!/usr/bin/env python3
#
# app/weather/base.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The weather/solar provider port and its closed error hierarchy.

A provider is read-only: it fetches external irradiance and returns the neutral ``SolarForecast``
the optimizer defines. Like the spot-market port, every expected failure maps to a
:class:`WeatherError` subclass, so a dead or rate-limited Open-Meteo never raises an unexpected
exception into the caller and can never reach the inverter/dispatch lifecycle. A weather failure
has no effect on inverter control; keeping the failure modes closed here is what guarantees that.
"""

from datetime import datetime, timedelta
from typing import Protocol

from app.optimizer.models import SolarForecast
from app.weather.models import WeatherConfig


class WeatherError(Exception):
    """Base class for every weather/solar provider failure."""


class WeatherConnectionError(WeatherError):
    """The external API could not be reached (network error, timeout, DNS, refused)."""


class WeatherResponseError(WeatherError):
    """The external API answered, but the response was unusable (bad status, malformed or
    inconsistent payload). Forecast values are never guessed or interpolated to paper over such a
    response.
    """


class SolarForecastSource(Protocol):
    """What every solar-forecast source implements. Synchronous by design: external reads use
    blocking ``urllib`` and run off the event loop via a thread, matching the spot-market provider.
    """

    def fetch(
        self, config: WeatherConfig, now: datetime, horizon: timedelta
    ) -> SolarForecast:
        """Fetch a solar-production forecast for ``config`` covering ``horizon`` from ``now``.

        Returns the neutral :class:`SolarForecast` (timezone-aware UTC intervals, ``energy_wh``).
        Raises a :class:`WeatherError` subclass on any failure; it never returns partial or
        fabricated data.
        """
        ...

    def fetch_preview(self, config: WeatherConfig, now: datetime) -> SolarForecast:
        """Fetch complete local yesterday/today/tomorrow intervals for the admin preview.

        This read-only presentation window is intentionally separate from :meth:`fetch`, whose
        optimizer contract remains future-only.
        """
        ...
