#!/usr/bin/env python3
#
# app/weather/timezone.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Provider-independent IANA timezone resolution from a geolocation.

The Weather tab fills the timezone automatically from the selected location, never from operator
input. A geocoding hit may already carry a valid IANA zone (Open-Meteo does); when it does not
(Photon, the default, never carries one) the zone MUST be derived server-side from the coordinates.
The derivation is correct, not a meridian heuristic: Open-Meteo's forecast endpoint resolves the
true IANA zone for any coordinate pair with ``timezone=auto`` and echoes it in the response's
``timezone`` field. Reusing the hardened :class:`JsonHttpClient` keeps the same security envelope
as the other weather calls (fixed HTTPS origin, same-origin-redirect refusal, request timeout,
response-size cap, capped retry budget, closed :class:`WeatherError` failures), so no new network
path or dependency is introduced. Callers run this blocking request off the event loop.
"""

import logging
from collections.abc import Callable

from app.weather.base import WeatherResponseError
from app.weather.geocoding import is_finite_number, optional_timezone
from app.weather.http_json import JsonHttpClient, TtlCache
from app.weather.models import OPEN_METEO_URL

log = logging.getLogger(__name__)

# A bounded, thread-safe cache: a site keeps its coordinates for a long time, so the same lat/lon
# pair resolves to the same zone repeatedly (preview, save, re-save). The key is the exact pair:
# rounding would let a neighbour across a timezone boundary reuse the wrong zone for up to 24 h.
_CACHE_MAX_ENTRIES = 128
_CACHE_TTL_SECONDS = 24 * 60 * 60


class TimezoneResolver:
    """Resolve a geolocation to its IANA timezone through Open-Meteo's keyfree forecast endpoint.

    ``resolve`` returns a validated IANA zone string, or raises a :class:`WeatherError` subclass on
    any failure. It never guesses or fabricates a zone: a response without a usable ``timezone`` is
    a :class:`WeatherResponseError`, so "could not determine" stays distinguishable from a success.
    """

    def __init__(
        self,
        sleep: Callable[[float], None] | None = None,
        *,
        cache: TtlCache | None = None,
    ) -> None:
        self._http = JsonHttpClient("Open-Meteo timezone lookup", sleep=sleep)
        self._cache = (
            cache
            if cache is not None
            else TtlCache(_CACHE_MAX_ENTRIES, _CACHE_TTL_SECONDS)
        )

    def resolve(self, latitude: float, longitude: float) -> str:
        """Return the IANA timezone for ``latitude``/``longitude`` or raise on failure.

        Coordinates are validated here as a guard even though callers pass validated config values;
        an out-of-range pair is a response error, never a request to the provider.
        """
        if not is_finite_number(latitude) or not is_finite_number(longitude):
            raise WeatherResponseError("Coordinates must be finite numbers")
        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            raise WeatherResponseError("Coordinates are out of range")
        key = (float(latitude), float(longitude))
        cached = self._cache.get(key)
        if cached is not None:
            return str(cached)
        payload = self._http.get_json(self._build_url(latitude, longitude))
        zone = optional_timezone(payload.get("timezone"))
        if zone is None:
            # A missing or unusable timezone is a hard failure, not a silent empty/wrong result.
            raise WeatherResponseError("Open-Meteo did not return a usable timezone")
        self._cache.put(key, zone)
        return zone

    def _build_url(self, latitude: float, longitude: float) -> str:
        """Build the HTTPS URL. ``timezone=auto`` makes the provider resolve the IANA zone; a tiny
        ``current`` selection keeps the request valid while the response stays a few hundred bytes.
        """
        params = [
            ("latitude", f"{latitude:.6f}"),
            ("longitude", f"{longitude:.6f}"),
            ("timezone", "auto"),
            ("current", "temperature_2m"),
            ("forecast_days", "1"),
        ]
        return self._http.build_url(OPEN_METEO_URL, params)
