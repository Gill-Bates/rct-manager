#!/usr/bin/env python3
#
# app/weather/open_meteo.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Open-Meteo free Open-Access Forecast API provider for PV production forecasts.

The ``/v1/forecast`` endpoint answers with a JSON object carrying a ``minutely_15`` block: a
``time`` array of ISO 8601 instants and a parallel ``global_tilted_irradiance`` array of W/m²
values, plus a ``minutely_15_units`` map naming each series' unit. With ``timezone=UTC`` the
timestamps are UTC; the provider normalises every instant to a timezone-aware UTC datetime.

Time semantics (authoritative for this project): Open-Meteo documents ``global_tilted_irradiance``
as the *mean of the preceding interval* in W/m². Each listed timestamp therefore labels the END of
its 15-minute step, and the value is the mean power over ``[timestamp - 15min, timestamp]``. The
provider maps a value to ``start = timestamp - step, end = timestamp`` and does NOT shift it one
step forward. The resulting interval is then clipped to the still-future window
``[now, now + horizon]``: when the first interval already overlaps ``now`` only its still-future
fraction is kept, and the emitted ``start``/``end`` carry the clipped bounds so that ``energy_wh``
is exactly the energy of ``[start, end]`` — past production is never reported as future and the
energy is never trimmed a second time downstream.

Energy conversion is deliberately simple, transparent and documented:

    pv_power_w   = gti_w_m2 / 1000 * pv_peak_power_wp * performance_ratio
    energy_wh    = pv_power_w * interval_hours

No module-temperature correction, no pvlib, no silent repair of provider values: a negative,
non-finite, null or mistyped value, an inconsistent array length or a wrong unit is a
:class:`WeatherResponseError`, never a patched series.

