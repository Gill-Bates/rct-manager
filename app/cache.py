#!/usr/bin/env python3
#
# app/cache.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Value store port and in-memory cache (Requirement 15)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from app.protocol.values import ScalarValue

type CacheKey = tuple[str, str]  # (device id, metric name)
type Origin = Literal["transaction", "periodic"]


class CacheFreshness(StrEnum):
    FRESH = "fresh"
    GRACE = "grace"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class CacheEntry:
    value: ScalarValue
    measured_at: datetime  # UTC, output only
    received_monotonic: float  # age is always derived from this axis
    origin: Origin


class ValueStore(Protocol):
    def get(self, key: CacheKey) -> CacheEntry | None: ...

    def put(
        self, key: CacheKey, value: ScalarValue, *, measured_at: datetime, received_monotonic: float, origin: Origin
    ) -> None: ...

    def invalidate(self, key: CacheKey) -> None: ...

    def classify(self, key: CacheKey, entry: CacheEntry, *, now_monotonic: float) -> CacheFreshness: ...

    def extend_ttl_when(self, window: Callable[[CacheKey], float | None]) -> None: ...


def age_seconds(entry: CacheEntry, now_monotonic: float) -> float:
    return max(0.0, now_monotonic - entry.received_monotonic)


class MemoryCache:
    """Entries age by comparison, never by deletion; the grace starts at the end of the TTL."""

    def __init__(self, ttl_seconds: float, grace_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._grace = grace_seconds
        self._entries: dict[CacheKey, CacheEntry] = {}
        self._window: Callable[[CacheKey], float | None] = lambda key: None

    def extend_ttl_when(self, window: Callable[[CacheKey], float | None]) -> None:
        """Bounded freshness window for periodically registered values; never unbounded.

        The device pushes some values rarely, so a bare TTL would flag them stale; a refresh read keeps them
        inside this window, and a value that is not refreshed ages out normally once it is exceeded.
        """
        self._window = window

    def get(self, key: CacheKey) -> CacheEntry | None:
        return self._entries.get(key)

    def put(
        self, key: CacheKey, value: ScalarValue, *, measured_at: datetime, received_monotonic: float, origin: Origin
    ) -> None:
        self._entries[key] = CacheEntry(value, measured_at, received_monotonic, origin)

    def invalidate(self, key: CacheKey) -> None:
        self._entries.pop(key, None)

    def classify(self, key: CacheKey, entry: CacheEntry, *, now_monotonic: float) -> CacheFreshness:
        age = age_seconds(entry, now_monotonic)
        ttl = max(self._ttl, self._window(key) or 0.0)
        if age <= ttl:
            return CacheFreshness.FRESH
        return CacheFreshness.GRACE if age <= ttl + self._grace else CacheFreshness.EXPIRED
