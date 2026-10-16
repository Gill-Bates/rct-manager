#!/usr/bin/env python3
#
# tests/test_spotmarket.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Spot-market provider and configuration: Energy-Charts v2 next-day parsing, unit normalisation,
timezone-aware intervals and closed failure handling. No test reaches the real Fraunhofer API; the
one outbound call is monkeypatched at the module-level opener (``energy_charts._OPENER``).
"""

import dataclasses
import io
import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

import pytest

from app.net import MAX_RETRY_AFTER_SECONDS
from app.spotmarket import energy_charts
from app.spotmarket.base import SpotMarketConnectionError, SpotMarketNotAvailableYet, SpotMarketResponseError
from app.spotmarket.energy_charts import EnergyChartsProvider
from app.spotmarket.models import (
    PriceUnit,
    SpotMarketConfig,
    SpotMarketProvider,
    SpotPriceInterval,
    SpotPriceSnapshot,
)
from app.spotmarket.snapshot_cache import SpotSnapshotCache


def _v2_payload(timestamps, prices, *, unit="EUR / MWh", series_unit=None, interval_minutes=15):
    """Build a ``/v2/price_next_day`` envelope from ISO 8601 interval starts and prices.

    ``timestamps`` are ISO 8601 strings with a UTC offset (as the v2 envelope delivers them in the
    bidding zone's local timezone); ``prices`` is the parallel value list. A single ``price`` series
    is used, matching the price endpoints.
    """
    descriptor = {"id": "price", "name": "Day-ahead price"}
    if series_unit is not None:
        descriptor["unit"] = series_unit
    return {
        "schema_version": "2.0",
        "endpoint": "price_next_day",
        "bidding_zone": "DE-LU",
        "timezone": "Europe/Berlin",
        "resolution": "PT15M",
        "interval_minutes": interval_minutes,
        "unit": unit,
        "series": [descriptor],
        "data": [{"timestamp": ts, "values": {"price": price}} for ts, price in zip(timestamps, prices, strict=True)],
        "deprecated": False,
    }


# Three contiguous 15-minute intervals in Europe/Berlin (summer, +02:00), EUR/MWh.
_STARTS = ["2026-07-21T00:00:00+02:00", "2026-07-21T00:15:00+02:00", "2026-07-21T00:30:00+02:00"]


def _day_starts(day: str = "2026-07-21", offset: str = "+02:00", count: int = 96) -> list[str]:
    """Quarter-hour starts from local midnight of ``day``; the offset must hold for the whole day."""
    return [f"{day}T{(i * 15) // 60:02d}:{(i * 15) % 60:02d}:00{offset}" for i in range(count)]


def _day_prices(head, count: int = 96) -> list[float]:
    return [*head, *([100.0] * (count - len(head)))]


# A complete local day: only the first three prices matter to the tests that read them back.
_PRICE_PAYLOAD = _v2_payload(_day_starts(), _day_prices([178.73, 177.82, 176.58]))
# The same first start as a tz-aware UTC instant: +02:00 00:00 == 22:00 UTC the previous day.
_FIRST_START_UTC = datetime.fromisoformat(_STARTS[0]).astimezone(UTC)


class _FakeResponse(io.BytesIO):
    """A context-manager byte stream standing in for urlopen's HTTPResponse."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _patch_opener(monkeypatch, *, payload=None, raw=None, error=None, capture=None):
    """Stub the module-level opener's ``open``. The provider reaches the network only through
    ``energy_charts._OPENER``; patching it keeps every test off the real Fraunhofer API.
    """

    def fake_open(request, timeout=None):
        if capture is not None:
            capture["url"] = request.full_url
            capture["timeout"] = timeout
        if error is not None:
            raise error
        body = raw if raw is not None else json.dumps(payload).encode()
        return _FakeResponse(body)

    monkeypatch.setattr(energy_charts._OPENER, "open", fake_open)


def test_day_ahead_normalises_eur_per_mwh_to_eur_per_kwh(monkeypatch):
    _patch_opener(monkeypatch, payload=_PRICE_PAYLOAD)
    snapshot = EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())
    assert snapshot.provider is SpotMarketProvider.ENERGY_CHARTS
    # Currency is provider metadata, read from the provider, not from operator config.
    assert snapshot.currency == "EUR"
    assert len(snapshot.intervals) == 96
    # 178.73 EUR/MWh -> 0.17873 EUR/kWh, exact in Decimal (no binary-float drift).
    assert snapshot.intervals[0].price_eur_per_kwh == Decimal("178.73") / Decimal(1000)
    assert snapshot.intervals[0].price_eur_per_kwh == Decimal("0.17873")


def test_intervals_are_timezone_aware_and_fifteen_minutes_long(monkeypatch):
    _patch_opener(monkeypatch, payload=_PRICE_PAYLOAD)
    intervals = EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig()).intervals
    for interval in intervals:
        assert interval.start.tzinfo is not None
        assert interval.end.tzinfo is not None
        assert (interval.end - interval.start).total_seconds() == 900
    # The first start matches the first envelope timestamp, normalised to the same UTC instant.
    assert intervals[0].start == _FIRST_START_UTC
    # Consecutive intervals are contiguous: each end is the next start.
    assert intervals[0].end == intervals[1].start


