#!/usr/bin/env python3
#
# tests/test_weather.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Open-Meteo solar-forecast provider, configuration and last-known-good cache.

No test reaches the real Open-Meteo API: the one outbound call is monkeypatched at the module-level
opener (``open_meteo._OPENER``). The tests pin the URL/query contract, the fixed HTTPS origin and
cross-origin-redirect refusal, the GTI->W->Wh conversion and performance ratio, timezone-aware UTC
intervals, the "mean of the preceding interval" placement, the partial first interval, horizon
trimming, every malformed-response rejection, the retry/Retry-After budget, and the cache's
disabled/no-storm/invalidation/last-known-good behaviour.
"""

import io
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from app.net import MAX_RETRY_AFTER_SECONDS, retry_after_seconds
from app.weather import models, open_meteo
from app.weather.base import WeatherConnectionError, WeatherResponseError
from app.weather.http_json import MAX_ATTEMPTS, REQUEST_TIMEOUT_SECONDS
from app.weather.models import WeatherConfig, WeatherProvider
from app.weather.open_meteo import OpenMeteoProvider
from app.weather.snapshot_cache import SolarSnapshotCache

NOW = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, 1.0),
        ("bad", 1.0),
        (" 2 ", 2.0),
        ("-2", 0.0),
        ("99999", 30.0),
        ("Tue, 21 Jul 2026 10:00:05 GMT", 5.0),
        ("Tue, 21 Jul 2026 09:59:55 GMT", 0.0),
        ("Tue, 21 Jul 2026 10:00:05", 5.0),
    ],
)
def test_shared_retry_after_parser_bounds_delta_and_http_dates(
    header: str | None, expected: float
) -> None:
    assert retry_after_seconds(header, now=NOW) == expected


def test_retry_after_clamps_integer_too_large_for_float() -> None:
    assert retry_after_seconds("9" * 400, now=NOW) == MAX_RETRY_AFTER_SECONDS


def _config(**overrides) -> WeatherConfig:
    values = {
        "enabled": True,
        "latitude": 52.52,
        "longitude": 13.405,
        "pv_peak_power_wp": 8000.0,
        "tilt_deg": 30.0,
        "azimuth_deg": 0.0,
        "performance_ratio": 0.9,
    }
    values.update(overrides)
    return WeatherConfig(**values)


def _labels(count: int, first_start: datetime = NOW) -> list[str]:
    # Open-Meteo labels each minutely_15 value with the END of its interval (the value is the mean
    # over the preceding 15 minutes). The returned strings are the end labels for ``count``
    # consecutive intervals whose first interval starts at ``first_start``, in the naive UTC ISO
    # form Open-Meteo emits with timezone=UTC (minute precision).
    return [
        (first_start + timedelta(minutes=15 * (i + 1))).strftime("%Y-%m-%dT%H:%M")
        for i in range(count)
    ]


def _payload(times, values, *, unit="W/m²"):
    return {
        "latitude": 52.52,
        "longitude": 13.405,
        "minutely_15_units": {"time": "iso8601", "global_tilted_irradiance": unit},
        "minutely_15": {"time": list(times), "global_tilted_irradiance": list(values)},
    }


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _patch_opener(
    monkeypatch, *, payload=None, raw=None, error=None, capture=None, errors=None
):
    """Stub the module-level opener's ``open``. ``errors`` is a list of exceptions raised in order
    (then the payload is served) so the retry path can be exercised deterministically.
    """
    sequence = list(errors) if errors is not None else []

    def fake_open(request, timeout=None):
        if capture is not None:
            capture["url"] = request.full_url
            capture["timeout"] = timeout
        if sequence:
            raise sequence.pop(0)
        if error is not None:
            raise error
        body = raw if raw is not None else json.dumps(payload).encode()
        return _FakeResponse(body)

    monkeypatch.setattr(open_meteo._OPENER, "open", fake_open)


# Configuration


def test_disabled_config_may_be_partial():
    config = WeatherConfig(enabled=False, latitude=52.52)
    assert config.enabled is False
    assert config.is_forecastable() is False


def test_enabled_config_requires_every_forecast_value():
    with pytest.raises(ValueError, match="latitude"):
        WeatherConfig(
            enabled=True,
            longitude=13.0,
            pv_peak_power_wp=5000,
            tilt_deg=30,
            azimuth_deg=0,
        )


def test_config_rejects_unknown_field():
    with pytest.raises(ValueError):
        WeatherConfig(enabled=False, unexpected=1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"latitude": 91.0},
        {"longitude": -181.0},
        {"tilt_deg": 91.0},
        {"azimuth_deg": 200.0},
        {"performance_ratio": 1.5},
        {"performance_ratio": 0.0},
        {"pv_peak_power_wp": 0.0},
    ],
)
def test_config_rejects_out_of_range_values(overrides):
    with pytest.raises(ValueError):
        _config(**overrides)


def test_default_provider_is_open_meteo():
    assert WeatherConfig().provider is WeatherProvider.OPEN_METEO


def test_location_name_and_timezone_round_trip():
    config = WeatherConfig(
        enabled=False,
        location_name="Remscheid, North Rhine-Westphalia, Germany",
        timezone="Europe/Berlin",
    )
    assert config.location_name == "Remscheid, North Rhine-Westphalia, Germany"
    assert config.timezone == "Europe/Berlin"


def test_an_empty_timezone_normalises_to_none():
    assert WeatherConfig(timezone="   ").timezone is None


def test_an_unknown_timezone_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        WeatherConfig(timezone="Mars/Olympus_Mons")


def test_location_name_and_timezone_are_descriptive_not_required_when_enabled():
    # An enabled config needs coordinates and PV geometry, but not a location_name or timezone:
    # those are descriptive, and the provider always requests the forecast in UTC.
    config = _config(location_name=None, timezone=None)
    assert config.is_forecastable() is True


def test_forecast_required_fields_match_between_validation_and_is_forecastable():
    # Both checks share one field list, so each missing field is reported AND blocks forecasting.
    for name in models._FORECAST_REQUIRED_FIELDS:
        with pytest.raises(ValueError, match=name):
            _config(**{name: None})
        assert _config(enabled=False, **{name: None}).is_forecastable() is False
    assert "timezone" not in models._FORECAST_REQUIRED_FIELDS


# Request contract


def test_request_targets_the_fixed_https_origin_with_expected_parameters(monkeypatch):
    capture = {}
    _patch_opener(
        monkeypatch, payload=_payload(_labels(4), [0, 100, 200, 300]), capture=capture
    )
    OpenMeteoProvider().fetch(
        _config(tilt_deg=35, azimuth_deg=-90), NOW, timedelta(hours=1)
    )
    url = capture["url"]
    assert url.startswith("https://api.open-meteo.com/v1/forecast?")
    assert "minutely_15=global_tilted_irradiance" in url
    assert "tilt=35" in url
    assert "azimuth=-90" in url
    assert "timezone=UTC" in url
    assert "latitude=52.520000" in url
    assert "longitude=13.405000" in url
    assert capture["timeout"] == REQUEST_TIMEOUT_SECONDS


def test_forecast_and_preview_urls_are_exact(monkeypatch):
    capture = {}
    _patch_opener(
        monkeypatch, payload=_payload(_labels(4), [0, 100, 200, 300]), capture=capture
    )
    OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))
    base = (
        "https://api.open-meteo.com/v1/forecast?latitude=52.520000&longitude=13.405000"
        "&minutely_15=global_tilted_irradiance&tilt=30&azimuth=0&timezone=UTC&timeformat=iso8601"
    )
    assert capture["url"] == base + "&forecast_days=2"
    window_start = datetime(2026, 7, 19, 22, 0, tzinfo=UTC)
    _patch_opener(
        monkeypatch, payload=_payload(_labels(2, window_start), [1, 2]), capture=capture
    )
    OpenMeteoProvider().fetch_preview(_config(timezone="Europe/Berlin"), NOW)
    assert capture["url"] == (
        base
        + "&start_minutely_15=2026-07-19T22%3A00&end_minutely_15=2026-07-22T22%3A00"
    )


def test_preview_without_a_site_timezone_uses_utc_day_boundaries(monkeypatch):
    # The whole pipeline treats a missing timezone as UTC (refresh, storage, API windows).
    capture = {}
    window_start = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    _patch_opener(
        monkeypatch, payload=_payload(_labels(2, window_start), [1, 2]), capture=capture
    )
    OpenMeteoProvider().fetch_preview(_config(timezone=None), NOW)
    assert "start_minutely_15=2026-07-20T00%3A00" in capture["url"]
    assert "end_minutely_15=2026-07-23T00%3A00" in capture["url"]


def test_horizon_bounds_the_requested_forecast_days(monkeypatch):
    capture = {}
    _patch_opener(
        monkeypatch, payload=_payload(_labels(4), [0, 100, 200, 300]), capture=capture
    )
    OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))
    # ceil(1h / 1 day) + 1 day of headroom so a horizon crossing midnight is still covered.
    assert "forecast_days=2" in capture["url"]
    _patch_opener(
        monkeypatch, payload=_payload(_labels(4), [0, 100, 200, 300]), capture=capture
    )
    OpenMeteoProvider().fetch(_config(), NOW, timedelta(days=40))
    assert f"forecast_days={open_meteo._MAX_FORECAST_DAYS}" in capture["url"]


def test_preview_requests_exact_local_yesterday_today_tomorrow_window(monkeypatch):
    capture = {}
    config = _config(timezone="Europe/Berlin")
    # 21 Jul 10:00 UTC is 12:00 CEST. Local 20 Jul 00:00 -> 23 Jul 00:00 is
    # 19 Jul 22:00 UTC -> 22 Jul 22:00 UTC.
    window_start = datetime(2026, 7, 19, 22, 0, tzinfo=UTC)
    _patch_opener(
        monkeypatch,
        payload=_payload(_labels(2, window_start), [100, 200]),
        capture=capture,
    )
    preview = OpenMeteoProvider().fetch_preview(config, NOW)
    assert "start_minutely_15=2026-07-19T22%3A00" in capture["url"]
    assert "end_minutely_15=2026-07-22T22%3A00" in capture["url"]
    assert "timezone=UTC" in capture["url"]
    assert preview.intervals[0].start == window_start
    assert preview.intervals[0].start < NOW


def test_a_cross_origin_redirect_is_refused(monkeypatch):
    def fake_open(request, timeout=None):
        raise HTTPError(request.full_url, 302, "redirect", {}, None)

    monkeypatch.setattr(open_meteo._OPENER, "open", fake_open)
    with pytest.raises(WeatherResponseError):
        OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))


def _redirect(source_url: str, target_url: str):
    """Drive the shared same-origin handler's decision for a 302 from ``source`` to ``target``."""
    handler = open_meteo._SameOriginRedirectHandler()
    request = Request(source_url)
    return handler.redirect_request(
        request, io.BytesIO(b""), 302, "Found", {}, target_url
    )