HTTP uses the standard-library ``urllib`` client, the same blocking client the spot-market provider
uses; the project pins no runtime HTTP dependency. The caller runs this off the event loop in a
thread. The destination is always the provider's fixed HTTPS origin; only query parameters derived
from the validated config vary, so there is no operator-supplied host (SSRF) surface.
"""

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from urllib.request import build_opener
from zoneinfo import ZoneInfo

from app.clock import is_aware
from app.net import SameOriginRedirectHandler
from app.optimizer.models import SolarForecast, SolarForecastInterval
from app.weather.base import WeatherResponseError
from app.weather.http_json import JsonHttpClient
from app.weather.models import GTI_VARIABLE, MAX_INTERVALS, OPEN_METEO_URL, WeatherConfig

log = logging.getLogger(__name__)

# A multi-day quarter-hour GTI series is tens of kB; this cap is generous but bounds a hostile or
# runaway response so a single read cannot exhaust memory.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# The forecast series is quarter-hourly. Each adjacent timestamp pair must be exactly this far
# apart; a gap, a duplicate or a different resolution is a response error, never patched.
_STEP_SECONDS = 900
# Open-Meteo caps forecast_days at 16; the request derives the days it needs from the horizon and is
# clamped into [1, 16] so a hostile horizon cannot ask for an unbounded range.
_MAX_FORECAST_DAYS = 16
_WH_PER_WS = 1.0 / 3600.0  # watt-seconds -> watt-hours
_W_PER_W_M2_PER_WP = 1.0 / 1000.0  # GTI(W/m²) -> fraction of nameplate at 1000 W/m² reference


class _SameOriginRedirectHandler(SameOriginRedirectHandler):
    provider_label = "Open-Meteo"


# One opener for the whole process: it refuses cross-origin redirects and is the only path the
# provider uses to reach the network. Built once so the handler chain is reused.
_OPENER = build_opener(_SameOriginRedirectHandler())


class OpenMeteoProvider:
    """Solar forecasts from the Open-Meteo free Open-Access Forecast API.

    Every outbound call has a hard timeout, a bounded retry budget and explicit 429/Retry-After
    handling. A failure maps to a closed :class:`WeatherError` subclass, so a dead or rate-limited
    Open-Meteo never crashes the caller and never reaches the inverter control path. ``sleep`` is
    injected so tests exercise the retry logic without real delays.
    """

    def __init__(self, sleep: Callable[[float], None] | None = None) -> None:
        self._http = JsonHttpClient("Open-Meteo", sleep=sleep, max_response_bytes=_MAX_RESPONSE_BYTES, opener=_OPENER)

    def fetch(self, config: WeatherConfig, now: datetime, horizon: timedelta) -> SolarForecast:
        if not is_aware(now):
            raise ValueError("now must be timezone-aware")
        if horizon <= timedelta():
            raise ValueError("horizon must be a positive timedelta")
        if not config.is_forecastable():
            raise WeatherResponseError("The solar forecast configuration is incomplete")
        payload = self._get(self._build_url(config, horizon))
        intervals = self._parse(payload, config, now, horizon)
        return SolarForecast(created_at=datetime.now(UTC), intervals=intervals)

    def fetch_preview(self, config: WeatherConfig, now: datetime) -> SolarForecast:
        """Fetch complete local yesterday/today/tomorrow PV series for the admin preview.

        The optimizer-facing fetch method remains future-only. This preview path requests an exact
        UTC window derived from the site IANA timezone, so calendar-day boundaries stay correct.
        """
        if not is_aware(now):
            raise ValueError("now must be timezone-aware")
        if not config.is_forecastable():
            raise WeatherResponseError("The solar forecast configuration is incomplete")
        zone = ZoneInfo(config.timezone or "UTC")
        local_midnight = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = (local_midnight - timedelta(days=1)).astimezone(UTC)
        window_end = (local_midnight + timedelta(days=2)).astimezone(UTC)
        payload = self._get(self._build_preview_url(config, window_start, window_end))
        intervals = self._parse_window(payload, config, window_start, window_end)
        return SolarForecast(created_at=datetime.now(UTC), intervals=intervals)

    def _build_url(self, config: WeatherConfig, horizon: timedelta) -> str:
        """Build the fixed-origin request URL from the validated config and horizon.

        Only query parameters vary; the scheme/host/path come from :data:`OPEN_METEO_URL`, and the
        shared client re-checks HTTPS as a second guard against a misconfigured constant. ``forecast_days`` is clamped so a hostile horizon cannot request an unbounded range.
        """
        days = max(1, min(_MAX_FORECAST_DAYS, math.ceil(horizon.total_seconds() / 86400) + 1))
        return self._http.build_url(OPEN_METEO_URL, [
            ("latitude", f"{config.latitude:.6f}"),
            ("longitude", f"{config.longitude:.6f}"),
            ("minutely_15", GTI_VARIABLE),
            ("tilt", f"{config.tilt_deg:g}"),
            ("azimuth", f"{config.azimuth_deg:g}"),
            ("timezone", "UTC"),
            ("timeformat", "iso8601"),
            ("forecast_days", str(days)),
        ])

    def _build_preview_url(self, config: WeatherConfig, window_start: datetime, window_end: datetime) -> str:
        """Build an exact UTC minutely-15 window for local yesterday/today/tomorrow."""
        return self._http.build_url(OPEN_METEO_URL, [
            ("latitude", f"{config.latitude:.6f}"),
            ("longitude", f"{config.longitude:.6f}"),
            ("minutely_15", GTI_VARIABLE),
            ("tilt", f"{config.tilt_deg:g}"),
            ("azimuth", f"{config.azimuth_deg:g}"),
            ("timezone", "UTC"),
            ("timeformat", "iso8601"),
            ("start_minutely_15", window_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")),
            ("end_minutely_15", window_end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")),
        ])

    def _get(self, url: str) -> dict:
        return self._http.get_json(url)

    def _parse(
        self, payload: dict, config: WeatherConfig, now: datetime, horizon: timedelta
    ) -> tuple[SolarForecastInterval, ...]:
        return self._parse_window(payload, config, now, now + horizon)

    def _parse_window(
        self, payload: dict, config: WeatherConfig, window_start: datetime, window_end: datetime
    ) -> tuple[SolarForecastInterval, ...]:
        block = payload.get("minutely_15")
        if not isinstance(block, dict):
            raise WeatherResponseError("Open-Meteo response has no minutely_15 block")
        self._check_unit(payload.get("minutely_15_units"))
        times = block.get("time")
        values = block.get(GTI_VARIABLE)
        if not isinstance(times, list) or not isinstance(values, list):
            raise WeatherResponseError("Open-Meteo minutely_15 arrays are missing")
        if len(times) != len(values):
            raise WeatherResponseError("Open-Meteo time and irradiance arrays differ in length")
        if len(times) > MAX_INTERVALS:
            raise WeatherResponseError("Open-Meteo returned more intervals than expected")
        labels = [self._timestamp(raw) for raw in times]
        self._check_resolution(labels)
        intervals: list[SolarForecastInterval] = []
        for label, raw_value in zip(labels, values, strict=True):
            # Open-Meteo labels a minutely_15 value with the END of the interval it is the mean of:
            # the value at ``label`` is the mean over the preceding step [label - step, label]. The
            # value is NOT shifted forward one slot; the raw interval is [label - step, label].
            raw_start = label - timedelta(seconds=_STEP_SECONDS)
            raw_end = label
            # Only intervals that still carry future time are relevant; a fully past interval is
            # dropped, and a future interval beyond the horizon is trimmed off below.
            if raw_end <= window_start or raw_start >= window_end:
                continue
            power_w = self._power_w(raw_value, config)
            # Clip to the still-future window [max(raw_start, now), min(raw_end, horizon_end)]. When
            # raw_start < now the first interval is partial: counting only the future fraction keeps
            # past production out of the forecast. The emitted interval carries the CLIPPED bounds so
            # that energy_wh is exactly the energy of [start, end]; a downstream power = energy /
            # duration therefore stays correct and the aggregate is never trimmed a second time.
            start = max(raw_start, window_start)
            end = min(raw_end, window_end)
            future_seconds = (end - start).total_seconds()
            if future_seconds <= 0:
                continue
            energy_wh = power_w * future_seconds * _WH_PER_WS
            intervals.append(SolarForecastInterval(start=start, end=end, energy_wh=energy_wh))
        return tuple(intervals)

    @staticmethod
    def _check_unit(units: object) -> None:
        """Reject a response whose GTI series is not reported in W/m².

        ``minutely_15_units`` is provider metadata; when present it must name W/m² for the GTI
        series so the W/m²->W conversion is sound. An absent units map is tolerated (the variable
        itself is fixed in the request), a present but wrong unit is a hard error.
        """
        if units is None:
            return
        if not isinstance(units, dict):
            raise WeatherResponseError("Open-Meteo minutely_15_units is malformed")
        unit = units.get(GTI_VARIABLE)
        if unit is None:
            return
        if not isinstance(unit, str) or "".join(unit.split()).lower() not in ("w/m²", "w/m2"):
            raise WeatherResponseError(f"Open-Meteo reported an unexpected irradiance unit: {unit!r}")

    @staticmethod
    def _timestamp(raw: object) -> datetime:
        if not isinstance(raw, str):
            raise WeatherResponseError("Open-Meteo timestamp is missing or not a string")
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise WeatherResponseError("Open-Meteo timestamp is not a valid ISO 8601 value") from exc
        # With timezone=UTC Open-Meteo emits naive ISO instants that are already UTC; attach UTC so
        # downstream comparisons are unambiguous. An explicit offset is honoured and normalised.
        if moment.tzinfo is None:
            return moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC)

    @staticmethod
    def _check_resolution(starts: list[datetime]) -> None:
        """Require every adjacent interval start to be exactly 15 minutes apart.

        A missing, duplicated, unsorted or differently-resolved slot is rejected rather than turned
        into a longer interval: a later planning decision must be able to trust that one slot is
        exactly 900 seconds. Timestamps are already parsed to tz-aware UTC instants.
        """
        for previous, current in pairwise(starts):
            delta = (current - previous).total_seconds()
            if delta != _STEP_SECONDS:
                raise WeatherResponseError(
                    "Open-Meteo timestamps are not 15 minutes apart: "
                    f"{previous.isoformat()} -> {current.isoformat()} is {delta}s, expected {_STEP_SECONDS}s"
                )

    @staticmethod
    def _power_w(raw_value: object, config: WeatherConfig) -> float:
        """Convert one GTI reading (W/m²) to PV AC power (W) with the documented linear model.

        A null value means "not available": the series must be complete, so it is a hard error
        rather than a hole patched with a fabricated zero. A non-finite, negative or mistyped value
        is likewise rejected — provider values are never silently repaired.
        """
        if raw_value is None:
            raise WeatherResponseError("Open-Meteo irradiance is missing for an interval")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise WeatherResponseError("Open-Meteo irradiance is not a number")
        gti = float(raw_value)
        if not math.isfinite(gti):
            raise WeatherResponseError("Open-Meteo irradiance is not a finite number")
        if gti < 0:
            raise WeatherResponseError("Open-Meteo irradiance is negative")
        # pv_power_w = GTI/1000 * peak_Wp * performance_ratio. Non-negative inputs give a
        # non-negative result; the SolarForecastInterval contract rejects anything else.
        return gti * _W_PER_W_M2_PER_WP * config.pv_peak_power_wp * config.performance_ratio