def test_a_series_level_unit_overrides_the_envelope_unit(monkeypatch):
    # The envelope declares EUR/MWh but the series overrides to EUR/kWh; the override wins, so the
    # price must NOT be divided by 1000 a second time.
    payload = _v2_payload(_day_starts(), _day_prices([0.17, 0.18, 0.19]), unit="EUR/MWh", series_unit="EUR/kWh")
    _patch_opener(monkeypatch, payload=payload)
    intervals = EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig()).intervals
    assert intervals[0].price_eur_per_kwh == Decimal("0.17")


def test_already_eur_per_kwh_is_not_divided(monkeypatch):
    payload = _v2_payload(_day_starts(), _day_prices([0.17, 0.18, 0.19]), unit="EUR/kWh")
    _patch_opener(monkeypatch, payload=payload)
    intervals = EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig()).intervals
    assert intervals[0].price_eur_per_kwh == Decimal("0.17")


def test_configured_zone_is_sent_to_the_v2_next_day_endpoint(monkeypatch):
    capture: dict = {}
    _patch_opener(monkeypatch, payload=_PRICE_PAYLOAD, capture=capture)
    EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig(bidding_zone="AT"))
    assert "bzn=AT" in capture["url"]
    assert capture["timeout"] == energy_charts._REQUEST_TIMEOUT_SECONDS
    # The request targets the v2 next-day endpoint, not the legacy /price endpoint for today.
    assert capture["url"].startswith(energy_charts.ENERGY_CHARTS_URL)
    assert "/v2/price_next_day" in capture["url"]


def test_a_network_error_is_a_connection_error_not_a_crash(monkeypatch):
    _patch_opener(monkeypatch, error=OSError("connection refused"))
    with pytest.raises(SpotMarketConnectionError):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_a_404_before_tomorrows_auction_is_not_available_yet(monkeypatch):
    error = HTTPError("https://api.energy-charts.info/v2/price_next_day", 404, "Not Found", {}, None)
    _patch_opener(monkeypatch, error=error)
    with pytest.raises(SpotMarketNotAvailableYet):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_current_price_404_is_still_a_provider_error(monkeypatch):
    error = HTTPError("https://api.energy-charts.info/v2/price_current", 404, "Not Found", {}, None)
    _patch_opener(monkeypatch, error=error)
    with pytest.raises(SpotMarketResponseError):
        EnergyChartsProvider().fetch_current(SpotMarketConfig())


def test_malformed_json_is_a_response_error(monkeypatch):
    _patch_opener(monkeypatch, raw=b"not json")
    with pytest.raises(SpotMarketResponseError):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_a_null_price_value_is_rejected_not_turned_into_a_fabricated_value(monkeypatch):
    # v2 encodes "not available" as a null value; the series must be complete, so a null is a hard
    # error, never a hole filled with an invented price.
    payload = _v2_payload(_STARTS, [None, 177.82, 176.58])
    _patch_opener(monkeypatch, payload=payload)
    with pytest.raises(SpotMarketResponseError, match="missing"):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_a_non_finite_price_is_not_turned_into_a_fabricated_value(monkeypatch):
    payload = _v2_payload(_STARTS[:2], [float("nan"), 1.0])
    _patch_opener(monkeypatch, payload=payload)
    with pytest.raises(SpotMarketResponseError):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_an_unknown_unit_is_rejected_rather_than_assumed(monkeypatch):
    _patch_opener(monkeypatch, payload=_v2_payload(_STARTS, [1.0, 2.0, 3.0], unit="USD/MWh"))
    with pytest.raises(SpotMarketResponseError):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_a_missing_price_series_is_rejected(monkeypatch):
    payload = _v2_payload(_STARTS, [1.0, 2.0, 3.0])
    payload["series"] = []  # no descriptor: the price key inside values is unknown
    _patch_opener(monkeypatch, payload=payload)
    with pytest.raises(SpotMarketResponseError, match="single price series"):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_an_empty_next_day_series_means_not_published_yet(monkeypatch):
    _patch_opener(monkeypatch, payload=_v2_payload([], []))
    with pytest.raises(SpotMarketNotAvailableYet):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def _fetch_day(provider, kind):
    config = SpotMarketConfig()
    if kind == "day_ahead":
        return provider.fetch_day_ahead(config)
    if kind == "today":
        return provider.fetch_today(config)
    return provider.fetch_yesterday(config, "Europe/Berlin")


@pytest.mark.parametrize("kind", ["day_ahead", "today", "yesterday"])
@pytest.mark.parametrize(
    ("starts", "match"),
    [
        (_day_starts(count=50), "complete local day"),  # starts at midnight, ends early
        (_day_starts()[4:], "complete local day"),  # starts after midnight
        (_day_starts()[:-1], "complete local day"),  # last slot missing
    ],
)
def test_a_partial_day_is_rejected(monkeypatch, kind, starts, match):
    _patch_opener(monkeypatch, payload=_v2_payload(starts, [1.0] * len(starts)))
    with pytest.raises(SpotMarketResponseError, match=match):
        _fetch_day(EnergyChartsProvider(), kind)