def test_a_same_origin_redirect_is_allowed():
    # The handler is now shared via app.net; the provider must still follow a same-origin relocation.
    new_request = _redirect(
        "https://api.open-meteo.com/v1/forecast",
        "https://api.open-meteo.com/v1/forecast/alt",
    )
    assert new_request is not None
    assert new_request.full_url == "https://api.open-meteo.com/v1/forecast/alt"


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/forecast",
        "https://evil.example/forecast",
        "http://api.open-meteo.com/v1/forecast",
    ],
)
def test_the_handler_refuses_a_cross_origin_or_downgrade_redirect(target):
    with pytest.raises(HTTPError, match="Open-Meteo redirected to a different origin"):
        _redirect("https://api.open-meteo.com/v1/forecast", target)


# Conversion


def test_gti_is_converted_to_energy_with_the_documented_formula(monkeypatch):
    # 500 W/m² at 8000 Wp and PR 0.9 -> 500/1000 * 8000 * 0.9 = 3600 W. One quarter hour = 900 Wh.
    # Labels end at NOW+15, NOW+30, so intervals are [NOW, NOW+15] and [NOW+15, NOW+30].
    _patch_opener(monkeypatch, payload=_payload(_labels(2), [500, 500]))
    forecast = OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))
    assert len(forecast.intervals) == 2
    assert forecast.intervals[0].energy_wh == pytest.approx(900.0)
    for interval in forecast.intervals:
        assert interval.start.tzinfo is not None and interval.end.tzinfo is not None
        assert (interval.end - interval.start).total_seconds() == 900


