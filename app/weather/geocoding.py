#!/usr/bin/env python3
#
# app/weather/geocoding.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Provider-neutral location/geocoding contract for the Weather tab's location search.

A :class:`Geocoder` turns a neutral search string into a short list of :class:`GeocodingResult`
choices. Application code never inspects the query to guess a country or a postal-code format: the
provider owns that knowledge. ``42899`` is just a search string; whether it resolves to a postcode
is the provider's responsibility, not an application regex.

:class:`PhotonGeocoder` (OpenStreetMap data, no key, no registration) is the default international
search. :class:`OpenMeteoGeocoder` is a legacy/alternative adapter of a general place search; it is
not a reliable international postal-code lookup and is no longer the default. The bounded client in
:mod:`app.weather.http_json` validates provider results and raises closed weather-domain errors;
callers run its blocking requests off the event loop.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.clock import is_valid_iana_zone
from app.weather.base import WeatherResponseError
from app.weather.http_json import (
    MAX_RESPONSE_BYTES,
    JsonHttpClient,
    TtlCache,
)
from app.weather.models import OPEN_METEO_GEOCODING_URL, PHOTON_SEARCH_URL

log = logging.getLogger(__name__)

# How many matches the client ever asks for and returns. The admin endpoint shows a short pick list,
# not an unbounded result set.
MAX_RESULTS = 10
# The shortest query that is worth sending: a single character floods the geocoder with noise.
MIN_QUERY_LENGTH = 2
# The longest query the client accepts. The admin endpoint enforces the SAME bound on its ``q``
# parameter, so the string the geocoder searches is exactly the string the caller sent; an
# over-long value is rejected, never silently truncated into a different query.
MAX_QUERY_LENGTH = 120
# A small, bounded, thread-safe TTL cache for identical autocomplete queries. A type-ahead search
# repeats the same prefixes, so caching a short while spares the provider without ever serving a
# stale location (the data barely changes).
CACHE_MAX_ENTRIES = 128
CACHE_TTL_SECONDS = 15 * 60
# Autocomplete must return promptly when a public geocoder is slow or rate-limiting requests.
LOOKUP_TIMEOUT_SECONDS = 8
LOOKUP_MAX_ATTEMPTS = 1

# Module-level aliases used by the geocoding transport tests.
_TtlCache = TtlCache
_REQUEST_TIMEOUT_SECONDS = LOOKUP_TIMEOUT_SECONDS
_MAX_RESPONSE_BYTES = MAX_RESPONSE_BYTES
_MAX_ATTEMPTS = LOOKUP_MAX_ATTEMPTS


class GeocodingProvider(StrEnum):
    """The location/geocoding source for the Weather tab's place search.

    Deliberately independent of :class:`app.weather.models.WeatherProvider` (the solar-forecast
    source): an installation can run the Open-Meteo forecast while geocoding through Photon, or
    vice versa. The closed enum makes an unknown value a configuration error, never a silent
    fallback.
    """

    PHOTON = "photon"
    OPEN_METEO = "open_meteo"


@dataclass(frozen=True, slots=True)
class GeocodingResult:
    """One validated, provider-neutral location choice offered to the Weather tab.

    ``admin1`` is the state/region and ``admin2`` the county/district. ``country_code`` is a
    two-letter lower-case country code (syntax-checked only, not validated against the ISO 3166-1
    list); it disambiguates a postal code several countries share. ``city`` is the populated place
    the result is or lies in, set only from structured provider fields. ``postal_code`` is the
    single postcode this result represents: the feature's own code when the provider returns a
    postcode area, or the listed code the query matched exactly. A place-name hit never surfaces an
    arbitrary postcode. Every field is a display/selection aid; only coordinates, name and timezone
    are later persisted, so no provider-specific meaning leaks into the stored configuration.
    """

    name: str
    latitude: float
    longitude: float
    country: str | None = None
    country_code: str | None = None
    admin1: str | None = None
    admin2: str | None = None
    district: str | None = None
    city: str | None = None
    timezone: str | None = None
    postal_code: str | None = None


class Geocoder(Protocol):
    """What every location provider implements. Synchronous by design: external reads use blocking
    ``urllib`` through the shared hardened transport and run off the event loop via a thread.
    """

    def search(
        self, query: str, *, count: int = MAX_RESULTS, country_code: str | None = None
    ) -> list[GeocodingResult]:
        """Resolve ``query`` to at most ``count`` neutral results.

        ``country_code`` is an optional ISO 3166-1 alpha-2 filter with no built-in default. When
        omitted the search is global; when given it is applied as a real provider filter, and an
        explicit value that is not a valid code is rejected rather than silently dropped. A
        too-short query returns no results without a network call; a provider failure raises a
        :class:`WeatherError` subclass, so "no match" stays distinguishable from "lookup broken".
        """
        ...