@pytest.mark.parametrize("kind", ["today", "yesterday"])
def test_an_empty_today_or_yesterday_series_is_a_response_error(monkeypatch, kind):
    _patch_opener(monkeypatch, payload=_v2_payload([], []))
    with pytest.raises(SpotMarketResponseError, match="no price intervals"):
        _fetch_day(EnergyChartsProvider(), kind)


@pytest.mark.parametrize("kind", ["day_ahead", "today"])
def test_a_series_without_a_timezone_cannot_prove_a_full_day(monkeypatch, kind):
    payload = _v2_payload(_day_starts(), [1.0] * 96)
    del payload["timezone"]
    _patch_opener(monkeypatch, payload=payload)
    with pytest.raises(SpotMarketResponseError, match="timezone"):
        _fetch_day(EnergyChartsProvider(), kind)


@pytest.mark.parametrize(("day", "count"), [("2026-03-29", 92), ("2026-10-25", 100)])
def test_dst_days_with_92_or_100_intervals_are_complete(monkeypatch, day, count):
    # Spring: 23 local hours; autumn: 25. Built from UTC instants so the offset flip is exact.
    zone = ZoneInfo("Europe/Berlin")
    midnight = datetime.fromisoformat(f"{day}T00:00:00").replace(tzinfo=zone).astimezone(UTC)
    starts = [(midnight + timedelta(minutes=15 * i)).astimezone(zone).isoformat() for i in range(count)]
    _patch_opener(monkeypatch, payload=_v2_payload(starts, [1.0] * count))
    snapshot = EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())
    assert len(snapshot.intervals) == count
    # One slot more or less is a different day boundary and must be rejected.
    _patch_opener(monkeypatch, payload=_v2_payload(starts[:-1], [1.0] * (count - 1)))
    with pytest.raises(SpotMarketResponseError, match="complete local day"):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_config_defaults_match_energy_charts_de_lu():
    config = SpotMarketConfig()
    assert config.enabled is False
    assert config.provider is SpotMarketProvider.ENERGY_CHARTS
    assert config.bidding_zone == "DE-LU"


def test_config_has_no_editable_api_url_field():
    # The operator cannot aim the provider at an arbitrary network target. The
    # endpoint is a provider-owned constant, not a config field, so posting one is a hard error.
    assert "api_url" not in SpotMarketConfig.model_fields
    with pytest.raises(ValueError):
        SpotMarketConfig(api_url="http://127.0.0.1/price")  # type: ignore[call-arg]


def test_config_has_no_operator_owned_currency_or_price_unit():
    # Currency and price unit are provider metadata, not operator input. They must not be
    # config fields, and smuggling one in through the model is a hard error (extra="forbid").
    assert "currency" not in SpotMarketConfig.model_fields
    assert "price_unit" not in SpotMarketConfig.model_fields
    with pytest.raises(ValueError):
        SpotMarketConfig(currency="USD")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        SpotMarketConfig(price_unit="EUR/MWh")  # type: ignore[call-arg]


def test_an_unknown_provider_is_rejected():
    with pytest.raises(ValueError):
        SpotMarketConfig.model_validate({"provider": "made_up"})


def test_an_empty_bidding_zone_is_rejected():
    with pytest.raises(ValueError):
        SpotMarketConfig(bidding_zone="   ")


def test_fee_region_and_bidding_zone_are_separate_fields():
    config = SpotMarketConfig(bidding_zone="DE-LU", fee_region={"code": "netze-bw", "label": "Netze BW"})
    assert config.bidding_zone == "DE-LU"
    assert config.fee_region.code == "netze-bw"
    # The two concepts never collapse into one value.
    assert config.fee_region.code != config.bidding_zone


