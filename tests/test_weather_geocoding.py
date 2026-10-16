#!/usr/bin/env python3
#
# tests/test_weather_geocoding.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Geocoding clients for the Weather tab's location search (Photon default, Open-Meteo legacy).

No test reaches a live provider: the one outbound call is monkeypatched at the shared transport
opener (``http_json._OPENER``). The tests pin the URL/query contract, the fixed HTTPS origin and
cross-origin-redirect refusal, the GeoJSON mapping (including the [lon, lat] order), the country
filter as a real request parameter, rejection of an invalid explicit country code, the exact
postal-code match, the empty-results and malformed-record handling, the response-size cap and the
interactive timeout and no-retry budget, while the forecast provider retains its retry policy.
"""

import io
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from app.weather import geocoding, http_json
from app.weather.base import WeatherConnectionError, WeatherResponseError
from app.weather.geocoding import OpenMeteoGeocoder, PhotonGeocoder


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _patch_opener(monkeypatch, *, payload=None, raw=None, error=None, capture=None, errors=None):
    sequence = list(errors) if errors is not None else []

    def fake_open(request, timeout=None):
        if capture is not None:
            capture["url"] = request.full_url
            capture["timeout"] = timeout
            capture["calls"] = capture.get("calls", 0) + 1
        if sequence:
            raise sequence.pop(0)
        if error is not None:
            raise error
        body = raw if raw is not None else json.dumps(payload).encode()
        return _FakeResponse(body)

    monkeypatch.setattr(http_json._OPENER, "open", fake_open)


# --- Photon GeoJSON fixtures -------------------------------------------------


def _feature(**properties):
    """Build one Photon GeoJSON feature. ``lon``/``lat`` override the coordinate pair; everything
    else is a ``properties`` entry.
    """
    lon = properties.pop("lon", 7.1925)
    lat = properties.pop("lat", 51.1798)
    base = {"name": "Remscheid", "country": "Germany", "countrycode": "DE", "state": "North Rhine-Westphalia"}
    base.update(properties)
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": base}


def _photon(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def test_a_too_short_query_makes_no_request(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(http_json._OPENER, "open", fail)
    assert PhotonGeocoder().search("R") == []


def test_request_targets_the_fixed_photon_origin_with_expected_parameters(monkeypatch):
    capture = {}
    _patch_opener(monkeypatch, payload=_photon(_feature()), capture=capture)
    PhotonGeocoder().search("Remscheid")
    url = capture["url"]
    assert url.startswith("https://photon.komoot.io/api?")
    assert "q=Remscheid" in url
    assert f"limit={geocoding.MAX_RESULTS}" in url
    assert "lang=en" in url
    # No country filter was requested, so the search stays global.
    assert "countrycode" not in url
    assert capture["timeout"] == geocoding._REQUEST_TIMEOUT_SECONDS


def test_a_cross_origin_redirect_is_refused(monkeypatch):
    def fake_open(request, timeout=None):
        raise HTTPError(request.full_url, 302, "redirect", {}, None)

    monkeypatch.setattr(http_json._OPENER, "open", fake_open)
    with pytest.raises(WeatherResponseError):
        PhotonGeocoder().search("Remscheid")


# --- Shared SSRF redirect guard (SameOriginRedirectHandler) ------------------
#
# The transport's redirect handler is the P1 SSRF guard: urllib follows 30x by default, so a fixed
# trusted origin could otherwise be bounced to an internal host or downgraded to http. These drive
# the shared handler subclass directly (the one http_json builds into _OPENER) rather than through
# a geocoder, so the same-origin/cross-origin/downgrade decision stays covered on its own.


def _redirect(handler, source_url, target_url):
    request = Request(source_url)
    return handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, target_url)


def test_a_same_origin_redirect_is_allowed():
    # Same scheme, host and port: the handler delegates to urllib and yields a follow-up request to
    # the new path rather than refusing it.
    handler = http_json._SameOriginRedirectHandler()
    follow_up = _redirect(handler, "https://photon.komoot.io/api?q=Rem", "https://photon.komoot.io/api/v2?q=Rem")
    assert isinstance(follow_up, Request)
    assert follow_up.full_url == "https://photon.komoot.io/api/v2?q=Rem"


def test_a_cross_origin_redirect_is_refused_by_the_handler():
    # A redirect to a different host must raise, never reach the internal/other target.
    handler = http_json._SameOriginRedirectHandler()
    with pytest.raises(HTTPError):
        _redirect(handler, "https://photon.komoot.io/api?q=Rem", "https://127.0.0.1/api?q=Rem")


def test_an_https_to_http_downgrade_redirect_is_refused_by_the_handler():
    # Same host but a scheme downgrade (https -> http) differs in origin and must be refused.
    handler = http_json._SameOriginRedirectHandler()
    with pytest.raises(HTTPError):
        _redirect(handler, "https://photon.komoot.io/api?q=Rem", "http://photon.komoot.io/api?q=Rem")


def test_features_are_mapped_from_geojson(monkeypatch):
    payload = _photon(
        _feature(name="Remscheid", city="Remscheid", county="Remscheid"),
        _feature(name="Remagen", state="Rhineland-Palatinate", lon=7.227, lat=50.579),
    )
    _patch_opener(monkeypatch, payload=payload)
    results = PhotonGeocoder().search("Rem")
    assert [r.name for r in results] == ["Remscheid", "Remagen"]
    first = results[0]
    # Coordinates come from the [lon, lat] pair in GeoJSON order.
    assert first.latitude == pytest.approx(51.1798)
    assert first.longitude == pytest.approx(7.1925)
    assert first.country == "Germany"
    assert first.country_code == "de"
    assert first.admin1 == "North Rhine-Westphalia"
    assert first.admin2 == "Remscheid"
    assert first.city == "Remscheid"
    # Photon carries no IANA timezone, so a hit never invents one.
    assert first.timezone is None


def test_lon_lat_order_is_not_swapped(monkeypatch):
    # A feature far from the equator/meridian proves the mapper reads coordinates[0]=lon,[1]=lat.
    _patch_opener(monkeypatch, payload=_photon(_feature(name="Toronto", lon=-79.3832, lat=43.6532, countrycode="CA")))
    hit = PhotonGeocoder().search("Toronto")[0]
    assert hit.latitude == pytest.approx(43.6532)
    assert hit.longitude == pytest.approx(-79.3832)


@pytest.mark.parametrize(
    ("code", "place", "countrycode"),
    [
        ("42899", "Remscheid", "DE"),
        ("80331", "München", "DE"),
        ("SW1A 1AA", "London", "GB"),
        ("10001", "New York", "US"),
        ("75008", "Paris", "FR"),
        ("M5V 3L9", "Toronto", "CA"),
    ],
)
def test_international_postal_codes_need_no_country_specific_code(monkeypatch, code, place, countrycode):
    # Numeric, alphabetic and alphanumeric-with-space codes are all sent verbatim as the free q
    # query; the application never classifies them with a country-specific regex.
    from urllib.parse import urlencode

    capture = {}
    payload = _photon(_feature(name=place, postcode=code, countrycode=countrycode))
    _patch_opener(monkeypatch, payload=payload, capture=capture)
    hit = PhotonGeocoder().search(code)[0]
    # The raw code is sent verbatim as q (urlencoded), with no country-specific parsing.
    assert urlencode([("q", code)]) in capture["url"]
    assert hit.name == place
    # The query matched the place's postcode exactly, so the canonical postal_code is surfaced.
    assert hit.postal_code == code
    assert hit.country_code == countrycode.lower()


def test_a_globally_ambiguous_code_yields_several_distinguishable_candidates(monkeypatch):
    # The same code exists in several countries; each candidate is distinct by country/country_code
    # so the operator can tell them apart. No country is assumed.
    payload = _photon(
        _feature(name="Someplace", postcode="1000", country="Belgium", countrycode="BE", state="Brussels"),
        _feature(name="Otherplace", postcode="1000", country="Denmark", countrycode="DK", state="Capital Region"),
    )
    _patch_opener(monkeypatch, payload=payload)
    results = PhotonGeocoder().search("1000")
    assert {r.country_code for r in results} == {"be", "dk"}
    assert all(r.postal_code == "1000" for r in results)


def test_an_explicit_country_filter_reaches_the_request(monkeypatch):
    capture = {}
    _patch_opener(monkeypatch, payload=_photon(_feature()), capture=capture)
    PhotonGeocoder().search("Berlin", country_code="DE")
    assert "countrycode=de" in capture["url"]


def test_an_invalid_explicit_country_code_is_rejected_not_silently_global(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("an invalid explicit country code must be rejected before any request")

    monkeypatch.setattr(http_json._OPENER, "open", fail)
    with pytest.raises(WeatherResponseError):
        PhotonGeocoder().search("Berlin", country_code="Germany")


def test_the_exact_postal_code_match_wins_over_other_listed_codes(monkeypatch):
    # The place carries one postcode; a query for exactly that code surfaces it, a name query does
    # not fabricate a postal_code match.
    _patch_opener(monkeypatch, payload=_photon(_feature(name="Titusville", postcode="32781", countrycode="US")))
    assert PhotonGeocoder().search("32781")[0].postal_code == "32781"


def test_a_place_name_query_matches_no_postal_code(monkeypatch):
    _patch_opener(monkeypatch, payload=_photon(_feature(name="Remscheid", postcode="42857")))
    hit = PhotonGeocoder().search("Remscheid")[0]
    # The query is a name, so there is no exact postcode match; the place's own code is not surfaced
    # (a name search must not show an arbitrary one of the place's many postcodes).
    assert hit.postal_code is None


def test_an_exact_postcode_match_prefers_the_postal_city_over_the_osm_object_name(monkeypatch):
    # The reported bug: for an exact postcode hit Photon's ``name`` is the matched OSM object, which
    # for 42899 is the administrative area "Nordrhein-Westfalen", not the postal city. The adapter
    # must title the hit with Photon's ``city`` ("Remscheid"), not that administrative name, while
    # keeping the coordinate inside the postcode area unchanged.
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [7.2336, 51.2088]},
        "properties": {
            "name": "Nordrhein-Westfalen", "postcode": "42899", "city": "Remscheid",
            "district": "Lüttringhausen", "state": "Nordrhein-Westfalen",
            "country": "Germany", "countrycode": "DE",
        },
    }
    _patch_opener(monkeypatch, payload=_photon(feature))
    hit = PhotonGeocoder().search("42899")[0]
    assert hit.name == "Remscheid"
    assert hit.district == "Lüttringhausen"
    assert hit.admin1 == "Nordrhein-Westfalen"
    assert hit.postal_code == "42899"
    # The coordinate the provider returned for the postcode area is kept verbatim.
    assert hit.latitude == pytest.approx(51.2088)
    assert hit.longitude == pytest.approx(7.2336)


def test_an_exact_postcode_match_falls_back_to_district_when_city_is_absent(monkeypatch):
    # Priority is city -> district -> name on an exact postcode hit, so a hit carrying only a
    # district titles with the district rather than the administrative object name.
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [7.2336, 51.2088]},
        "properties": {
            "name": "Nordrhein-Westfalen", "postcode": "42899",
            "district": "Lüttringhausen", "state": "Nordrhein-Westfalen", "countrycode": "DE",
        },
    }
    _patch_opener(monkeypatch, payload=_photon(feature))
    hit = PhotonGeocoder().search("42899")[0]
    assert hit.name == "Lüttringhausen"
    assert hit.district == "Lüttringhausen"


@pytest.mark.parametrize(
    ("code", "city", "countrycode"),
    [
        ("42899", "Remscheid", "DE"),
        ("SW1A 1AA", "London", "GB"),
        ("M5V 3L9", "Toronto", "CA"),
    ],
)
def test_exact_postcode_hits_title_with_the_city_not_the_region(monkeypatch, code, city, countrycode):
    # Regression for the geocoding-mapping defect across formats (numeric, alphabetic-with-space,
    # alphanumeric): when Photon returns the region as ``name`` and the postal city separately, an
    # exact postcode query surfaces the city, with no country-specific parsing.
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [7.0, 51.0]},
        "properties": {
            "name": "Some Region", "postcode": code, "city": city,
            "state": "Some Region", "countrycode": countrycode,
        },
    }
    _patch_opener(monkeypatch, payload=_photon(feature))
    hit = PhotonGeocoder().search(code)[0]
    assert hit.name == city
    assert hit.postal_code == code
    assert hit.country_code == countrycode.lower()


def test_a_postcode_only_feature_falls_back_to_the_code_as_name(monkeypatch):
    feature = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [7.19, 51.18]},
               "properties": {"postcode": "42899", "country": "Germany", "countrycode": "DE"}}
    _patch_opener(monkeypatch, payload=_photon(feature))
    hit = PhotonGeocoder().search("42899")[0]
    assert hit.name == "42899"
    assert hit.postal_code == "42899"


def test_an_empty_feature_collection_is_no_matches(monkeypatch):
    _patch_opener(monkeypatch, payload=_photon())
    assert PhotonGeocoder().search("zzzzzz") == []


def test_a_missing_features_key_is_no_matches(monkeypatch):
    _patch_opener(monkeypatch, payload={"type": "FeatureCollection"})
    assert PhotonGeocoder().search("zzzzzz") == []


def test_a_non_list_features_value_is_a_response_error(monkeypatch):
    _patch_opener(monkeypatch, payload={"features": "nope"})
    with pytest.raises(WeatherResponseError):
        PhotonGeocoder().search("Rem")


def test_a_feature_without_name_or_coordinates_is_skipped(monkeypatch):
    payload = {"features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [7.0, 51.0]}, "properties": {}},  # no name
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [7.0]}, "properties": {"name": "x"}},  # bad coords
        _feature(name="Valid"),
    ]}
    _patch_opener(monkeypatch, payload=payload)
    assert [r.name for r in PhotonGeocoder().search("Rem")] == ["Valid"]


def test_a_feature_with_non_finite_coordinates_is_skipped(monkeypatch):
    bad = {"type": "Feature", "geometry": {"type": "Point", "coordinates": ["nan", 51.0]},
           "properties": {"name": "Broken"}}
    _patch_opener(monkeypatch, payload={"features": [bad, _feature(name="Valid")]})
    assert [r.name for r in PhotonGeocoder().search("Rem")] == ["Valid"]


def test_a_feature_with_out_of_range_coordinates_is_skipped(monkeypatch):
    _patch_opener(monkeypatch, payload=_photon(_feature(name="Bad", lat=120.0), _feature(name="Valid")))
    assert [r.name for r in PhotonGeocoder().search("Rem")] == ["Valid"]


def test_a_malformed_feature_is_skipped(monkeypatch):
    _patch_opener(monkeypatch, payload={"features": ["not-a-dict", _feature(name="Valid")]})
    assert [r.name for r in PhotonGeocoder().search("Rem")] == ["Valid"]


def test_results_are_limited(monkeypatch):
    _patch_opener(monkeypatch, payload=_photon(*[_feature(name=f"City{i}") for i in range(50)]))
    assert len(PhotonGeocoder().search("City", count=3)) == 3


def test_identical_queries_are_served_from_the_ttl_cache(monkeypatch):
    calls = {"n": 0}

    def counting_open(request, timeout=None):
        calls["n"] += 1
        return _FakeResponse(json.dumps(_photon(_feature())).encode())

    monkeypatch.setattr(http_json._OPENER, "open", counting_open)
    geocoder = PhotonGeocoder()
    first = geocoder.search("Remscheid")
    # A case/whitespace variant of the same query hits the cache, not the network.
    second = geocoder.search("  remscheid ")
    assert calls["n"] == 1
    assert [r.name for r in first] == [r.name for r in second]


def test_the_country_filter_is_part_of_the_cache_key(monkeypatch):
    calls = {"n": 0}

    def counting_open(request, timeout=None):
        calls["n"] += 1
        return _FakeResponse(json.dumps(_photon(_feature())).encode())

    monkeypatch.setattr(http_json._OPENER, "open", counting_open)
    geocoder = PhotonGeocoder()
    geocoder.search("Berlin")
    # A country-scoped search for the same query is a different key, so it is fetched separately.
    geocoder.search("Berlin", country_code="DE")
    assert calls["n"] == 2


def test_an_oversized_response_is_a_response_error(monkeypatch):
    _patch_opener(monkeypatch, raw=b"x" * (geocoding._MAX_RESPONSE_BYTES + 2))
    with pytest.raises(WeatherResponseError):
        PhotonGeocoder().search("Remscheid")


def test_invalid_json_is_a_response_error(monkeypatch):
    _patch_opener(monkeypatch, raw=b"not json")
    with pytest.raises(WeatherResponseError):
        PhotonGeocoder().search("Remscheid")


def test_an_over_long_query_is_rejected_not_truncated(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("an over-long query must be rejected before any network call")

    monkeypatch.setattr(http_json._OPENER, "open", fail)
    with pytest.raises(WeatherResponseError):
        PhotonGeocoder().search("x" * (geocoding.MAX_QUERY_LENGTH + 1))


def test_a_timeout_is_a_weather_connection_error(monkeypatch):
    _patch_opener(monkeypatch, error=TimeoutError("timed out"))
    with pytest.raises(WeatherConnectionError):
        PhotonGeocoder().search("Remscheid")


def test_a_connection_error_is_a_weather_connection_error(monkeypatch):
    _patch_opener(monkeypatch, error=URLError("name resolution failed"))
    with pytest.raises(WeatherConnectionError):
        PhotonGeocoder().search("Remscheid")


def test_a_4xx_is_a_response_error(monkeypatch):
    _patch_opener(monkeypatch, error=HTTPError("u", 400, "bad request", {}, None))
    with pytest.raises(WeatherResponseError):
        PhotonGeocoder().search("Remscheid")


def test_interactive_lookup_does_not_wait_on_retry_after(monkeypatch):
    slept = []
    _patch_opener(
        monkeypatch,
        payload=_photon(_feature()),
        errors=[HTTPError("u", 429, "slow down", {"Retry-After": "30"}, None)],
    )
    with pytest.raises(WeatherConnectionError):
        PhotonGeocoder(sleep=slept.append).search("Remscheid")
    assert slept == []


def test_interactive_lookup_does_not_retry_5xx(monkeypatch):
    errors = [HTTPError("u", 503, "down", {}, None)]
    capture = {}
    _patch_opener(monkeypatch, errors=errors, capture=capture)
    with pytest.raises(WeatherConnectionError):
        PhotonGeocoder(sleep=lambda _s: None).search("Remscheid")
    assert capture["calls"] == 1


# --- Shared TTL cache --------------------------------------------------------


def test_an_expired_cache_entry_is_refetched(monkeypatch):
    clock = {"t": 1000.0}
    cache = geocoding._TtlCache(max_entries=8, ttl_seconds=60, clock=lambda: clock["t"])
    calls = {"n": 0}

    def counting_open(request, timeout=None):
        calls["n"] += 1
        return _FakeResponse(json.dumps(_photon(_feature())).encode())

    monkeypatch.setattr(http_json._OPENER, "open", counting_open)
    geocoder = PhotonGeocoder(cache=cache)
    geocoder.search("Remscheid")
    clock["t"] += 61  # past the TTL
    geocoder.search("Remscheid")
    assert calls["n"] == 2


def test_the_cache_is_bounded_in_size():
    cache = geocoding._TtlCache(max_entries=2, ttl_seconds=60)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)  # evicts the least-recently-used "a"
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


# --- Open-Meteo legacy adapter ----------------------------------------------
#
# Open-Meteo stays available as an alternative geocoder. These tests pin that it applies the
# countryCode filter, rejects an invalid explicit code, and only claims a postal_code on an exact
# query match (the postcodes[0] defect fix) — never surfacing an arbitrary code from the list.


def _om_result(**overrides):
    base = {
        "name": "Remscheid", "latitude": 51.1798, "longitude": 7.1925,
        "country": "Germany", "country_code": "DE", "admin1": "North Rhine-Westphalia",
        "timezone": "Europe/Berlin",
    }
    base.update(overrides)
    return base


def test_open_meteo_targets_its_fixed_origin(monkeypatch):
    capture = {}
    _patch_opener(monkeypatch, payload={"results": [_om_result()]}, capture=capture)
    OpenMeteoGeocoder().search("Remscheid")
    assert capture["url"].startswith("https://geocoding-api.open-meteo.com/v1/search?")
    assert "name=Remscheid" in capture["url"]
    assert capture["timeout"] == geocoding.LOOKUP_TIMEOUT_SECONDS


def test_open_meteo_applies_the_country_filter(monkeypatch):
    capture = {}
    _patch_opener(monkeypatch, payload={"results": [_om_result()]}, capture=capture)
    OpenMeteoGeocoder().search("Berlin", country_code="de")
    # Open-Meteo expects the upper-cased alpha-2 code.
    assert "countryCode=DE" in capture["url"]


def test_open_meteo_rejects_an_invalid_explicit_country_code(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("an invalid explicit country code must be rejected before any request")

    monkeypatch.setattr(http_json._OPENER, "open", fail)
    with pytest.raises(WeatherResponseError):
        OpenMeteoGeocoder().search("Berlin", country_code="XYZ")


def test_open_meteo_sets_postal_code_only_on_an_exact_query_match(monkeypatch):
    # The defect: querying 32781 against ["32780","32781"] must surface 32781, never postcodes[0].
    payload = {"results": [_om_result(name="Titusville", postcodes=["32780", "32781"])]}
    _patch_opener(monkeypatch, payload=payload)
    assert OpenMeteoGeocoder().search("32781")[0].postal_code == "32781"


def test_open_meteo_name_query_has_no_postal_code(monkeypatch):
    payload = {"results": [_om_result(name="Titusville", postcodes=["32780", "32781"])]}
    _patch_opener(monkeypatch, payload=payload)
    assert OpenMeteoGeocoder().search("Titusville")[0].postal_code is None


def test_open_meteo_malformed_postcodes_leave_postal_code_none(monkeypatch):
    _patch_opener(monkeypatch, payload={"results": [_om_result(postcodes="not-a-list")]})
    assert OpenMeteoGeocoder().search("42857")[0].postal_code is None


def test_open_meteo_malformed_timezone_is_dropped(monkeypatch):
    _patch_opener(monkeypatch, payload={"results": [_om_result(timezone="Not/AZone")]})
    assert OpenMeteoGeocoder().search("Rem")[0].timezone is None


def test_open_meteo_empty_results_is_no_matches(monkeypatch):
    _patch_opener(monkeypatch, payload={"generationtime_ms": 0.1})
    assert OpenMeteoGeocoder().search("zzzzzz") == []


# --- Timezone resolution from coordinates ------------------------------------
#
# The Weather tab's timezone is always derived from the geolocation, provider-independent: Photon
# (the default geocoder) carries no zone, so the zone is resolved server-side from lat/lon through
# Open-Meteo's keyfree ``timezone=auto`` endpoint over the SAME hardened transport. No test reaches
# a live provider: the one outbound call is monkeypatched at ``http_json._OPENER``.


from app.weather.timezone import TimezoneResolver


def test_timezone_request_targets_the_fixed_open_meteo_forecast_origin(monkeypatch):
    capture = {}
    _patch_opener(monkeypatch, payload={"timezone": "Europe/Berlin"}, capture=capture)
    TimezoneResolver().resolve(51.1798, 7.1925)
    url = capture["url"]
    assert url.startswith("https://api.open-meteo.com/v1/forecast?")
    assert "timezone=auto" in url
    assert "latitude=51.179800" in url
    assert "longitude=7.192500" in url
    assert capture["timeout"] == http_json.REQUEST_TIMEOUT_SECONDS


def test_timezone_is_resolved_for_known_coordinates(monkeypatch):
    # Remscheid 51.18/7.19 resolves to Europe/Berlin; the resolver returns the provider's zone.
    _patch_opener(monkeypatch, payload={"timezone": "Europe/Berlin"})
    assert TimezoneResolver().resolve(51.18, 7.19) == "Europe/Berlin"


def test_timezone_is_resolved_for_a_second_zone(monkeypatch):
    # A location in a different zone proves the zone follows the coordinates, not a fixed default.
    _patch_opener(monkeypatch, payload={"timezone": "America/New_York"})
    assert TimezoneResolver().resolve(40.7128, -74.0060) == "America/New_York"


def test_a_missing_timezone_in_the_response_is_a_failure_not_an_empty_success(monkeypatch):
    # A response without a usable zone is a hard error, never a silent empty/wrong result.
    _patch_opener(monkeypatch, payload={"utc_offset_seconds": 7200})
    with pytest.raises(WeatherResponseError):
        TimezoneResolver().resolve(51.18, 7.19)


def test_a_malformed_timezone_in_the_response_is_a_failure(monkeypatch):
    _patch_opener(monkeypatch, payload={"timezone": "Not/AZone"})
    with pytest.raises(WeatherResponseError):
        TimezoneResolver().resolve(51.18, 7.19)


def test_out_of_range_coordinates_make_no_request(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("out-of-range coordinates must be rejected before any network call")

    monkeypatch.setattr(http_json._OPENER, "open", fail)
    with pytest.raises(WeatherResponseError):
        TimezoneResolver().resolve(120.0, 7.19)


def test_timezone_derivation_network_failure_is_a_weather_connection_error(monkeypatch):
    _patch_opener(monkeypatch, error=URLError("name resolution failed"))
    with pytest.raises(WeatherConnectionError):
        TimezoneResolver().resolve(51.18, 7.19)


def test_identical_coordinates_are_served_from_the_timezone_cache(monkeypatch):
    calls = {"n": 0}

    def counting_open(request, timeout=None):
        calls["n"] += 1
        return _FakeResponse(json.dumps({"timezone": "Europe/Berlin"}).encode())

    monkeypatch.setattr(http_json._OPENER, "open", counting_open)
    resolver = TimezoneResolver()
    assert resolver.resolve(51.18, 7.19) == "Europe/Berlin"
    # A sub-kilometre variant rounds to the same cache key, so it is served without a second call.
    assert resolver.resolve(51.1803, 7.1897) == "Europe/Berlin"
    assert calls["n"] == 1


# Live Photon shapes (photon.komoot.io, lang=en), trimmed to the properties the mapper reads.
def _live(name, osm_key, osm_value, **props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [7.2, 51.2]},
        "properties": {"osm_key": osm_key, "osm_value": osm_value, "name": name, **props},
    }


@pytest.mark.parametrize(
    ("code", "city", "district", "state", "country", "countrycode"),
    [
        ("42899", "Remscheid", "Lüttringhausen", "North Rhine-Westphalia", "Germany", "DE"),
        ("SW1A 1AA", "London", "Westminster", "England", "United Kingdom", "GB"),
    ],
)
def test_a_postcode_area_carries_its_code_in_name_and_titles_with_the_city(
    monkeypatch, code, city, district, state, country, countrycode
):
    feature = _live(code, "place", "postcode", type="other", city=city, district=district,
                    state=state, country=country, countrycode=countrycode)
    _patch_opener(monkeypatch, payload=_photon(feature))
    hit = PhotonGeocoder().search(code)[0]
    assert (hit.name, hit.city, hit.district) == (city, city, district)
    assert hit.postal_code == code
    assert (hit.admin1, hit.country, hit.country_code) == (state, country, countrycode.lower())


def test_a_postcode_area_without_a_city_titles_with_its_district(monkeypatch):
    feature = _live("428997", "place", "postcode", district="Bedok", country="Singapore", countrycode="SG")
    _patch_opener(monkeypatch, payload=_photon(feature))
    hit = PhotonGeocoder().search("42899")[0]
    assert (hit.name, hit.city, hit.postal_code) == ("Bedok", None, "428997")


def test_a_settlement_is_its_own_city_and_claims_no_postal_code(monkeypatch):
    payload = _photon(
        _live("Berlin", "place", "city", type="city", country="Germany", countrycode="DE"),
        _live("Berlin", "place", "town", type="city", county="Worcester", state="Maryland",
              country="United States", countrycode="US", postcode="21811"),
    )
    _patch_opener(monkeypatch, payload=payload)
    germany, maryland = PhotonGeocoder().search("Berlin")
    assert (germany.name, germany.city, germany.postal_code) == ("Berlin", "Berlin", None)
    # The place's own postcode is not surfaced for a name search.
    assert (maryland.city, maryland.postal_code, maryland.admin2) == ("Berlin", None, "Worcester")


def test_a_non_settlement_object_is_not_a_city_and_keeps_its_postal_city(monkeypatch):
    feature = _live("Remscheid Hbf", "railway", "station", type="house", postcode="42859",
                    district="Alt-Remscheid", locality="Gemarkung Remscheid", city="Remscheid")
    _patch_opener(monkeypatch, payload=_photon(feature))
    hit = PhotonGeocoder().search("Remscheid")[0]
    assert (hit.name, hit.city, hit.postal_code) == ("Remscheid Hbf", "Remscheid", None)
    peak = _live("Remscheid", "natural", "peak", type="other", county="Rhein-Sieg-Kreis")
    _patch_opener(monkeypatch, payload=_photon(peak))
    assert PhotonGeocoder().search("Remscheid")[0].city is None


def test_open_meteo_marks_only_populated_places_as_city(monkeypatch):
    payload = {"results": [
        _om_result(name="Remscheid", feature_code="PPLA3"),
        _om_result(name="Hohenstein", feature_code="MT"),
    ]}
    _patch_opener(monkeypatch, payload=payload)
    town, mountain = OpenMeteoGeocoder().search("Rem")
    assert town.city == "Remscheid"
    assert mountain.city is None