def normalize_country_code(country_code: str | None) -> str | None:
    """Normalise a two-letter country code to lower-case, or ``None`` when absent/unusable.

    Intentionally strict in syntax only: exactly two ASCII letters. The code is not checked against
    the ISO 3166-1 list; the provider's own filter decides whether it matches anything. This is the lenient mapping used for a value a
    provider reports back (where a malformed code is simply dropped); callers that must reject a
    bad *explicit* filter use :func:`require_country_code` instead.
    """
    if not isinstance(country_code, str):
        return None
    trimmed = country_code.strip().lower()
    if len(trimmed) == 2 and trimmed.isascii() and trimmed.isalpha():
        return trimmed
    return None


def require_country_code(country_code: str | None) -> str | None:
    """Resolve a caller-supplied country filter: ``None`` stays ``None`` (global search is correct),
    a valid code is normalised, and an explicit-but-invalid value is rejected.

    Degrading an unusable explicit code to ``None`` would silently turn a country-scoped search into
    a global one, so a non-empty value that is not a valid alpha-2 code raises before any request.
    """
    if country_code is None:
        return None
    if isinstance(country_code, str) and not country_code.strip():
        return None
    normalized = normalize_country_code(country_code)
    if normalized is None:
        raise WeatherResponseError(
            "The country filter must be a two-letter country code"
        )
    return normalized


class PhotonGeocoder:
    """International place/postcode search over Photon (OpenStreetMap), with no key or registration.

    Photon is the default geocoder: ``/api?q=`` resolves a city name or an international postal code
    directly against OpenStreetMap, so the application never guesses a country or a postcode format.
    Shares the hardened transport (fixed HTTPS origin, same-origin-redirect refusal, request
    timeout and response-size cap) and a bounded TTL cache with the other weather providers.
    Interactive searches use one short attempt, so a slow public server cannot stall the form.
    """

    # Photon serves OpenStreetMap data; the ODbL requires crediting OpenStreetMap contributors.
    # Exposed through the admin API so the GUI shows the attribution for the ACTIVE geocoder.
    location_attribution: str = "Location data from Photon © OpenStreetMap contributors"

    def __init__(
        self,
        sleep: Callable[[float], None] | None = None,
        *,
        cache: TtlCache | None = None,
    ) -> None:
        self._http = JsonHttpClient(
            "Photon geocoding",
            sleep=sleep,
            timeout_seconds=LOOKUP_TIMEOUT_SECONDS,
            max_attempts=LOOKUP_MAX_ATTEMPTS,
        )
        self._cache = (
            cache
            if cache is not None
            else TtlCache(CACHE_MAX_ENTRIES, CACHE_TTL_SECONDS)
        )

    def search(
        self, query: str, *, count: int = MAX_RESULTS, country_code: str | None = None
    ) -> list[GeocodingResult]:
        trimmed = (query or "").strip()
        if len(trimmed) < MIN_QUERY_LENGTH:
            return []
        if len(trimmed) > MAX_QUERY_LENGTH:
            # Reject rather than truncate: a truncated query would search a different string than the
            # caller asked for. The admin endpoint enforces the same bound, so this is a guard.
            raise WeatherResponseError("The location query is too long")
        limit = max(1, min(int(count), MAX_RESULTS))
        # An explicit-but-invalid country code is rejected here, before any request leaves.
        country = require_country_code(country_code)
        key = (trimmed.casefold(), country, limit)
        cached = self._cache.get(key)
        if cached is not None:
            return list(cached)
        payload = self._http.get_json(self._build_url(trimmed, limit, country))
        results = self._parse(payload, trimmed, limit)
        self._cache.put(key, tuple(results))
        return results

    def _build_url(self, query: str, count: int, country: str | None) -> str:
        """Build the HTTPS forward-search URL. ``countrycode`` is a hard provider-side filter added
        only when a code was given; the free ``q`` search stays country-global otherwise.
        """
        params = [("q", query), ("limit", str(count)), ("lang", "en")]
        if country is not None:
            params.append(("countrycode", country))
        return self._http.build_url(PHOTON_SEARCH_URL, params)

    def _parse(self, payload: dict, query: str, limit: int) -> list[GeocodingResult]:
        """Map Photon's GeoJSON ``features`` list to :class:`GeocodingResult` values.

        An absent or empty ``features`` list is a valid "no matches" answer, not an error; a
        non-list ``features`` is a structurally broken response. A feature missing a usable name or
        coordinates is skipped rather than guessed, and only the first ``limit`` usable features are
        returned.
        """
        features = payload.get("features")
        if features is None:
            return []
        if not isinstance(features, list):
            raise WeatherResponseError("Photon geocoding response is malformed")
        results: list[GeocodingResult] = []
        for feature in features:
            mapped = _map_photon_feature(feature, query)
            if mapped is not None:
                results.append(mapped)
            if len(results) >= limit:
                break
        return results