def test_spot_price_interval_is_frozen():
    # A frozen dataclass raises FrozenInstanceError on attribute assignment; assert that
    # concrete error rather than a catch-all Exception.
    start = datetime.now(UTC)
    interval = SpotPriceInterval(start=start, end=start + timedelta(minutes=15), price_eur_per_kwh=Decimal("0.1"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        interval.price_eur_per_kwh = Decimal("0.2")  # type: ignore[misc]


def test_snapshot_rejects_invalid_timezone():
    snapshot = _snapshot((_interval(_T0, _T15),))
    assert dataclasses.replace(snapshot, timezone="Europe/Berlin").timezone == "Europe/Berlin"
    for invalid in ("Not/AZone", "", 123):
        with pytest.raises(ValueError, match="IANA zone"):
            dataclasses.replace(snapshot, timezone=invalid)


def test_snapshot_rejects_naive_availability_window():
    # available_from/available_until are optional, but when present they must be tz-aware instants,
    # like source_generated_at and valid_until; a naive datetime has no usable instant.
    snapshot = _snapshot((_interval(_T0, _T15),))
    naive = datetime(2026, 1, 1, 0, 0)  # noqa: DTZ001 - deliberately naive to prove it is rejected
    for field in ("available_from", "available_until"):
        with pytest.raises(ValueError, match="timezone-aware"):
            dataclasses.replace(snapshot, **{field: naive})
    # The tz-aware form of the same fields constructs without error.
    ok = dataclasses.replace(snapshot, available_from=_T0, available_until=_T15)
    assert ok.available_from == _T0 and ok.available_until == _T15


def test_the_endpoint_constant_is_a_fixed_https_origin():
    # The provider owns a fixed, trusted HTTPS endpoint; nothing operator-supplied reaches the URL.
    assert energy_charts.ENERGY_CHARTS_URL.startswith("https://")


def _redirect(source_url: str, target_url: str):
    """Drive the same-origin handler's redirect decision for a 302 from ``source`` to ``target``."""
    handler = energy_charts._SameOriginRedirectHandler()
    request = energy_charts.Request(source_url)
    return handler.redirect_request(request, io.BytesIO(b""), 302, "Found", {}, target_url)


def test_a_cross_origin_redirect_is_refused():
    # urllib follows 30x by default, so a redirect to an internal host would re-open the SSRF hole.
    with pytest.raises(HTTPError):
        _redirect("https://api.energy-charts.info/v2/price_next_day?bzn=DE-LU", "http://127.0.0.1/price")


def test_an_https_to_http_downgrade_redirect_is_refused():
    with pytest.raises(HTTPError):
        _redirect(
            "https://api.energy-charts.info/v2/price_next_day",
            "http://api.energy-charts.info/v2/price_next_day",
        )


def test_a_same_origin_redirect_is_allowed():
    # A redirect that stays on the same scheme/host/port is a normal relocation, not an escape.
    new_request = _redirect(
        "https://api.energy-charts.info/v2/price_next_day?bzn=DE-LU",
        "https://api.energy-charts.info/v2/price_next_day/alt?bzn=DE-LU",
    )
    assert new_request is not None
    assert new_request.full_url == "https://api.energy-charts.info/v2/price_next_day/alt?bzn=DE-LU"


def test_a_clean_fifteen_minute_series_parses(monkeypatch):
    _patch_opener(monkeypatch, payload=_PRICE_PAYLOAD)
    intervals = EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig()).intervals
    assert len(intervals) == 96
    assert all((item.end - item.start).total_seconds() == 900 for item in intervals)


def test_a_missing_slot_is_rejected_not_turned_into_a_thirty_minute_interval(monkeypatch):
    # 00:00, 00:15, 00:45 — the 00:30 slot is missing, which must raise, never become a 30-min slot.
    starts = ["2026-07-21T00:00:00+02:00", "2026-07-21T00:15:00+02:00", "2026-07-21T00:45:00+02:00"]
    _patch_opener(monkeypatch, payload=_v2_payload(starts, [1.0, 2.0, 3.0]))
    with pytest.raises(SpotMarketResponseError, match="15 minutes apart"):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_a_duplicate_timestamp_is_rejected(monkeypatch):
    starts = ["2026-07-21T00:00:00+02:00", "2026-07-21T00:00:00+02:00", "2026-07-21T00:15:00+02:00"]
    _patch_opener(monkeypatch, payload=_v2_payload(starts, [1.0, 2.0, 3.0]))
    with pytest.raises(SpotMarketResponseError, match="15 minutes apart"):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_an_unsorted_series_is_rejected(monkeypatch):
    starts = ["2026-07-21T00:15:00+02:00", "2026-07-21T00:00:00+02:00", "2026-07-21T00:30:00+02:00"]
    _patch_opener(monkeypatch, payload=_v2_payload(starts, [1.0, 2.0, 3.0]))
    with pytest.raises(SpotMarketResponseError, match="15 minutes apart"):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_a_different_resolution_is_rejected_by_the_per_pair_check(monkeypatch):
    # Hourly starts (3600s apart) are not the day-ahead 15-min feed. Even if interval_minutes were
    # consistent, the per-pair 900 s guard rejects them; here interval_minutes is omitted so the
    # per-pair check is the one that fires.
    starts = ["2026-07-21T00:00:00+02:00", "2026-07-21T01:00:00+02:00"]
    _patch_opener(monkeypatch, payload=_v2_payload(starts, [1.0, 2.0], interval_minutes=None))
    with pytest.raises(SpotMarketResponseError, match="15 minutes apart"):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_a_declared_non_fifteen_minute_resolution_is_rejected(monkeypatch):
    # When the envelope declares a non-15-minute resolution, it is rejected up front.
    _patch_opener(monkeypatch, payload=_v2_payload(_STARTS, [1.0, 2.0, 3.0], interval_minutes=60))
    with pytest.raises(SpotMarketResponseError, match="resolution"):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def test_a_naive_timestamp_is_rejected(monkeypatch):
    # A v2 timestamp without an offset has no usable instant; it is rejected, never assumed UTC.
    starts = ["2026-07-21T00:00:00", "2026-07-21T00:15:00"]
    _patch_opener(monkeypatch, payload=_v2_payload(starts, [1.0, 2.0]))
    with pytest.raises(SpotMarketResponseError, match="timezone-aware"):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


_T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_T15 = _T0 + timedelta(minutes=15)
_T30 = _T0 + timedelta(minutes=30)


def test_interval_rejects_a_naive_timestamp():
    naive = datetime(2026, 1, 1, 0, 0)  # noqa: DTZ001 - deliberately naive to prove it is rejected
    with pytest.raises(ValueError, match="timezone-aware"):
        SpotPriceInterval(start=naive, end=_T15, price_eur_per_kwh=Decimal("0.1"))


def test_interval_rejects_end_not_after_start():
    with pytest.raises(ValueError, match="after start"):
        SpotPriceInterval(start=_T15, end=_T0, price_eur_per_kwh=Decimal("0.1"))
    with pytest.raises(ValueError, match="after start"):
        SpotPriceInterval(start=_T0, end=_T0, price_eur_per_kwh=Decimal("0.1"))


def test_interval_rejects_a_non_finite_price():
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ValueError, match="finite"):
            SpotPriceInterval(start=_T0, end=_T15, price_eur_per_kwh=bad)


