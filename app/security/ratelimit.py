#!/usr/bin/env python3
#
# app/security/ratelimit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Three independent rate counters (Requirement 13, 20.31, 20.32; ASVS v5.0.0-16.4.1).

Any internal failure rejects the request instead of admitting it.
"""

import logging
import math
import threading
from collections import deque
from ipaddress import IPv6Network, ip_address

from app.clock import Clock
from app.errors import RateLimited

log = logging.getLogger(__name__)

MAX_KEYS = 10_000


def _bucket(key: str) -> str:
    """IPv6 callers share one counter per /64 so address rotation inside a prefix does not help."""
    try:
        ip = ip_address(key)
    except ValueError:
        return key
    if ip.version == 6:
        return str(IPv6Network(f"{ip}/64", strict=False))
    return key


def caller_key(token_id: str | None, address: str) -> str:
    """Business-limit caller: token id plus source address (IPv6 reduced to its /64)."""
    return f"{token_id or '-'}|{_bucket(address)}"


class SlidingWindow:
    """Per-key request counter over a sliding window on the monotonic axis."""

    def __init__(self, limit: int, window_seconds: float, clock: Clock) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()  # shared by the event loop and threadpool handlers

    def set_limit(self, limit: int, window_seconds: float) -> None:
        """Live reconfiguration; existing timestamps stay valid under the new window."""
        with self._lock:
            self._limit = limit
            self._window = window_seconds

    def _prune(self, stamps: deque[float], now: float) -> None:
        while stamps and stamps[0] <= now - self._window:
            stamps.popleft()

    def _make_room(self, now: float) -> None:
        """Drops expired keys only; dropping an active one would reset a live limit."""
        for key in [k for k, s in self._hits.items() if not s or s[-1] <= now - self._window]:
            del self._hits[key]
        if len(self._hits) >= MAX_KEYS:  # still full: reject instead of forgetting an active caller
            raise RuntimeError("rate limiter capacity exhausted")

    def hit(self, key: str) -> float | None:
        """Count one request; returns None when allowed, else the seconds until a slot frees up."""
        key = _bucket(key)
        with self._lock:
            now = self._clock.monotonic()
            stamps = self._hits.get(key)
            if stamps is None:
                if len(self._hits) >= MAX_KEYS:
                    self._make_room(now)
                stamps = self._hits[key] = deque()
            self._prune(stamps, now)
            if len(stamps) >= self._limit:
                return max(0.0, stamps[0] + self._window - now)
            stamps.append(now)
            return None


class AuthFailureTracker:
    """Failed authentications per source address; reaching the limit blocks the address."""

    def __init__(self, limit: int, window_seconds: float, block_seconds: float, clock: Clock) -> None:
        self._limit = limit
        self._window = window_seconds
        self._block = block_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked_for(self, address: str) -> float:
        with self._lock:
            return self._blocked_for(_bucket(address))

    def _blocked_for(self, address: str) -> float:
        until = self._blocked_until.get(address)
        if until is None:
            return 0.0
        remaining = until - self._clock.monotonic()
        if remaining <= 0:
            del self._blocked_until[address]
            return 0.0
        return remaining

    def record_failure(self, address: str) -> None:
        with self._lock:
            self._record_failure(_bucket(address))

    def _record_failure(self, address: str) -> None:
        now = self._clock.monotonic()
        if address not in self._failures and len(self._failures) >= MAX_KEYS:
            for known in [a for a, s in self._failures.items() if not s or s[-1] <= now - self._window]:
                del self._failures[known]
            if len(self._failures) >= MAX_KEYS:
                # Capacity exhausted and no stale entries to evict: fail closed rather than
                # count a brand-new address into a shared bucket that blocked_for() never
                # queries for real callers.
                raise RuntimeError("auth failure capacity exhausted")
        stamps = self._failures.setdefault(address, deque())
        stamps.append(now)
        while stamps and stamps[0] <= now - self._window:
            stamps.popleft()
        if len(stamps) >= self._limit:
            if address not in self._blocked_until and len(self._blocked_until) >= MAX_KEYS:
                self._blocked_until = {a: u for a, u in self._blocked_until.items() if u > now}
                if len(self._blocked_until) >= MAX_KEYS:
                    raise RuntimeError("auth block capacity exhausted")
            self._blocked_until[address] = now + self._block
            stamps.clear()


class RateLimiter:
    """Business requests per caller, auth failures per source address, scrapes on their own counter."""

    def __init__(
        self,
        clock: Clock,
        *,
        requests: int,
        window_seconds: float,
        auth_fail_limit: int,
        auth_fail_window_seconds: float,
        auth_fail_block_seconds: float,
        scrape_requests: int,
        scrape_window_seconds: float,
    ) -> None:
        self._business = SlidingWindow(requests, window_seconds, clock)
        self._scrapes = SlidingWindow(scrape_requests, scrape_window_seconds, clock)
        self._auth = AuthFailureTracker(auth_fail_limit, auth_fail_window_seconds, auth_fail_block_seconds, clock)
        self._fallback_retry = max(window_seconds, scrape_window_seconds)

    def _reject(self, retry_after: float) -> RateLimited:
        return RateLimited(retry_after=max(1, math.ceil(retry_after)))

    def _count(self, window: SlidingWindow, caller: str, what: str) -> None:
        try:
            wait = window.hit(caller)
        except Exception:  # unreadable limiter state means rejection, not admission
            log.exception("Rate limiter state unreadable; rejecting the %s", what)
            raise self._reject(self._fallback_retry) from None
        if wait is not None:
            raise self._reject(wait)

    def check_request(self, caller: str) -> None:
        """Count a business request regardless of its eventual status code (Requirement 13.1)."""
        self._count(self._business, caller, "request")

    def check_scrape(self, caller: str) -> None:
        self._count(self._scrapes, caller, "scrape")

    def set_scrape_limit(self, requests: int, window_seconds: float) -> None:
        self._scrapes.set_limit(requests, window_seconds)

    def check_auth_blocked(self, address: str) -> None:
        try:
            wait = self._auth.blocked_for(address)
        except Exception:
            log.exception("Auth failure state unreadable; rejecting the request")
            raise self._reject(self._fallback_retry) from None
        if wait > 0:
            raise self._reject(wait)

    def record_auth_failure(self, address: str) -> None:
        try:
            self._auth.record_failure(address)
        except Exception:
            log.exception("Could not record an authentication failure; rejecting the request")
            raise self._reject(self._fallback_retry) from None