def test_performance_ratio_scales_the_energy(monkeypatch):
    _patch_opener(monkeypatch, payload=_payload(_labels(1), [1000]))
    full = OpenMeteoProvider().fetch(
        _config(performance_ratio=1.0), NOW, timedelta(hours=1)
    )
    _patch_opener(monkeypatch, payload=_payload(_labels(1), [1000]))
    half = OpenMeteoProvider().fetch(
        _config(performance_ratio=0.5), NOW, timedelta(hours=1)
    )
    assert half.intervals[0].energy_wh == pytest.approx(full.intervals[0].energy_wh / 2)


def test_a_value_labels_the_preceding_interval_not_the_following_one(monkeypatch):
    # Open-Meteo labels a value with the END of its interval: the value at label NOW+15 is the mean
    # over [NOW, NOW+15]. The provider must map it to start=NOW, end=NOW+15 and NOT shift it forward
    # to [NOW+15, NOW+30].
    _patch_opener(monkeypatch, payload=_payload(_labels(2), [500, 500]))
    forecast = OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))
    assert forecast.intervals[0].start == NOW
    assert forecast.intervals[0].end == NOW + timedelta(minutes=15)


def test_a_partial_first_interval_counts_only_its_future_fraction(monkeypatch):
    # The first interval is [NOW-5min, NOW+10min] (label NOW+10min): only the future 10 of 15
    # minutes count, and the emitted bounds are clipped to [NOW, NOW+10min] so energy_wh is exactly
    # the energy of that clipped window — no second trimming downstream.
    label = NOW + timedelta(minutes=10)
    first_start = label - timedelta(minutes=15)  # NOW - 5min
    times = [
        label.strftime("%Y-%m-%dT%H:%M"),
        (label + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M"),
    ]
    _patch_opener(monkeypatch, payload=_payload(times, [600, 600]))
    forecast = OpenMeteoProvider().fetch(
        _config(performance_ratio=1.0), NOW, timedelta(hours=1)
    )
    first = forecast.intervals[0]
    # 600 W/m² * 8000 Wp / 1000 = 4800 W; the clipped window is 10 minutes = 1/6 h -> 800 Wh.
    assert first.start == NOW
    assert first.end == label
    assert first_start < NOW  # the raw interval really did start in the past
    assert first.energy_wh == pytest.approx(4800 * (10 / 60))


def test_a_fully_past_interval_is_dropped(monkeypatch):
    # Labels at NOW-15, NOW, NOW+15, NOW+30 -> intervals [NOW-30,NOW-15], [NOW-15,NOW] (both fully
    # past, dropped) and [NOW,NOW+15], [NOW+15,NOW+30].
    first_start = NOW - timedelta(minutes=30)
    _patch_opener(
        monkeypatch, payload=_payload(_labels(4, first_start), [100, 200, 300, 400])
    )
    forecast = OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))
    assert all(interval.end > NOW for interval in forecast.intervals)
    assert forecast.intervals[0].start == NOW