def test_a_valid_interval_constructs():
    interval = SpotPriceInterval(start=_T0, end=_T15, price_eur_per_kwh=Decimal("0.2"))
    assert interval.start == _T0 and interval.end == _T15


def _interval(start, end, price="0.1"):
    return SpotPriceInterval(start=start, end=end, price_eur_per_kwh=Decimal(price))


def _snapshot(intervals, fetched_at=_T0):
    return SpotPriceSnapshot(
        provider=SpotMarketProvider.ENERGY_CHARTS,
        bidding_zone="DE-LU",
        currency="EUR",
        fetched_at=fetched_at,
        intervals=intervals,
    )


def test_current_price_uses_dedicated_provider_endpoint(monkeypatch):
    start = datetime.now(UTC).replace(second=0, microsecond=0)
    start = start - timedelta(minutes=start.minute % 15)
    seen = {}
    _patch_opener(monkeypatch, payload=_v2_payload([start.isoformat()], [124.0]), capture=seen)
    snapshot = EnergyChartsProvider().fetch_current(SpotMarketConfig(enabled=True))
    assert "price_current" in seen["url"]
    assert snapshot.intervals[0].price_eur_per_kwh == Decimal("0.124")


def test_provider_retains_source_timestamps_and_timezone(monkeypatch):
    payload = _v2_payload(_STARTS[:1], [178.73])
    payload.update(
        generated_at="2026-07-20T13:00:00+02:00",
        available_from="2026-07-21T00:00:00+02:00",
        available_until="2026-07-22T00:00:00+02:00",
        valid_until="2026-07-21T00:15:00+02:00",
    )
    _patch_opener(monkeypatch, payload=payload)
    snapshot = EnergyChartsProvider().fetch_current(SpotMarketConfig())
    assert snapshot.timezone == "Europe/Berlin"
    assert snapshot.source_generated_at == datetime.fromisoformat(payload["generated_at"])
    assert snapshot.available_from == datetime.fromisoformat(payload["available_from"])
    assert snapshot.available_until == datetime.fromisoformat(payload["available_until"])
    assert snapshot.valid_until == datetime.fromisoformat(payload["valid_until"])


def test_provider_rejects_naive_source_timestamp(monkeypatch):
    payload = _v2_payload(_day_starts(), _day_prices([178.73, 177.82, 176.58]))
    payload["generated_at"] = "2026-07-20T13:00:00"
    _patch_opener(monkeypatch, payload=payload)
    with pytest.raises(SpotMarketResponseError, match="generated_at"):
        EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())


def _join_refresh_thread(timeout: float = 2.0) -> None:
    """Wait for the cache's background refresh thread to finish storing its result.

    ``SpotSnapshotCache.get`` spawns a daemon thread named ``spot-snapshot-refresh`` and only
    writes ``_snapshot``/``_failed`` once that thread returns from ``_refresh``. Joining the named
    thread is deterministic: it needs no ``refresh_seconds=0`` self-trigger loop, no polling of
    ``get()`` (which itself can start another refresh), and no reach into private cache state.
    """
    for thread in threading.enumerate():
        if thread.name == "spot-snapshot-refresh":
            thread.join(timeout)
            assert not thread.is_alive(), "refresh thread did not finish in time"


def test_dashboard_snapshot_cache_keeps_last_success_after_failure():
    good = _snapshot((_interval(_T0, _T15),))
    calls = 0

    def fetch(_config):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise SpotMarketConnectionError("unavailable")
        return good

    # A real (non-zero) refresh interval the test steps past explicitly, instead of
    # refresh_seconds=0 where every get() could kick off a fresh background refresh.
    cache = SpotSnapshotCache(fetch, refresh_seconds=60)
    config = SpotMarketConfig(enabled=True)
    # First get: no snapshot yet, triggers the first (successful) refresh.
    assert cache.get(config) == (None, False)
    _join_refresh_thread()
    assert cache.get(config) == (good, False)
    # Force the next refresh to run and fail; the last good snapshot must survive as (good, True).
    cache._next_fetch = 0.0
    cache.get(config)
    _join_refresh_thread()
    assert cache.get(config) == (good, True)
    assert cache.get(SpotMarketConfig(enabled=False)) == (None, False)


