#!/usr/bin/env python3
#
# app/weather/models.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Weather/solar configuration and the provider-owned endpoint metadata.

Two concerns live here, deliberately kept apart from the neutral forecast types in
:mod:`app.optimizer.models`:

* :class:`WeatherConfig` is the operator-editable configuration, validated at the admin trust
  boundary and persisted in the admin store under its own key (``weather_config``), not as
  environment variables.
* The provider owns its fixed trusted HTTPS endpoint (:data:`OPEN_METEO_URL`). There is no
  operator-editable URL to turn into an SSRF surface.

Geographic location is intentionally *not* defaulted to ``0/0``: unset coordinates stay ``None`` so
a half-finished configuration can be saved while disabled, but an enabled configuration must carry
every value a forecast needs. ``extra="forbid"`` rejects an unknown stored field rather than
silently dropping it.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.clock import is_valid_iana_zone

# Open-Meteo free Open-Access Forecast API. A fixed, trusted origin owned by the provider, never an
# operator-editable field: a free-form URL would be an SSRF target (loopback, RFC1918, link-local
# metadata, internal DNS). The provider holds the only copy. The free endpoint needs no API key and
# is intended for non-commercial use under CC BY 4.0 attribution; see docs/energy-manager.md.
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
# Fixed provider origin of the default location search: Photon over OpenStreetMap data, no API key
# and no registration. Never operator-editable, so it can never become an SSRF target.
PHOTON_SEARCH_URL = "https://photon.komoot.io/api"
# Fixed provider origin of the legacy/alternative Open-Meteo location search; never operator-editable
# (SSRF boundary).
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
# The irradiance variable the provider requests: total radiation on the tilted module plane, which
# already folds in tilt and azimuth, as the preceding-interval mean in W/m².
GTI_VARIABLE = "global_tilted_irradiance"
# Bound persisted location metadata.
MAX_LOCATION_NAME_LENGTH = 200
MAX_TIMEZONE_LENGTH = 64
# A conservative upper bound on how many 15-minute intervals one forecast may carry. A horizon of a
# few days at quarter-hour resolution is a few hundred; this leaves generous headroom.
MAX_INTERVALS = 2000


class WeatherProvider(StrEnum):
    """The solar-forecast source. Only Open-Meteo exists in this first step; the closed enum makes
    an unknown provider a validation error, not a silent fallback.
    """

    OPEN_METEO = "open_meteo"


class WeatherConfig(BaseModel):
    """Operator-editable PV/solar-forecast configuration, validated at the admin boundary.

    It expresses the site (coordinates, PV geometry and efficiency) and which provider, never a
    network target: the provider owns its fixed trusted endpoint. When ``enabled`` is false a
    partial first configuration may be stored; when ``enabled`` is true every value a forecast needs
    must be present and valid, enforced by :meth:`_require_complete_when_enabled`.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    provider: WeatherProvider = WeatherProvider.OPEN_METEO
    # Descriptive only; coordinates remain authoritative.
    location_name: str | None = Field(default=None, max_length=MAX_LOCATION_NAME_LENGTH)
    # Region and country of the chosen place, kept as its own structured value so the UI never has
    # to split ``location_name``. Absent in older stored configs.
    location_region: str | None = Field(default=None, max_length=MAX_LOCATION_NAME_LENGTH)
    # Coordinates stay None until set: 0/0 (Gulf of Guinea) is a real location, so it must never be
    # a silent working default. An enabled config requires both.
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    # Validated IANA zone for display and future local-time rendering; forecasts remain UTC.
    timezone: str | None = Field(default=None, max_length=MAX_TIMEZONE_LENGTH)
    # PV nameplate DC power in watt-peak (Wp). SI watt field, named with its unit so a later reader
    # cannot mistake it for kW.
    pv_peak_power_wp: float | None = Field(default=None, gt=0, le=10_000_000, allow_inf_nan=False)
    # Module/roof tilt from horizontal (0° flat, 90° vertical) and azimuth in the Open-Meteo
    # convention: 0°=south, -90°=east, +90°=west, ±180°=north.
    tilt_deg: float | None = Field(default=None, ge=0, le=90, allow_inf_nan=False)
    azimuth_deg: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    # Performance ratio / system factor: a finite factor in (0, 1] folding inverter, wiring, soiling
    # and temperature losses into one transparent multiplier (see docs/energy-manager.md).
    performance_ratio: float = Field(default=0.85, gt=0, le=1, allow_inf_nan=False)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        """Normalize an empty timezone and reject unknown IANA zone names."""
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        if not is_valid_iana_zone(trimmed):
            raise ValueError(f"unknown timezone: {value!r}")
        return trimmed

    @model_validator(mode="after")
    def _require_complete_when_enabled(self) -> "WeatherConfig":
        if not self.enabled:
            return self
        missing = [
            name
            for name in ("latitude", "longitude", "pv_peak_power_wp", "tilt_deg", "azimuth_deg")
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(
                "an enabled solar forecast requires " + ", ".join(sorted(missing))
            )
        return self

    def is_forecastable(self) -> bool:
        """True when the config is enabled and carries every value a forecast needs.

        ``_require_complete_when_enabled`` already guarantees this for a validated enabled config;
        the explicit check keeps the cache/provider independent of that invariant.
        """
        return self.enabled and None not in (
            self.latitude, self.longitude, self.pv_peak_power_wp, self.tilt_deg, self.azimuth_deg,
        )