def test_the_horizon_trims_future_intervals(monkeypatch):
    _patch_opener(monkeypatch, payload=_payload(_labels(8), [200] * 8))
    forecast = OpenMeteoProvider().fetch(_config(), NOW, timedelta(minutes=30))
    # Only the two intervals that fall inside [now, now+30min) remain, and the last end is clipped
    # exactly to now+30min, never now+45min.
    assert len(forecast.intervals) == 2
    assert forecast.intervals[-1].end == NOW + timedelta(minutes=30)


def test_intervals_are_sorted_and_non_overlapping(monkeypatch):
    _patch_opener(monkeypatch, payload=_payload(_labels(4), [100, 200, 300, 400]))
    forecast = OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=2))
    for previous, current in zip(
        forecast.intervals, forecast.intervals[1:], strict=False
    ):
        assert current.start >= previous.end


# Malformed responses


@pytest.mark.parametrize(
    "value", [-10, float("nan"), float("inf"), None, "sunny", True]
)
def test_unusable_irradiance_values_are_response_errors(monkeypatch, value):
    payload = _payload(_labels(2), [100, value])
    _patch_opener(monkeypatch, payload=payload)
    with pytest.raises(WeatherResponseError):
        OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))


def test_a_wrong_irradiance_unit_is_a_response_error(monkeypatch):
    _patch_opener(monkeypatch, payload=_payload(_labels(2), [100, 200], unit="kW/m²"))
    with pytest.raises(WeatherResponseError):
        OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))