def test_unpublished_next_day_does_not_mark_dashboard_cache_failed():
    def fetch(_config):
        raise SpotMarketNotAvailableYet("not published")

    cache = SpotSnapshotCache(fetch, refresh_seconds=60)
    config = SpotMarketConfig(enabled=True)
    assert cache.get(config) == (None, False)
    _join_refresh_thread()
    # A not-yet-published next day is not a failure: the cache stays (None, False), never (None, True).
    assert cache.get(config) == (None, False)


def test_dashboard_snapshot_cache_returns_while_refresh_is_waiting():
    entered = threading.Event()
    release = threading.Event()
    good = _snapshot((_interval(_T0, _T15),))

    def fetch(_config):
        entered.set()
        assert release.wait(2)
        return good

    cache = SpotSnapshotCache(fetch, refresh_seconds=60)
    config = SpotMarketConfig(enabled=True)
    assert cache.get(config) == (None, False)
    assert entered.wait(1)
    assert cache.get(config) == (None, False)
    release.set()


def test_dashboard_snapshot_cache_does_not_overlap_config_refreshes():
    entered = {"DE-LU": threading.Event(), "AT": threading.Event()}
    release = threading.Event()
    calls = []

    def fetch(config):
        calls.append(config.bidding_zone)
        entered[config.bidding_zone].set()
        assert release.wait(2)
        return _snapshot((_interval(_T0, _T15),))

    cache = SpotSnapshotCache(fetch, refresh_seconds=60)
    original = SpotMarketConfig(enabled=True, bidding_zone="DE-LU")
    changed = SpotMarketConfig(enabled=True, bidding_zone="AT")
    # First get starts the DE-LU refresh; wait until it is actually running inside fetch().
    assert cache.get(original) == (None, False)
    assert entered["DE-LU"].wait(1)
    # Switching config while the DE-LU refresh runs must not start a second, overlapping refresh.
    assert cache.get(changed) == (None, False)
    assert calls == ["DE-LU"]
    # Let the DE-LU refresh finish; join its thread so the overlap guard is cleared deterministically.
    release.set()
    _join_refresh_thread()
    # The next get for the changed config starts exactly one AT refresh (no overlap, no skip).
    assert cache.get(changed) == (None, False)
    assert entered["AT"].wait(1)
    _join_refresh_thread()
    assert calls == ["DE-LU", "AT"]


def test_snapshot_rejects_a_naive_fetched_at():
    naive = datetime(2026, 1, 1, 0, 0)  # noqa: DTZ001 - deliberately naive
    with pytest.raises(ValueError, match="timezone-aware"):
        _snapshot((_interval(_T0, _T15),), fetched_at=naive)


def test_snapshot_rejects_overlapping_intervals():
    with pytest.raises(ValueError, match="ordered and non-overlapping"):
        _snapshot((_interval(_T0, _T30), _interval(_T15, _T30)))


def test_snapshot_rejects_unordered_intervals():
    with pytest.raises(ValueError, match="ordered and non-overlapping"):
        _snapshot((_interval(_T15, _T30), _interval(_T0, _T15)))


def test_a_valid_snapshot_constructs():
    snapshot = _snapshot((_interval(_T0, _T15), _interval(_T15, _T30)))
    assert len(snapshot.intervals) == 2


# ``PriceUnit`` stays part of the provider contract even though it is no longer operator config.
def test_price_unit_enum_still_models_both_source_units():
    assert PriceUnit.EUR_PER_KWH.value == "EUR/kWh"
    assert PriceUnit.EUR_PER_MWH.value == "EUR/MWh"


from app.spotmarket.base import SpotMarketError
from app.spotmarket.metadata import BiddingZoneMetadataCache
from app.spotmarket.models import BiddingZoneCatalog


def _catalog_payload(zones=("DE-LU", "AT", "FR"), default="DE-LU"):
    """A minimal GET /v2 catalog carrying the next-day endpoint and its bzn allowed_values."""
    bzn = {"name": "bzn", "type": "string", "required": False, "allowed_values": list(zones)}
    if default is not None:
        bzn["default"] = default
    return {
        "schema_version": "2.0",
        "title": "Energy-Charts API",
        "description": "Self-describing directory of the v2 API.",
        "openapi_url": "/openapi.json",
        "response_envelope": "TimeSeriesResponseV2",
        "endpoints": [
            {"path": "/v2/public_power", "method": "GET", "parameters": []},
            {"path": "/v2/price_next_day", "method": "GET", "parameters": [bzn]},
        ],
    }


def test_fetch_bidding_zones_reads_the_allowed_values_from_the_catalog(monkeypatch):
    _patch_opener(monkeypatch, payload=_catalog_payload())
    catalog = EnergyChartsProvider().fetch_bidding_zones()
    assert catalog.zones == ("DE-LU", "AT", "FR")
    assert catalog.default == "DE-LU"
    assert catalog.fetched_at.tzinfo is not None


