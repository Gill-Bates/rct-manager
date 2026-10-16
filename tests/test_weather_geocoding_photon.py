#!/usr/bin/env python3
#
# tests/test_weather_geocoding_photon.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""International-postcode regression tests for the default Photon geocoder.

Photon is the default location source, so the cases the review called out — ``42899`` resolving to
the postal *city* Remscheid (not its administrative area), and clean hits for ``SW1A 1AA`` (London)
and ``M5V 3L9`` (Toronto) — are pinned here. No test reaches the network: the one outbound call is
monkeypatched at the shared transport opener (``app.weather.http_json._OPENER``) and fed canned
Photon GeoJSON, following the ``_FakeResponse``/``_patch_opener`` pattern from
``tests/test_weather_geocoding.py``. These tests exercise :class:`PhotonGeocoder` only and never
touch ``_map_photon_feature``.
"""

import io
import json

from app.weather import http_json
from app.weather.geocoding import PhotonGeocoder


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _patch_opener(monkeypatch, *, payload, capture=None):
    def fake_open(request, timeout=None):
        if capture is not None:
            capture["url"] = request.full_url
            capture["timeout"] = timeout
        return _FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(http_json._OPENER, "open", fake_open)


def _feature(lon, lat, **properties):
    return {"geometry": {"coordinates": [lon, lat]}, "properties": properties}


def test_a_german_postcode_resolves_to_its_postal_city(monkeypatch):
    # 42899 is a Remscheid postcode. Photon returns the locality as the feature name, with the code
    # in `postcode`; the primary result must be the city Remscheid, not the administrative area.
    payload = {"features": [
        _feature(
            7.1925, 51.1798,
            name="Remscheid", postcode="42899", city="Remscheid",
            state="North Rhine-Westphalia", county="Remscheid",
            country="Germany", countrycode="DE",
        ),
    ]}
    _patch_opener(monkeypatch, payload=payload)
    results = PhotonGeocoder().search("42899")
    assert results, "expected a Photon hit for 42899"
    first = results[0]
    assert first.name == "Remscheid"
    assert first.postal_code == "42899"
    assert first.country_code == "de"


def test_a_uk_postcode_resolves_to_london(monkeypatch):
    payload = {"features": [
        _feature(
            -0.1419, 51.5010,
            name="London", postcode="SW1A 1AA", city="London",
            state="England", country="United Kingdom", countrycode="GB",
        ),
    ]}
    _patch_opener(monkeypatch, payload=payload)
    first = PhotonGeocoder().search("SW1A 1AA")[0]
    assert first.name == "London"
    # The queried postcode is matched exactly, case-insensitively.
    assert first.postal_code == "SW1A 1AA"
    assert first.country_code == "gb"


def test_a_canadian_postcode_resolves_to_toronto(monkeypatch):
    payload = {"features": [
        _feature(
            -79.3903, 43.6426,
            name="Toronto", postcode="M5V 3L9", city="Toronto",
            state="Ontario", country="Canada", countrycode="CA",
        ),
    ]}
    _patch_opener(monkeypatch, payload=payload)
    first = PhotonGeocoder().search("M5V 3L9")[0]
    assert first.name == "Toronto"
    assert first.postal_code == "M5V 3L9"
    assert first.country_code == "ca"


def test_the_postal_city_wins_over_the_administrative_area(monkeypatch):
    # Photon may return the administrative area ahead of the locality. The postal city must be the
    # primary pick, so the first result carrying the exact code is the city, not the Regierungsbezirk.
    payload = {"features": [
        _feature(
            7.1, 51.2,
            name="Regierungsbezirk Düsseldorf", state="North Rhine-Westphalia",
            country="Germany", countrycode="DE",
        ),
        _feature(
            7.1925, 51.1798,
            name="Remscheid", postcode="42899", city="Remscheid",
            state="North Rhine-Westphalia", county="Remscheid",
            country="Germany", countrycode="DE",
        ),
    ]}
    _patch_opener(monkeypatch, payload=payload)
    results = PhotonGeocoder().search("42899")
    postal_hit = next(r for r in results if r.postal_code == "42899")
    assert postal_hit.name == "Remscheid"


def test_no_features_is_a_clean_empty_result(monkeypatch):
    _patch_opener(monkeypatch, payload={"features": []})
    assert PhotonGeocoder().search("00000") == []