def test_missing_arrays_are_a_response_error(monkeypatch):
    _patch_opener(monkeypatch, payload={"minutely_15": {"time": _labels(2)}})
    with pytest.raises(WeatherResponseError):
        OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))


def test_differing_array_lengths_are_a_response_error(monkeypatch):
    _patch_opener(monkeypatch, payload=_payload(_labels(3), [100, 200]))
    with pytest.raises(WeatherResponseError):
        OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))


def test_a_non_quarter_hour_grid_is_a_response_error(monkeypatch):
    times = ["2026-07-21T10:00", "2026-07-21T10:20"]  # 20 minutes apart
    _patch_opener(monkeypatch, payload=_payload(times, [100, 200]))
    with pytest.raises(WeatherResponseError):
        OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))


def test_invalid_json_is_a_response_error(monkeypatch):
    _patch_opener(monkeypatch, raw=b"not json")
    with pytest.raises(WeatherResponseError):
        OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))


def test_an_oversized_response_is_a_response_error(monkeypatch):
    _patch_opener(monkeypatch, raw=b"x" * (open_meteo._MAX_RESPONSE_BYTES + 2))
    with pytest.raises(WeatherResponseError):
        OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))


# Failure and retry handling


def test_a_connection_error_is_a_weather_connection_error(monkeypatch):
    _patch_opener(monkeypatch, error=URLError("name resolution failed"))
    with pytest.raises(WeatherConnectionError):
        OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))


def test_a_timeout_is_a_weather_connection_error(monkeypatch):
    _patch_opener(monkeypatch, error=TimeoutError("timed out"))
    with pytest.raises(WeatherConnectionError):
        OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))


def test_a_4xx_is_a_response_error(monkeypatch):
    _patch_opener(monkeypatch, error=HTTPError("u", 400, "bad request", {}, None))
    with pytest.raises(WeatherResponseError):
        OpenMeteoProvider().fetch(_config(), NOW, timedelta(hours=1))


def test_a_429_is_retried_then_succeeds(monkeypatch):
    slept = []
    _patch_opener(
        monkeypatch,
        payload=_payload(_labels(2), [100, 200]),
        errors=[HTTPError("u", 429, "slow down", {"Retry-After": "2"}, None)],
    )
    forecast = OpenMeteoProvider(sleep=slept.append).fetch(
        _config(), NOW, timedelta(hours=1)
    )
    assert len(forecast.intervals) == 2
    assert slept == [2.0]


def test_a_5xx_is_retried(monkeypatch):
    slept = []
    _patch_opener(
        monkeypatch,
        payload=_payload(_labels(1), [100]),
        errors=[HTTPError("u", 503, "unavailable", {}, None)],
    )
    forecast = OpenMeteoProvider(sleep=slept.append).fetch(
        _config(), NOW, timedelta(hours=1)
    )
    assert len(forecast.intervals) == 1
    assert slept == [1.0]  # no Retry-After -> one second fallback


def test_an_over_long_retry_after_is_clamped(monkeypatch):
    slept = []
    _patch_opener(
        monkeypatch,
        payload=_payload(_labels(1), [100]),
        errors=[HTTPError("u", 429, "slow", {"Retry-After": "9999"}, None)],
    )
    OpenMeteoProvider(sleep=slept.append).fetch(_config(), NOW, timedelta(hours=1))
    assert slept == [float(MAX_RETRY_AFTER_SECONDS)]