def test_fetch_bidding_zones_requests_the_v2_catalog_endpoint(monkeypatch):
    capture: dict = {}
    _patch_opener(monkeypatch, payload=_catalog_payload(), capture=capture)
    EnergyChartsProvider().fetch_bidding_zones()
    assert capture["url"] == energy_charts._CATALOG_URL
    assert capture["url"].endswith("/v2")


def test_a_catalog_without_the_next_day_endpoint_is_a_response_error(monkeypatch):
    payload = _catalog_payload()
    payload["endpoints"] = [{"path": "/v2/public_power", "method": "GET", "parameters": []}]
    _patch_opener(monkeypatch, payload=payload)
    with pytest.raises(SpotMarketResponseError, match="next-day"):
        EnergyChartsProvider().fetch_bidding_zones()


def test_a_catalog_with_no_zones_is_a_response_error(monkeypatch):
    _patch_opener(monkeypatch, payload=_catalog_payload(zones=(), default=None))
    with pytest.raises(SpotMarketResponseError, match="no bidding zones"):
        EnergyChartsProvider().fetch_bidding_zones()


def test_a_default_outside_the_zone_list_falls_back_to_de_lu(monkeypatch):
    # A default the provider does not actually list must not be returned; DE-LU is used when present.
    _patch_opener(monkeypatch, payload=_catalog_payload(zones=("DE-LU", "AT"), default="XX"))
    catalog = EnergyChartsProvider().fetch_bidding_zones()
    assert catalog.default == "DE-LU"


def _http_error(code, headers=None):
    return HTTPError("https://api.energy-charts.info/v2/price_next_day", code, "err", headers or {}, None)


def test_a_hard_timeout_is_passed_to_every_request(monkeypatch):
    capture: dict = {}
    _patch_opener(monkeypatch, payload=_PRICE_PAYLOAD, capture=capture)
    EnergyChartsProvider().fetch_day_ahead(SpotMarketConfig())
    assert capture["timeout"] == energy_charts._REQUEST_TIMEOUT_SECONDS
    assert capture["timeout"] is not None and capture["timeout"] > 0


def test_a_429_is_retried_then_succeeds_and_honours_retry_after(monkeypatch):
    slept: list[float] = []
    calls = {"n": 0}

    def fake_open(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, {"Retry-After": "2"})
        return _FakeResponse(json.dumps(_PRICE_PAYLOAD).encode())

    monkeypatch.setattr(energy_charts._OPENER, "open", fake_open)
    provider = EnergyChartsProvider(sleep=slept.append)
    snapshot = provider.fetch_day_ahead(SpotMarketConfig())
    assert len(snapshot.intervals) == 96
    assert calls["n"] == 2
    assert slept == [2.0]  # the Retry-After delay was honoured exactly once


def test_a_persistent_429_gives_up_after_a_bounded_number_of_attempts(monkeypatch):
    slept: list[float] = []
    calls = {"n": 0}

    def fake_open(request, timeout=None):
        calls["n"] += 1
        raise _http_error(429, {"Retry-After": "1"})

    monkeypatch.setattr(energy_charts._OPENER, "open", fake_open)
    provider = EnergyChartsProvider(sleep=slept.append)
    with pytest.raises(SpotMarketConnectionError, match="unavailable"):
        provider.fetch_day_ahead(SpotMarketConfig())
    # Bounded: exactly _MAX_ATTEMPTS calls, with one sleep fewer than attempts; never an endless loop.
    assert calls["n"] == energy_charts._MAX_ATTEMPTS
    assert len(slept) == energy_charts._MAX_ATTEMPTS - 1


def test_a_hostile_retry_after_is_clamped(monkeypatch):
    slept: list[float] = []
    calls = {"n": 0}

    def fake_open(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, {"Retry-After": "99999"})
        return _FakeResponse(json.dumps(_PRICE_PAYLOAD).encode())

    monkeypatch.setattr(energy_charts._OPENER, "open", fake_open)
    EnergyChartsProvider(sleep=slept.append).fetch_day_ahead(SpotMarketConfig())
    assert slept == [float(MAX_RETRY_AFTER_SECONDS)]


def test_a_404_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def fake_open(request, timeout=None):
        calls["n"] += 1
        raise _http_error(404)

    monkeypatch.setattr(energy_charts._OPENER, "open", fake_open)
    with pytest.raises(SpotMarketNotAvailableYet):
        EnergyChartsProvider(sleep=lambda _s: None).fetch_day_ahead(SpotMarketConfig())
    assert calls["n"] == 1


def test_a_timeout_is_a_connection_error(monkeypatch):
    _patch_opener(monkeypatch, error=TimeoutError("timed out"))
    with pytest.raises(SpotMarketConnectionError):
        EnergyChartsProvider(sleep=lambda _s: None).fetch_day_ahead(SpotMarketConfig())


_CACHE_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _catalog(zones=("DE-LU", "AT"), default="DE-LU", fetched_at=_CACHE_T0):
    return BiddingZoneCatalog(zones=zones, default=default, fetched_at=fetched_at)