class OpenMeteoGeocoder:
    """Legacy, no-key place search with bounded responses, retries and request timeouts.

    A general Open-Meteo place-index search, kept as an alternative to the default Photon geocoder.
    It is NOT a reliable international postal-code lookup: its index carries postcodes for only part
    of its places, so a postcode query is sent like any other name query and may legitimately find
    nothing. Its ``countryCode`` filter is applied when a code is given.
    """

    # Open-Meteo's place/geocoding index is sourced from GeoNames; its terms require the credit.
    # Exposed through the admin API so the GUI shows the attribution for the ACTIVE geocoder.
    location_attribution: str = "Location data based on GeoNames via Open-Meteo"

    def __init__(
        self,
        sleep: Callable[[float], None] | None = None,
        *,
        cache: TtlCache | None = None,
    ) -> None:
        self._http = JsonHttpClient(
            "Open-Meteo geocoding",
            sleep=sleep,
            timeout_seconds=LOOKUP_TIMEOUT_SECONDS,
            max_attempts=LOOKUP_MAX_ATTEMPTS,
        )
        self._cache = (
            cache
            if cache is not None
            else TtlCache(CACHE_MAX_ENTRIES, CACHE_TTL_SECONDS)
        )

    def search(
        self, query: str, *, count: int = MAX_RESULTS, country_code: str | None = None
    ) -> list[GeocodingResult]:
        """Resolve ``query`` to at most ``count`` places. A too-short query returns no results
        without a network call; a provider failure raises a :class:`WeatherError` subclass.

        ``country_code`` is applied as Open-Meteo's ``countryCode`` filter when given; an explicit
        value that is not a valid alpha-2 code is rejected rather than dropped. Identical queries are
        served from a short-lived TTL cache so a type-ahead search does not hit the provider on
        every keystroke.
        """
        trimmed = (query or "").strip()
        if len(trimmed) < MIN_QUERY_LENGTH:
            return []
        if len(trimmed) > MAX_QUERY_LENGTH:
            # Reject rather than truncate: a truncated query would search a different string than the
            # caller asked for. The admin endpoint enforces the same bound, so this is a guard.
            raise WeatherResponseError("The location query is too long")
        limit = max(1, min(int(count), MAX_RESULTS))
        country = require_country_code(country_code)
        key = (trimmed.casefold(), country, limit)
        cached = self._cache.get(key)
        if cached is not None:
            return list(cached)
        payload = self._http.get_json(self._build_url(trimmed, limit, country))
        results = self._parse(payload, trimmed, limit)
        self._cache.put(key, tuple(results))
        return results

    def _build_url(self, query: str, count: int, country: str | None) -> str:
        """Build an HTTPS geocoding URL by varying only query parameters."""
        params = [
            ("name", query),
            ("count", str(count)),
            ("format", "json"),
            ("language", "en"),
        ]
        if country is not None:
            # Open-Meteo expects the alpha-2 code upper-cased for its countryCode filter.
            params.append(("countryCode", country.upper()))
        return self._http.build_url(OPEN_METEO_GEOCODING_URL, params)

    def _parse(self, payload: dict, query: str, limit: int) -> list[GeocodingResult]:
        """Map the provider's ``results`` list to :class:`GeocodingResult` values.

        An absent or empty ``results`` key is a valid "no matches" answer (the geocoder omits the
        key for a query with no hit), not an error. A record missing a name or usable coordinates is
        skipped rather than guessed, and only the first ``limit`` usable records are returned.
        """
        records = payload.get("results")
        if records is None:
            return []
        if not isinstance(records, list):
            raise WeatherResponseError("Open-Meteo geocoding results are malformed")
        results: list[GeocodingResult] = []
        for record in records:
            mapped = self._map_record(record, query)
            if mapped is not None:
                results.append(mapped)
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _map_record(record: object, query: str) -> GeocodingResult | None:
        if not isinstance(record, dict):
            return None
        name = record.get("name")
        latitude = record.get("latitude")
        longitude = record.get("longitude")
        if not isinstance(name, str) or not name.strip():
            return None
        if not is_finite_number(latitude) or not is_finite_number(longitude):
            return None
        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            return None
        # Only claim a postal_code when the query itself exactly matches one of the listed codes;
        # a name search, or a code the place does not carry, must not surface an arbitrary postcode.
        postcodes = _postcodes(record.get("postcodes"))
        postal_code = _exact_postal_match(query, postcodes)
        return GeocodingResult(
            name=name.strip(),
            latitude=float(latitude),
            longitude=float(longitude),
            country=optional_str(record.get("country")),
            country_code=normalize_country_code(record.get("country_code")),
            admin1=optional_str(record.get("admin1")),
            admin2=optional_str(record.get("admin2")),
            # GeoNames feature codes starting with PPL are populated places; only those are a city.
            city=(
                name.strip()
                if str(record.get("feature_code") or "").startswith("PPL")
                else None
            ),
            timezone=optional_timezone(record.get("timezone")),
            postal_code=postal_code,
        )


