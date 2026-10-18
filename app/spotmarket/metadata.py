#!/usr/bin/env python3
#
# app/spotmarket/metadata.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Database-first bidding-zone metadata for the Spot Market tab.

The form never triggers an external request: it reads the last good :class:`BiddingZoneCatalog`
from memory, or from the admin database after a restart. Only :meth:`BiddingZoneMetadataCache.refresh`
talks to the provider; the daily spot-price retrieval calls it (Energy-Charts rate limits to roughly
two requests per minute and recommends client-side caching). A failed refresh keeps the stored list
and flags it ``stale`` instead of dropping or fabricating it.

The cache is read off the event loop (via ``asyncio.to_thread``) from the admin route, so it guards
its own state with a lock. The clock is injected so tests control timestamps without real time.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.spotmarket.base import SpotMarketError
from app.spotmarket.models import BiddingZoneCatalog

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ZoneListing:
    """What the admin route needs: the zones, the default, whether the data is stale, and when it
    was last fetched. ``stale`` is True when the latest refresh failed, so the UI can mark the list
    as potentially out of date.
    """

    zones: tuple[str, ...]
    default: str
    stale: bool
    last_update: datetime


class BiddingZoneMetadataCache:
    """A database-backed store for the provider's bidding-zone catalog; only ``refresh`` fetches."""

    def __init__(
        self,
        fetch: Callable[[], BiddingZoneCatalog],
        *,
        load: Callable[[], BiddingZoneCatalog | None] | None = None,
        save: Callable[[BiddingZoneCatalog], None] | None = None,
    ) -> None:
        self._fetch = fetch
        self._load = load
        self._save = save
        self._loaded = load is None
        self._lock = threading.Lock()  # guards state only, never held across a provider fetch
        self._refresh_lock = threading.Lock()  # serialises fetches so concurrent refreshes do not stack
        self._cached: BiddingZoneCatalog | None = None
        self._refresh_failed = False

    def get(self) -> ZoneListing:
        """Return the stored zone listing without any network call.

        Raises :class:`SpotMarketError` only when no catalog was ever stored.
        """
        with self._lock:
            self._ensure_loaded()
            if self._cached is None:
                raise SpotMarketError("No bidding-zone list is stored yet")
            return self._listing(self._cached, stale=self._refresh_failed)

    def refresh(self) -> ZoneListing:
        """Fetch the catalog from the provider and store it; a failure keeps the stored list as stale.

        Raises :class:`SpotMarketError` when the fetch fails and no catalog is stored at all.
        """
        with self._refresh_lock:
            with self._lock:
                self._ensure_loaded()
            try:
                catalog = self._fetch()
            except SpotMarketError as exc:
                with self._lock:
                    self._refresh_failed = True
                    cached = self._cached
                if cached is None:
                    raise
                log.warning("Bidding-zone list refresh failed (%s); keeping the stored list", exc)
                return self._listing(cached, stale=True)
            with self._lock:
                self._cached = catalog
                self._refresh_failed = False
            self._persist(catalog)
            return self._listing(catalog, stale=False)

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._loaded = True
            self._cached = self._load_persisted()

    def _load_persisted(self) -> BiddingZoneCatalog | None:
        try:
            return self._load()
        except Exception:
            # A damaged stored copy must not block the refresh; it is simply not used.
            log.warning("Stored bidding-zone catalog could not be read", exc_info=True)
            return None

    def _persist(self, catalog: BiddingZoneCatalog) -> None:
        if self._save is None:
            return
        try:
            self._save(catalog)
        except Exception:
            log.warning("Bidding-zone catalog could not be stored", exc_info=True)

    @staticmethod
    def _listing(catalog: BiddingZoneCatalog, *, stale: bool) -> ZoneListing:
        return ZoneListing(
            zones=catalog.zones,
            default=catalog.default,
            stale=stale,
            last_update=catalog.fetched_at,
        )


ZONE_CATALOG_KEY = "spotmarket_zone_catalog"


def zone_catalog_persistence(store) -> tuple[
    Callable[[], BiddingZoneCatalog | None], Callable[[BiddingZoneCatalog], None]
]:
    """``load``/``save`` hooks that keep the last good catalog in the admin settings store."""

    def load() -> BiddingZoneCatalog | None:
        data = store.get(ZONE_CATALOG_KEY)
        if data is None:
            return None
        return BiddingZoneCatalog(
            zones=tuple(data["zones"]), default=data["default"], fetched_at=datetime.fromisoformat(data["fetched_at"])
        )

    def save(catalog: BiddingZoneCatalog) -> None:
        store.put(ZONE_CATALOG_KEY, {
            "zones": list(catalog.zones), "default": catalog.default, "fetched_at": catalog.fetched_at.isoformat(),
        })

    return load, save
