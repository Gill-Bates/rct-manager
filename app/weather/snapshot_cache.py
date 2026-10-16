#!/usr/bin/env python3
#
# app/weather/snapshot_cache.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Bounded, last-known-good cache for the dashboard's solar forecast.

Modelled on :class:`app.spotmarket.snapshot_cache.SpotSnapshotCache`: a disabled or incomplete
configuration triggers no network request; a configuration change invalidates the stored forecast;
the refresh runs on a daemon thread, never on the FastAPI event loop; concurrent requests inside the
refresh window share one in-flight refresh rather than starting a request storm; the refresh cadence
is a fixed in-code constant (not an environment setting). A temporary provider failure keeps the
last successful forecast as last-known-good and reports ``failed=True`` so a projection can mark it
stale; a failure with no prior forecast reports ``(None, True)`` which the projection renders as
error/unavailable. An unexpected cache error is logged and treated as a forecast failure, isolated
from the rest of the service.
"""

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.optimizer.models import SolarForecast
from app.weather.base import WeatherError
from app.weather.models import WeatherConfig

log = logging.getLogger(__name__)

# How often the forecast is refreshed. A fixed in-code cadence (not operator-settable), in the
# 15–30 minute range the task calls for: Open-Meteo updates on that order and the free endpoint must
# not be polled aggressively (roughly 10,000 requests/day; see docs/energy-manager.md).
REFRESH_SECONDS = 20 * 60
# The horizon a background refresh fetches. One day of quarter-hour intervals comfortably covers the
# dashboard's "next 24 hours" and the next planning cycle without requesting an unbounded range.
FORECAST_HORIZON = timedelta(hours=36)


class SolarSnapshotCache:
    def __init__(
        self,
        fetch: Callable[[WeatherConfig, datetime, timedelta], SolarForecast],
        refresh_seconds: int = REFRESH_SECONDS,
        *,
        now: Callable[[], datetime] | None = None,
        horizon: timedelta = FORECAST_HORIZON,
    ) -> None:
        self._fetch = fetch
        self._refresh_seconds = refresh_seconds
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._horizon = horizon
        self._lock = threading.Lock()
        self._config: WeatherConfig | None = None
        self._snapshot: SolarForecast | None = None
        self._failed = False
        self._next_fetch = 0.0
        self._refreshing = False
        self._generation = 0

    def get(self, config: WeatherConfig) -> tuple[SolarForecast | None, bool]:
        """Return ``(last_known_good_forecast_or_None, failed)`` without ever blocking on the network.

        A changed configuration drops the previous forecast and schedules a fresh fetch. A disabled
        or incomplete configuration serves nothing and triggers no request. Otherwise a background
        refresh is started at most once per refresh window; the current snapshot (which may be the
        last-known-good) is returned immediately.
        """
        with self._lock:
            if config != self._config:
                self._generation += 1
                self._config = config
                self._snapshot = None
                self._failed = False
                self._next_fetch = 0.0
            if not config.is_forecastable():
                return None, False
            now = time.monotonic()
            if now >= self._next_fetch and not self._refreshing:
                self._next_fetch = now + self._refresh_seconds
                self._refreshing = True
                threading.Thread(
                    target=self._refresh, args=(config, self._generation), daemon=True,
                    name="solar-snapshot-refresh",
                ).start()
            return self._snapshot, self._failed

    def _refresh(self, config: WeatherConfig, generation: int) -> None:
        try:
            snapshot = self._fetch(config, self._now(), self._horizon)
            failed = False
        except WeatherError:
            snapshot = None
            failed = True
        except Exception:
            log.exception("Solar snapshot refresh failed unexpectedly")
            snapshot = None
            failed = True
        with self._lock:
            self._refreshing = False
            if generation == self._generation:
                if snapshot is not None:
                    self._snapshot = snapshot
                self._failed = failed