# OSM ``place`` values that denote a populated settlement (the feature itself is the city).
_PHOTON_SETTLEMENT_VALUES = frozenset({"city", "town", "village"})


def _map_photon_feature(feature: object, query: str) -> GeocodingResult | None:
    """Project one Photon GeoJSON feature; skip it rather than guess a missing or invalid value.

    ``geometry.coordinates`` is ``[lon, lat]`` (GeoJSON order). Photon carries no IANA timezone, so ``timezone`` stays ``None``.
    """
    if not isinstance(feature, dict):
        return None
    properties = feature.get("properties")
    geometry = feature.get("geometry")
    if not isinstance(properties, dict) or not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        return None
    longitude, latitude = coordinates[0], coordinates[1]
    if not is_finite_number(latitude) or not is_finite_number(longitude):
        return None
    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        return None
    raw_name = optional_str(properties.get("name"))
    district = optional_str(properties.get("district"))
    osm_key, osm_value = properties.get("osm_key"), properties.get("osm_value")
    if osm_key == "place" and osm_value == "postcode":
        # A postcode area carries its code as ``name`` and has no ``postcode`` property; it is titled
        # by the place it belongs to.
        postal_code = raw_name
        city = optional_str(properties.get("city"))
        name = (
            city or district or optional_str(properties.get("locality")) or postal_code
        )
    else:
        # Any other feature claims a postcode only when the query matched its own code exactly, so a
        # place-name search never shows an arbitrary postcode. A populated place is its own city.
        own_code = optional_str(properties.get("postcode"))
        postal_code = _exact_postal_match(query, (own_code,) if own_code else ())
        city = optional_str(properties.get("city"))
        if (
            city is None
            and osm_key == "place"
            and osm_value in _PHOTON_SETTLEMENT_VALUES
        ):
            city = raw_name
        # For an exact postcode search the postal locality titles the hit, not the matched OSM
        # object (which can be an administrative area); a normal search keeps the provider's name.
        if postal_code is not None:
            name = city or district or raw_name or postal_code
        else:
            name = raw_name or city or district or own_code
    if name is None:
        return None
    return GeocodingResult(
        name=name,
        latitude=float(latitude),
        longitude=float(longitude),
        country=optional_str(properties.get("country")),
        country_code=normalize_country_code(properties.get("countrycode")),
        admin1=optional_str(properties.get("state")),
        admin2=optional_str(properties.get("county")),
        district=district,
        city=city,
        # Photon returns no IANA timezone; never derive one from the country or the longitude.
        timezone=None,
        postal_code=postal_code,
    )


def is_finite_number(value: object) -> bool:
    import math

    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _postcodes(value: object) -> tuple[str, ...]:
    """Collect any postal codes a record lists, de-duplicated and order-preserving.

    A missing or malformed list is simply no postcodes; non-string entries are skipped rather than
    guessed. Used only to decide the canonical ``postal_code`` for an exact query match; it is never
    part of the neutral contract.
    """
    if not isinstance(value, list):
        return ()
    seen: dict[str, None] = {}
    for item in value:
        if isinstance(item, str) and item.strip():
            seen.setdefault(item.strip(), None)
    return tuple(seen)


def _exact_postal_match(query: str, postcodes: tuple[str, ...]) -> str | None:
    """Return the single postcode the query matched exactly (case-insensitive), else ``None``.

    This is the fix for the ``postcodes[0]`` defect: querying ``32781`` against ``["32780","32781"]``
    must surface ``32781``, and a place-name query matches nothing here.
    """
    wanted = (query or "").strip().casefold()
    if not wanted:
        return None
    return next((code for code in postcodes if code.casefold() == wanted), None)


def optional_timezone(value: object) -> str | None:
    """Keep a timezone only when it is a real IANA zone; drop a malformed one rather than passing a
    value a later local-time consumer cannot resolve.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    trimmed = value.strip()
    return trimmed if is_valid_iana_zone(trimmed) else None