def test_an_exhausted_retry_budget_is_a_connection_error(monkeypatch):
    errors = [HTTPError("u", 429, "slow", {}, None) for _ in range(MAX_ATTEMPTS)]
    _patch_opener(monkeypatch, errors=errors)
    with pytest.raises(WeatherConnectionError):
        OpenMeteoProvider(sleep=lambda _s: None).fetch(
            _config(), NOW, timedelta(hours=1)
        )


# Cache


def _ready(snapshot_or_exc):
    """A fetch callable returning a prebuilt forecast or raising a prebuilt exception."""

    def fetch(config, now, horizon):
        if isinstance(snapshot_or_exc, BaseException):
            raise snapshot_or_exc
        return snapshot_or_exc

    return fetch


def _wait_until(predicate, timeout=2.0):
    """Poll until ``predicate`` is true or the timeout elapses; the refresh runs on a daemon thread
    and records its result after the fetch returns, so a flag set inside the fetch would race it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_disabled_config_triggers_no_fetch():
    calls = []

    def fetch(config, now, horizon):
        calls.append(config)
        raise AssertionError("must not fetch")

    cache = SolarSnapshotCache(fetch)
    assert cache.get(WeatherConfig(enabled=False)) == (None, False)
    assert calls == []


def test_the_first_fetch_populates_the_cache(monkeypatch):
    from app.optimizer.models import SolarForecast, SolarForecastInterval

    forecast = SolarForecast(
        NOW, (SolarForecastInterval(NOW, NOW + timedelta(minutes=15), 100.0),)
    )
    cache = SolarSnapshotCache(_ready(forecast))
    assert cache.get(_config()) == (None, False)  # refresh scheduled on a thread
    # The next get returns the populated snapshot (the window has not elapsed, so no new fetch).
    assert _wait_until(lambda: cache.get(_config())[0] is forecast)
    assert cache.get(_config()) == (forecast, False)


def test_no_fetch_storm_within_the_refresh_window():
    count = {"n": 0}
    lock = threading.Lock()
    gate = threading.Event()

    def fetch(config, now, horizon):
        with lock:
            count["n"] += 1
        gate.wait(1)
        from app.optimizer.models import SolarForecast

        return SolarForecast(NOW, ())

    cache = SolarSnapshotCache(fetch)
    for _ in range(5):
        cache.get(_config())
    gate.set()
    assert count["n"] == 1


def test_a_config_change_invalidates_the_cache():
    from app.optimizer.models import SolarForecast

    first = SolarForecast(NOW, ())
    done = threading.Event()

    def fetch(config, now, horizon):
        try:
            return first
        finally:
            done.set()

    cache = SolarSnapshotCache(fetch)
    cache.get(_config())
    assert done.wait(2)
    done.clear()
    # A different config drops the snapshot immediately and schedules a new fetch.
    snapshot, failed = cache.get(_config(tilt_deg=45))
    assert snapshot is None and failed is False


def test_a_failure_keeps_the_last_known_good():
    from app.optimizer.models import SolarForecast

    good = SolarForecast(NOW, ())
    state = {"mode": "ok"}

    def fetch(config, now, horizon):
        if state["mode"] == "ok":
            return good
        raise WeatherConnectionError("down")

    cache = SolarSnapshotCache(fetch, refresh_seconds=0)
    cache.get(_config())
    assert _wait_until(lambda: cache.get(_config())[0] is good)
    state["mode"] = "fail"
    cache.get(_config())  # refresh_seconds=0 forces a new refresh
    # The last-known-good forecast stays while the refresh now reports failed=True.
    assert _wait_until(lambda: cache.get(_config()) == (good, True))


def test_a_failure_with_no_snapshot_reports_error():
    cache = SolarSnapshotCache(_ready(WeatherConnectionError("down")))
    cache.get(_config())
    assert _wait_until(lambda: cache.get(_config()) == (None, True))


def test_an_unexpected_exception_is_isolated():
    cache = SolarSnapshotCache(_ready(RuntimeError("bug")))
    cache.get(_config())
    assert _wait_until(lambda: cache.get(_config()) == (None, True))
