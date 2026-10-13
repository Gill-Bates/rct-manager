#!/usr/bin/env python3
#
# app/spotmarket/metadata.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Cache-first bidding-zone metadata for the Spot Market tab.

Opening the Spot Market form must not trigger an external request every time: Energy-Charts rate
limits its API (roughly two requests per minute, bursts of two) and explicitly recommends
client-side caching. This cache holds the last successful :class:`BiddingZoneCatalog` and serves it
without a network call while it is fresh. A stale entry is refreshed on demand; if the refresh
fails, the last good list is returned, flagged ``stale``, rather than fabricated or dropped.

The cache is read off the event loop (via ``asyncio.to_thread``) from the admin route, so it guards
its own state with a lock. The clock is injected so tests control freshness without real time.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.spotmarket.base import SpotMarketError
from app.spotmarket.models import BiddingZoneCatalog

log = logging.getLogger(__name__)

# How long a fetched catalog is served without a refresh. The zone set changes rarely, and the rate
# limit makes a long TTL the right default; a form open inside this window costs no network call.
DEFAULT_TTL = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class ZoneListing:
    """What the admin route needs: the zones, the default, whether the data is stale, and when it
    was last fetched. ``stale`` is True when the served catalog is older than the TTL (a refresh was
    attempted and failed) so the UI can mark the list as potentially out of date.
    """

    zones: tuple[str, ...]
    default: str
    stale: bool
    last_update: datetime


class BiddingZoneMetadataCache:
    """A single-flight, cache-first store for the provider's bidding-zone catalog."""

    def __init__(
        self,
        fetch: Callable[[], BiddingZoneCatalog],
        *,
        ttl: timedelta = DEFAULT_TTL,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._cached: BiddingZoneCatalog | None = None

    def get(self) -> ZoneListing:
        """Return the zone listing, cache-first.

        A fresh cached catalog is returned without any network call. A missing or stale catalog
        triggers one refresh; if that refresh fails and a previous catalog exists, the previous one
        is returned flagged ``stale`` instead of raising, so a transient provider failure never
        blanks the form. Only a failure with no catalog at all propagates.
        """
        with self._lock:
            now = self._now()
            if self._cached is not None and now - self._cached.fetched_at < self._ttl:
                return self._listing(self._cached, stale=False)
            try:
                self._cached = self._fetch()
                return self._listing(self._cached, stale=False)
            except SpotMarketError:
                if self._cached is None:
                    raise
                # A refresh failed but a last-good catalog exists: serve it, clearly stale. Price
                # data is never faked, but a slightly old zone *list* is safe to keep showing.
                log.info("Bidding-zone metadata refresh failed; serving the last known list as stale")
                return self._listing(self._cached, stale=True)

    @staticmethod
    def _listing(catalog: BiddingZoneCatalog, *, stale: bool) -> ZoneListing:
        return ZoneListing(
            zones=catalog.zones,
            default=catalog.default,
            stale=stale,
            last_update=catalog.fetched_at,
        )
