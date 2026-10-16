#!/usr/bin/env python3
#
# app/weather/http_json.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""One hardened JSON transport for the weather domain's fixed provider origins.

Every outbound location/weather call shares the same envelope: an opener that refuses a
cross-origin redirect, a hard request timeout, a response-size cap, a capped retry budget for 429
and 5xx, and closed :class:`WeatherError` failures. A second provider origin therefore inherits the
hardening of the first instead of re-implementing it. Callers run these blocking requests off the
event loop.
"""

import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import OpenerDirector, Request, build_opener

from app.net import SameOriginRedirectHandler, retry_after_seconds
from app.weather.base import WeatherConnectionError, WeatherResponseError

REQUEST_TIMEOUT_SECONDS = 15
# A geocoding answer is a short list of places; this cap is generous but bounds a hostile or runaway
# response so a single read cannot exhaust memory.
MAX_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_ATTEMPTS = 3


class TtlCache:
    """A tiny LRU+TTL cache: bounded size, per-entry expiry, safe for concurrent access.

    Keys are the normalised query the caller builds; values are its resolved result lists. Expired
    or evicted entries are dropped on access/insert, so the map never grows past ``max_entries``.
    """

    def __init__(self, max_entries: int, ttl_seconds: float, *, clock: Callable[[], float] | None = None) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.Lock()
        self._entries: OrderedDict[object, tuple[float, object]] = OrderedDict()

    def get(self, key: object) -> object | None:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def put(self, key: object, value: object) -> None:
        now = self._clock()
        with self._lock:
            self._entries[key] = (now + self._ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)


class _SameOriginRedirectHandler(SameOriginRedirectHandler):
    provider_label = "A weather provider"


# One opener for the whole process: it refuses cross-origin redirects and is the only path these
# clients use to reach the network. Built once so the handler chain is reused.
_OPENER = build_opener(_SameOriginRedirectHandler())


class JsonHttpClient:
    """Fetch a JSON object from one fixed, provider-owned HTTPS origin.

    ``provider`` only labels the failure messages a caller may surface to an operator; it never
    carries a credential, and no query parameter is ever logged or echoed into an error.
    """

    def __init__(
        self,
        provider: str,
        *,
        sleep: Callable[[float], None] | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        opener: OpenerDirector | None = None,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if timeout_seconds <= 0 or max_attempts < 1:
            raise ValueError("HTTP timeout and attempt count must be positive")
        self._provider = provider
        self._sleep = sleep if sleep is not None else time.sleep
        self._max_response_bytes = max_response_bytes
        self._opener = opener if opener is not None else _OPENER
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts

    def build_url(self, base_url: str, params: Iterable[tuple[str, str]]) -> str:
        """Build an HTTPS URL from the provider's fixed base by varying only query parameters."""
        parts = urlsplit(base_url)
        if parts.scheme != "https":
            raise WeatherResponseError(f"The {self._provider} endpoint must be an https URL")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(list(params)), ""))

    def get_json(self, url: str) -> dict:
        # The destination is always the provider's own fixed HTTPS origin, never an operator-supplied
        # host; only query parameters derived from the sanitised query vary, so this is not an SSRF
        # surface (S310 is moot here).
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "rct-manager"})  # noqa: S310
        raw = self._read_with_retry(request)
        if len(raw) > self._max_response_bytes:
            raise WeatherResponseError(f"{self._provider} response is implausibly large")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WeatherResponseError(f"{self._provider} response is not valid JSON") from exc
        if not isinstance(data, dict):
            raise WeatherResponseError(f"{self._provider} response is not a JSON object")
        return data

    def _read_with_retry(self, request: Request) -> bytes:
        """Open the request with a hard timeout and a bounded retry budget.

        A 429 (rate limit) is retried after honouring a sane ``Retry-After``; a 5xx is retried too.
        A 4xx (other than 429) is a response error and is not retried. The retry count is strictly
        capped, so there is no unbounded loop even against a provider that keeps answering 429.
        """
        for attempt in range(1, self._max_attempts + 1):
            try:
                with self._opener.open(request, timeout=self._timeout_seconds) as response:
                    return response.read(self._max_response_bytes + 1)
            except HTTPError as exc:
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt < self._max_attempts:
                        self._sleep(retry_after_seconds(exc.headers.get("Retry-After") if exc.headers is not None else None))
                        continue
                    raise WeatherConnectionError(
                        f"{self._provider} is unavailable after {attempt} attempts (HTTP {exc.code})"
                    ) from exc
                raise WeatherResponseError(f"{self._provider} returned HTTP {exc.code}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", None) or exc
                raise WeatherConnectionError(f"{self._provider} is unreachable: {reason}") from exc
        # Unreachable: the loop either returns or raises, but keep the type checker and intent clear.
        raise WeatherConnectionError(f"{self._provider} is unavailable")  # pragma: no cover