def test_cache_get_never_fetches_and_refresh_stores_the_catalog():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return _catalog(fetched_at=_CACHE_T0)

    cache = BiddingZoneMetadataCache(fetch)
    with pytest.raises(SpotMarketError):
        cache.get()  # nothing stored yet: no provider request is made to fill the gap
    assert calls["n"] == 0
    refreshed = cache.refresh()
    assert refreshed.zones == ("DE-LU", "AT") and refreshed.stale is False
    for _ in range(3):
        assert cache.get().zones == ("DE-LU", "AT")
    assert calls["n"] == 1


def test_cache_keeps_the_last_good_list_as_stale_when_a_refresh_fails():
    state = {"first": True}

    def fetch():
        if state["first"]:
            state["first"] = False
            return _catalog(fetched_at=_CACHE_T0)
        raise SpotMarketError("rate limited")

    cache = BiddingZoneMetadataCache(fetch)
    good = cache.refresh()
    assert good.stale is False
    stale = cache.refresh()
    assert stale.stale is True and cache.get().stale is True
    assert stale.zones == good.zones  # the last good list is kept, never fabricated or blanked


def test_cache_refresh_propagates_a_failure_when_nothing_was_ever_stored():
    def fetch():
        raise SpotMarketError("down")

    with pytest.raises(SpotMarketError):
        BiddingZoneMetadataCache(fetch).refresh()


def test_zone_list_survives_a_restart_through_the_admin_database(tmp_path):
    from app.admin.store import AdminStore
    from app.spotmarket.metadata import zone_catalog_persistence

    def open_store():
        store = AdminStore(tmp_path / "admin.db", "f" * 48)
        store.initialize()
        return store

    store = open_store()
    load, save = zone_catalog_persistence(store)
    BiddingZoneMetadataCache(lambda: _catalog(fetched_at=_CACHE_T0), load=load, save=save).refresh()
    store.close()

    def unreachable():
        raise AssertionError("a restarted cache must not fetch")

    store = open_store()
    load, save = zone_catalog_persistence(store)
    restarted = BiddingZoneMetadataCache(unreachable, load=load, save=save)
    listing = restarted.get()
    assert listing.zones == ("DE-LU", "AT") and listing.stale is False and listing.last_update == _CACHE_T0
    store.close()


def test_bidding_zone_catalog_rejects_a_default_not_in_the_list():
    with pytest.raises(ValueError, match="default"):
        BiddingZoneCatalog(zones=("DE-LU",), default="AT", fetched_at=_CACHE_T0)


@pytest.mark.parametrize(
    ("zones", "default"),
    [(("DE-LU", ""), "DE-LU"), (("DE-LU", 5), "DE-LU"), (["DE-LU"], "DE-LU"), (("DE-LU",), ""), (("",), "")],
)
def test_bidding_zone_catalog_rejects_empty_or_non_string_entries(zones, default):
    with pytest.raises(ValueError):
        BiddingZoneCatalog(zones=zones, default=default, fetched_at=_CACHE_T0)


@pytest.mark.parametrize(
    "stored",
    [
        {"zones": ["DE-LU", ""], "default": "DE-LU", "fetched_at": _CACHE_T0.isoformat()},
        {"zones": ["DE-LU"], "default": 7, "fetched_at": _CACHE_T0.isoformat()},
        {"zones": ["DE-LU"], "default": "DE-LU", "fetched_at": "not-a-date"},
        {"zones": ["DE-LU"]},
    ],
)
def test_a_damaged_stored_zone_catalog_is_ignored_and_refetched(tmp_path, stored):
    from app.admin.store import AdminStore
    from app.spotmarket.metadata import ZONE_CATALOG_KEY, zone_catalog_persistence

    store = AdminStore(tmp_path / "admin.db", "f" * 48)
    store.initialize()
    store.put(ZONE_CATALOG_KEY, stored)
    load, save = zone_catalog_persistence(store)
    cache = BiddingZoneMetadataCache(lambda: _catalog(fetched_at=_CACHE_T0), load=load, save=save)
    with pytest.raises(SpotMarketError):
        cache.get()  # the damaged copy is not served
    assert cache.refresh().zones == ("DE-LU", "AT")
    store.close()


def test_get_does_not_block_while_a_refresh_is_fetching():
    entered, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    def slow_fetch():
        calls["n"] += 1
        if calls["n"] == 2:
            entered.set()
            assert release.wait(5)
        return _catalog(fetched_at=_CACHE_T0)

    cache = BiddingZoneMetadataCache(slow_fetch)
    cache.refresh()
    worker = threading.Thread(target=cache.refresh)
    worker.start()
    try:
        assert entered.wait(5)
        result: list = []
        reader = threading.Thread(target=lambda: result.append(cache.get()))
        reader.start()
        reader.join(2)
        assert not reader.is_alive(), "get() blocked behind the running refresh"
        assert result[0].zones == ("DE-LU", "AT")
    finally:
        release.set()
        worker.join(5)
