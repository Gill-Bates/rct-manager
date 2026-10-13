#!/usr/bin/env python3
#
# app/spotmarket/energy_charts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Fraunhofer Energy-Charts day-ahead price provider (API v2).

The ``/v2/price_next_day`` endpoint answers with the uniform v2 envelope: a timezone, a resolution,
an envelope-level unit and a chronologically ordered ``data`` list. Each record carries an ISO 8601
``timestamp`` (with UTC offset, marking the interval start) and a ``values`` map from series id to
number::

    {"schema_version": "2.0", "timezone": "Europe/Berlin", "resolution": "PT15M",
     "interval_minutes": 15, "unit": "EUR/MWh",
     "series": [{"id": "price", "name": "Day-ahead price"}],
     "data": [{"timestamp": "2026-07-21T00:00:00+02:00", "values": {"price": 178.73}}, ...],
     "deprecated": false}

The endpoint returns tomorrow's full curve, not today's running intervals, and answers 404 until
tomorrow's auction is published (typically early afternoon CET). A 404 on this endpoint means
"not available yet"; other unsuccessful responses remain provider errors.

The provider validates the shape, resolves the price series by its single descriptor id, normalises
the unit (envelope-level or series-level) to EUR/kWh and returns timezone-aware timestamps. It never
invents or interpolates a price: a gap, a non-finite value, a missing series or an inconsistent
record is a :class:`SpotMarketResponseError`, not a silently patched series.

