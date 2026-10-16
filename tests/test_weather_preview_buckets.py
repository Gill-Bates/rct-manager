#!/usr/bin/env python3
#
# tests/test_weather_preview_buckets.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Weather preview day-bucketing invariant backing the frontend "Forecast preview" chart fix.

The WEATHER tab chart was blanked whenever any single day-bucket (yesterday / today / tomorrow) was
empty, even though the NEXT-24H KPI stayed valid. This test locks in the backend contract that makes
the frontend fix the whole fix: a day the Open-Meteo forecast window does not cover serialises as an
EMPTY list (``[]``), not ``null``, while the KPI (built from still-future intervals) stays non-zero.
No backend change is required or made; the preview path and its invariants (provider owns its fixed
origin, timezone derived server-side, forecast input-only) are untouched. The admin-session and
settings setup mirror ``tests/test_energy_api.py`` but is kept local so this file does not import
from a module a concurrent workflow is editing.
"""

from datetime import UTC, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.admin.forecast_refresh import refresh_solar_forecast
from app.admin.store import AdminStore
from app.optimizer.models import SolarForecast, SolarForecastInterval
from tests.api_helpers import make_settings, running_app

SECRET = "d" * 48
PASSWORD = "replacement-test-password"

WEATHER_PREVIEW = "/admin/api/energy/weather/preview"
WEATHER_CONFIG = "/admin/api/energy/weather/config"


def _spot_market_settings(tmp_path: Path):
    # Mount the admin energy router with the bootstrap password already changed to ``PASSWORD``.
    admin_path = tmp_path / "weather-admin.db"
    store = AdminStore(admin_path, SECRET)
    password = store.initialize()
    assert password is not None
    assert store.change_password(password, PASSWORD)
    store.close()
    return make_settings(hmac_secret=SECRET, admin_db_path=admin_path)


class _WeatherTimezoneResolver:
    def resolve(self, latitude, longitude):
        return "America/New_York" if longitude < -30 else "Europe/Berlin"


def _stub_weather_timezone(harness) -> None:
    harness.app.state.solar_timezone_resolver = _WeatherTimezoneResolver()


def _weather_body(**overrides) -> dict:
    base = {
        "enabled": True,
        "provider": "open_meteo",
        "location_name": "Remscheid, North Rhine-Westphalia, Germany",
        "latitude": 51.1798,
        "longitude": 7.1925,
        "pv_peak_power_wp": 10000.0,
        "tilt_deg": 35.0,
        "azimuth_deg": 0.0,
        "performance_ratio": 0.85,
    }
    return {**base, **overrides}


async def _admin_session(harness) -> dict[str, str]:
    csrf = (await harness.client.get("/admin/api/session")).json()["csrf_token"]
    login = await harness.client.post(
        "/admin/api/login",
        headers={"X-CSRF-Token": csrf},
        json={"username": "admin", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    return {"X-CSRF-Token": login.json()["csrf_token"]}


async def test_uncovered_day_bucket_is_empty_while_the_kpi_stays_valid(tmp_path: Path) -> None:
    # The forecast window covers only today and tomorrow (nothing for yesterday). The preview must
    # report a non-empty today bucket, an EMPTY (not null) yesterday bucket, and a non-zero NEXT-24H
    # KPI — the exact "KPI valid while a day-bucket is empty" condition the frontend chart fix draws
    # through instead of blanking the whole chart.
    class _TodayAndTomorrowProvider:
        def fetch_preview(self, config, now):
            zone = ZoneInfo(config.timezone or "UTC")
            midnight = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
            starts = [
                midnight + timedelta(hours=12),
                midnight + timedelta(days=1, hours=12),
            ]
            return SolarForecast(
                created_at=now,
                intervals=tuple(
                    SolarForecastInterval(
                        start.astimezone(UTC),
                        (start + timedelta(minutes=15)).astimezone(UTC),
                        1000.0,
                    )
                    for start in starts
                ),
            )

    async with running_app(_spot_market_settings(tmp_path)) as harness:
        _stub_weather_timezone(harness)
        harness.app.state.solar_forecast_provider = _TodayAndTomorrowProvider()
        headers = await _admin_session(harness)
        saved = await harness.client.put(WEATHER_CONFIG, headers=headers, json=_weather_body())
        assert saved.status_code == 200, saved.text
        await refresh_solar_forecast(
            harness.app.state.admin_store,
            harness.app.state.solar_forecast_provider,
            harness.app.state.weather_config,
        )
        response = await harness.client.post(WEATHER_PREVIEW, headers=headers, json=_weather_body())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "connected"
        # A day the window does not cover is an empty list, never null — the frontend treats it as a
        # simply-absent series rather than an invalid one.
        assert body["yesterday_intervals"] == []
        assert len(body["today_intervals"]) == 1
        assert len(body["next_day_intervals"]) == 1
        # The KPI stays valid even though a day-bucket is empty, which is why the chart must still
        # draw from the usable series.
        assert body["energy_next_24h_kwh"] > 0