HTTP uses the standard-library ``urllib`` client, the same blocking client the release check uses;
the project pins no runtime HTTP dependency. The caller runs this off the event loop in a thread.
"""

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from itertools import pairwise
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.spotmarket.base import SpotMarketConnectionError, SpotMarketNotAvailableYet, SpotMarketResponseError
from app.spotmarket.models import (
    DEFAULT_BIDDING_ZONE,
    ENERGY_CHARTS_CURRENCY,
    ENERGY_CHARTS_URL,
    MAX_INTERVALS,
    MWH_PER_KWH,
    BiddingZoneCatalog,
    PriceUnit,
    SpotMarketConfig,
    SpotMarketProvider,
    SpotPriceInterval,
    SpotPriceSnapshot,
)

CURRENT_PRICE_URL = "https://api.energy-charts.info/v2/price_current"
TODAY_PRICE_URL = "https://api.energy-charts.info/v2/price"

log = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 15
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # a day of 15-min prices is a few kB; this is a generous cap
# The v2 catalog endpoint (GET /v2) is the provider's machine-readable directory; it lists every
# endpoint with the accepted values of its parameters, so the bidding-zone list comes from here
# rather than a hardcoded copy in the client. Derived from ENERGY_CHARTS_URL's own origin.
_CATALOG_URL = urljoin(ENERGY_CHARTS_URL, "/v2")
_NEXT_DAY_PATH = urlsplit(ENERGY_CHARTS_URL).path
# A tight retry budget for a transient 429/5xx: a few attempts, never an unbounded loop. The caller
# runs in a thread, so a bounded sleep for Retry-After is acceptable and must stay small.
_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 30
# The day-ahead feed is strictly 15-minute resolution. Every adjacent timestamp pair must be exactly
# this far apart; a gap, a duplicate or a different resolution is a response error, never patched.
EXPECTED_INTERVAL_SECONDS = 900
EXPECTED_INTERVAL_MINUTES = 15
# Normalise the provider's unit string (e.g. "EUR / MWh", "eur/mwh") to a PriceUnit. Whitespace and
# case vary, so the key is the compacted, lower-cased form.
_UNIT_BY_COMPACT = {
    "eur/kwh": PriceUnit.EUR_PER_KWH,
    "eur/mwh": PriceUnit.EUR_PER_MWH,
}
# Reported price unit -> divisor that turns it into EUR/kWh. EUR/MWh is a factor of 1000 larger.
_TO_EUR_PER_KWH = {
    PriceUnit.EUR_PER_KWH: Decimal(1),
    PriceUnit.EUR_PER_MWH: MWH_PER_KWH,
}


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    """A redirect handler that refuses to leave the provider's origin.

    ``urllib`` follows 30x responses by default, so even the fixed trusted URL could be bounced to
    ``http://127.0.0.1`` or an internal host. A redirect is allowed only when the target keeps the
    same scheme, host and port as the request; anything else (an https->http downgrade, a different
    host or port) raises, so a redirect cannot reach an internal target.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        source = urlsplit(req.full_url)
        target = urlsplit(newurl)
        if (target.scheme, target.hostname, target.port) != (source.scheme, source.hostname, source.port):
            raise HTTPError(req.full_url, code, "Energy-Charts redirected to a different origin", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# One opener for the whole process: it refuses cross-origin redirects and is the only path the
# provider uses to reach the network. Built once so the handler chain is reused.
_OPENER = build_opener(_SameOriginRedirectHandler())


class EnergyChartsProvider:
    """Day-ahead prices from the Fraunhofer Energy-Charts API (v2).

    Every outbound call has a hard timeout, a bounded retry budget and explicit 429/Retry-After
    handling. A failure maps to a closed :class:`SpotMarketError` subclass, so a dead or
    rate-limited external API never crashes the caller and never reaches the inverter control path.
    ``sleep`` is injected so tests exercise the retry logic without real delays.
    """

    def __init__(self, sleep: Callable[[float], None] | None = None) -> None:
        self._sleep = sleep if sleep is not None else time.sleep

    def fetch_day_ahead(self, config: SpotMarketConfig) -> SpotPriceSnapshot:
        payload = self._get(self._with_zone(config.bidding_zone))
        intervals = self._parse(payload)
        return SpotPriceSnapshot(
            provider=SpotMarketProvider.ENERGY_CHARTS,
            bidding_zone=config.bidding_zone,
            # Currency is provider metadata, not operator input: Energy-Charts reports EUR.
            currency=ENERGY_CHARTS_CURRENCY,
            fetched_at=datetime.now(UTC),
            intervals=intervals,
            timezone=self._timezone(payload),
            source_generated_at=self._optional_datetime(payload, "generated_at"),
            available_from=self._optional_datetime(payload, "available_from"),
            available_until=self._optional_datetime(payload, "available_until"),
        )

    def fetch_today(self, config: SpotMarketConfig) -> SpotPriceSnapshot:
        """Read today's complete day-ahead curve in the bidding zone's local day."""
        payload = self._get(self._with_zone(config.bidding_zone, endpoint=TODAY_PRICE_URL))
        return SpotPriceSnapshot(
            provider=SpotMarketProvider.ENERGY_CHARTS,
            bidding_zone=config.bidding_zone,
            currency=ENERGY_CHARTS_CURRENCY,
            fetched_at=datetime.now(UTC),
            intervals=self._parse(payload),
            timezone=self._timezone(payload),
            source_generated_at=self._optional_datetime(payload, "generated_at"),
            available_from=self._optional_datetime(payload, "available_from"),
            available_until=self._optional_datetime(payload, "available_until"),
        )

    def fetch_yesterday(self, config: SpotMarketConfig, timezone: str | None) -> SpotPriceSnapshot:
        """Read only yesterday's day-ahead curve in the bidding zone's local timezone."""
        if timezone is None:
            raise SpotMarketResponseError("Energy-Charts did not provide a timezone")
        try:
            local_today = datetime.now(ZoneInfo(timezone)).date()
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise SpotMarketResponseError("Energy-Charts timezone is invalid") from exc
        yesterday = (local_today - timedelta(days=1)).isoformat()
        payload = self._get(self._with_zone(
            config.bidding_zone,
            endpoint=TODAY_PRICE_URL,
            extra_params=(("start", yesterday), ("end", yesterday)),
        ))
        return SpotPriceSnapshot(
            provider=SpotMarketProvider.ENERGY_CHARTS,
            bidding_zone=config.bidding_zone,
            currency=ENERGY_CHARTS_CURRENCY,
            fetched_at=datetime.now(UTC),
            intervals=self._parse(payload),
            timezone=self._timezone(payload),
            source_generated_at=self._optional_datetime(payload, "generated_at"),
            available_from=self._optional_datetime(payload, "available_from"),
            available_until=self._optional_datetime(payload, "available_until"),
        )

    def fetch_current(self, config: SpotMarketConfig) -> SpotPriceSnapshot:
        """Read the provider's current interval from its dedicated v2 endpoint."""
        payload = self._get(self._with_zone(config.bidding_zone, endpoint=CURRENT_PRICE_URL))
        intervals = self._parse(payload)
        if len(intervals) != 1:
            raise SpotMarketResponseError("Energy-Charts current price must contain one interval")
        return SpotPriceSnapshot(
            provider=SpotMarketProvider.ENERGY_CHARTS,
            bidding_zone=config.bidding_zone,
            currency=ENERGY_CHARTS_CURRENCY,
            fetched_at=datetime.now(UTC),
            intervals=intervals,
            timezone=self._timezone(payload),
            source_generated_at=self._optional_datetime(payload, "generated_at"),
            available_from=self._optional_datetime(payload, "available_from"),
            available_until=self._optional_datetime(payload, "available_until"),
            valid_until=self._optional_datetime(payload, "valid_until"),
        )

    @staticmethod
    def _timezone(payload: dict) -> str | None:
        value = payload.get("timezone")
        if value is None:
            return None
        if not isinstance(value, str):
            raise SpotMarketResponseError("Energy-Charts timezone is malformed")
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise SpotMarketResponseError("Energy-Charts timezone is invalid") from exc
        return value

    @staticmethod
    def _optional_datetime(payload: dict, field: str) -> datetime | None:
        value = payload.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            raise SpotMarketResponseError(f"Energy-Charts {field} is malformed")
        try:
            moment = datetime.fromisoformat(value)
        except ValueError as exc:
            raise SpotMarketResponseError(f"Energy-Charts {field} is invalid") from exc
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise SpotMarketResponseError(f"Energy-Charts {field} is not timezone-aware")
        return moment

    def fetch_bidding_zones(self) -> BiddingZoneCatalog:
        """The accepted bidding zones, read from the v2 catalog (GET /v2).

        The catalog is the provider's machine-readable directory; the ``bzn`` parameter of the
        next-day endpoint carries its closed ``allowed_values`` list and ``default``. Nothing here
        is hardcoded in the client, so a provider-side change to the zone set is picked up without a
        code change.
        """
        payload = self._get(_CATALOG_URL)
        zones, default = self._parse_zone_catalog(payload)
        return BiddingZoneCatalog(zones=zones, default=default, fetched_at=datetime.now(UTC))

    def _get(self, url: str) -> dict:
        # The destination is always the provider's own fixed HTTPS origin, never an operator-supplied
        # host; only the ``bzn`` query parameter or the fixed catalog path varies, so this is not an
        # SSRF surface (S310 is moot here).
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "rct-manager"})  # noqa: S310
        raw = self._read_with_retry(request)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise SpotMarketResponseError("Energy-Charts response is implausibly large")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpotMarketResponseError("Energy-Charts response is not valid JSON") from exc
        if not isinstance(data, dict):
            raise SpotMarketResponseError("Energy-Charts response is not a JSON object")
        return data

    def _read_with_retry(self, request: Request) -> bytes:
        """Open the request with a hard timeout and a bounded retry budget.

        A 429 (rate limit) is retried after honouring a sane ``Retry-After``; a 5xx is retried too.
        A next-day 404 is an expected unpublished state; other 4xx are response errors. Neither is retried. The retry count is
        strictly capped, so there is no unbounded loop even against a provider that keeps answering
        429.
        """
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                with _OPENER.open(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                    return response.read(_MAX_RESPONSE_BYTES + 1)
            except HTTPError as exc:
                if exc.code == 404 and urlsplit(request.full_url).path == urlsplit(ENERGY_CHARTS_URL).path:
                    raise SpotMarketNotAvailableYet("Energy-Charts next-day prices are not published yet") from exc
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt < _MAX_ATTEMPTS:
                        self._sleep(self._retry_after_seconds(exc))
                        continue
                    # Retry budget exhausted: a transient server/limit condition that did not clear
                    # is "temporarily no data", a connection-class failure, never fabricated prices.
                    raise SpotMarketConnectionError(
                        f"Energy-Charts is unavailable after {attempt} attempts (HTTP {exc.code})"
                    ) from exc
                raise SpotMarketResponseError(f"Energy-Charts returned HTTP {exc.code}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", None) or exc
                raise SpotMarketConnectionError(f"Energy-Charts is unreachable: {reason}") from exc
        # Unreachable: the loop either returns or raises, but keep the type checker and intent clear.
        raise SpotMarketConnectionError("Energy-Charts is unavailable")  # pragma: no cover

    def _retry_after_seconds(self, exc: HTTPError) -> float:
        """Parse a ``Retry-After`` header (delta-seconds or HTTP date), clamped to a sane bound.

        A missing or unparseable value falls back to one second; an over-long or negative value is
        clamped into ``[0, _MAX_RETRY_AFTER_SECONDS]`` so a hostile header cannot stall the thread.
        """
        header = exc.headers.get("Retry-After") if exc.headers is not None else None
        if header is None:
            return 1.0
        header = header.strip()
        try:
            seconds = float(int(header))
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
            except (TypeError, ValueError):
                return 1.0
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - datetime.now(UTC)).total_seconds()
        return max(0.0, min(seconds, float(_MAX_RETRY_AFTER_SECONDS)))

    @staticmethod
    def _with_zone(
        bidding_zone: str,
        *,
        endpoint: str = ENERGY_CHARTS_URL,
        extra_params: tuple[tuple[str, str], ...] = (),
    ) -> str:
        """Add the ``bzn`` query parameter to the provider's fixed trusted HTTPS endpoint.

        The origin comes from :data:`ENERGY_CHARTS_URL`, not from any operator input, and HTTPS is
        enforced here as a second guard against a misconfigured constant.
        """
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or parts.netloc != urlsplit(ENERGY_CHARTS_URL).netloc:
            raise SpotMarketResponseError("The Energy-Charts endpoint must be an https URL")
        existing = [(k, v) for k, v in _parse_query(parts.query) if k != "bzn"]
        query = urlencode([*existing, ("bzn", bidding_zone), *extra_params])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))

    @staticmethod
    def _parse_zone_catalog(payload: dict) -> tuple[tuple[str, ...], str]:
        """Extract the next-day endpoint's accepted ``bzn`` values and default from the v2 catalog.

        The client never guesses the zone set: it reads the ``allowed_values`` the provider itself
        publishes for the ``bzn`` parameter of the next-day endpoint. A catalog that does not carry
        that parameter is a response error, not a silent empty list.
        """
        endpoints = payload.get("endpoints")
        if not isinstance(endpoints, list):
            raise SpotMarketResponseError("Energy-Charts catalog has no endpoint list")
        entry = next(
            (e for e in endpoints if isinstance(e, dict) and e.get("path") == _NEXT_DAY_PATH),
            None,
        )
        if entry is None:
            raise SpotMarketResponseError("Energy-Charts catalog does not describe the next-day endpoint")
        parameters = entry.get("parameters")
        if not isinstance(parameters, list):
            raise SpotMarketResponseError("Energy-Charts catalog endpoint has no parameters")
        bzn = next((p for p in parameters if isinstance(p, dict) and p.get("name") == "bzn"), None)
        if bzn is None:
            raise SpotMarketResponseError("Energy-Charts catalog has no bzn parameter")
        allowed = bzn.get("allowed_values")
        if not isinstance(allowed, list) or not allowed or not all(isinstance(z, str) and z for z in allowed):
            raise SpotMarketResponseError("Energy-Charts catalog lists no bidding zones")
        zones = tuple(dict.fromkeys(allowed))  # preserve provider order, drop any accidental repeat
        raw_default = bzn.get("default")
        default = raw_default if isinstance(raw_default, str) and raw_default in zones else None
        if default is None:
            default = DEFAULT_BIDDING_ZONE if DEFAULT_BIDDING_ZONE in zones else zones[0]
        return zones, default

    def _parse(self, payload: dict) -> tuple[SpotPriceInterval, ...]:
        self._check_interval_minutes(payload.get("interval_minutes"))
        series_id, series_unit = self._price_series(payload)
        unit = self._unit(series_unit if series_unit is not None else payload.get("unit"))
        divisor = _TO_EUR_PER_KWH[unit]
        records = payload.get("data")
        if not isinstance(records, list):
            raise SpotMarketResponseError("Energy-Charts response has no data records")
        if not records:
            return ()
        if len(records) > MAX_INTERVALS:
            raise SpotMarketResponseError("Energy-Charts returned more intervals than expected")
        starts = self._timestamps(records)
        self._check_resolution(starts)
        prices = [self._price(record, series_id, divisor) for record in records]
        intervals = [
            SpotPriceInterval(
                start=start,
                end=_shift(start, EXPECTED_INTERVAL_SECONDS),
                price_eur_per_kwh=price,
            )
            for start, price in zip(starts, prices, strict=True)
        ]
        return tuple(intervals)

    @staticmethod
    def _check_interval_minutes(value: object) -> None:
        """Reject an envelope whose declared resolution is not the 15-minute day-ahead grid.

        ``interval_minutes`` is optional metadata (``null`` for an irregular axis), so it is only
        checked when present; the per-pair 900 s check below is the authoritative guard either way.
        """
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int):
            raise SpotMarketResponseError("Energy-Charts interval_minutes is not an integer")
        if value != EXPECTED_INTERVAL_MINUTES:
            raise SpotMarketResponseError(
                f"Energy-Charts resolution is {value} minutes, expected {EXPECTED_INTERVAL_MINUTES}"
            )

    @staticmethod
    def _price_series(payload: dict) -> tuple[str, str | None]:
        """Resolve the single price series: its id (the key inside every ``values`` map) and its
        optional series-level unit override. The price endpoints carry exactly one series.
        """
        series = payload.get("series")
        if not isinstance(series, list) or len(series) != 1:
            raise SpotMarketResponseError("Energy-Charts response does not carry a single price series")
        descriptor = series[0]
        if not isinstance(descriptor, dict):
            raise SpotMarketResponseError("Energy-Charts series descriptor is malformed")
        series_id = descriptor.get("id")
        if not isinstance(series_id, str) or not series_id:
            raise SpotMarketResponseError("Energy-Charts series descriptor has no id")
        series_unit = descriptor.get("unit")
        if series_unit is not None and not isinstance(series_unit, str):
            raise SpotMarketResponseError("Energy-Charts series unit is malformed")
        return series_id, series_unit

    @staticmethod
    def _check_resolution(starts: list[datetime]) -> None:
        """Require every adjacent interval start to be exactly 15 minutes apart.

        A missing, duplicated, unsorted or differently-resolved slot is rejected here rather than
        turned into a 30- or 45-minute interval: a later charging decision must be able to trust
        that one slot is exactly 900 seconds. Timestamps are already parsed to tz-aware instants.
        """
        for previous, current in pairwise(starts):
            delta = (current - previous).total_seconds()
            if delta != EXPECTED_INTERVAL_SECONDS:
                raise SpotMarketResponseError(
                    "Energy-Charts timestamps are not 15 minutes apart: "
                    f"{previous.isoformat()} -> {current.isoformat()} is {delta}s, "
                    f"expected {EXPECTED_INTERVAL_SECONDS}s"
                )

    @staticmethod
    def _unit(value: object) -> PriceUnit:
        if not isinstance(value, str):
            raise SpotMarketResponseError("Energy-Charts response has no price unit")
        compact = "".join(value.split()).lower()
        unit = _UNIT_BY_COMPACT.get(compact)
        if unit is None:
            raise SpotMarketResponseError(f"Energy-Charts reported an unsupported price unit: {value!r}")
        return unit

    @staticmethod
    def _timestamps(records: list) -> list[datetime]:
        result = []
        for record in records:
            if not isinstance(record, dict):
                raise SpotMarketResponseError("Energy-Charts data record is malformed")
            raw = record.get("timestamp")
            if not isinstance(raw, str):
                raise SpotMarketResponseError("Energy-Charts timestamp is missing or not a string")
            try:
                moment = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise SpotMarketResponseError("Energy-Charts timestamp is not a valid ISO 8601 value") from exc
            if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
                raise SpotMarketResponseError("Energy-Charts timestamp is not timezone-aware")
            # Normalise to UTC so interval starts are comparable regardless of the envelope zone;
            # the instant is preserved, only the offset representation changes.
            result.append(moment.astimezone(UTC))
        return result

    @staticmethod
    def _price(record: dict, series_id: str, divisor: Decimal) -> Decimal:
        values = record.get("values")
        if not isinstance(values, dict) or series_id not in values:
            raise SpotMarketResponseError("Energy-Charts record has no price value")
        value = values[series_id]
        # A null value means "not available"; the series must be complete, so it is a hard error
        # rather than a hole patched with a fabricated price.
        if value is None:
            raise SpotMarketResponseError("Energy-Charts price is missing for an interval")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SpotMarketResponseError("Energy-Charts price is not a number")
        try:
            price = Decimal(str(value))
        except InvalidOperation as exc:
            raise SpotMarketResponseError("Energy-Charts price is not a finite number") from exc
        if not price.is_finite():
            raise SpotMarketResponseError("Energy-Charts price is not a finite number")
        return price / divisor


def _parse_query(query: str) -> list[tuple[str, str]]:
    return parse_qsl(query, keep_blank_values=True)


def _shift(moment: datetime, step_seconds: int) -> datetime:
    return moment + timedelta(seconds=step_seconds)
