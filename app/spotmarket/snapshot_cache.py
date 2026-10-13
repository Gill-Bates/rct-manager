#!/usr/bin/env python3
#
# app/spotmarket/snapshot_cache.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Bounded, last-known-good cache for the dashboard's day-ahead snapshot."""

import logging
import threading
import time
from collections.abc import Callable

from app.spotmarket.base import SpotMarketError, SpotMarketNotAvailableYet
from app.spotmarket.models import SpotMarketConfig, SpotPriceSnapshot

REFRESH_SECONDS = 15 * 60
log = logging.getLogger(__name__)


class SpotSnapshotCache:
    def __init__(
        self, fetch: Callable[[SpotMarketConfig], SpotPriceSnapshot], refresh_seconds: int = REFRESH_SECONDS
    ) -> None:
        self._fetch = fetch
        self._refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._config: SpotMarketConfig | None = None
        self._snapshot: SpotPriceSnapshot | None = None
        self._failed = False
        self._next_fetch = 0.0
        self._refreshing = False
        self._generation = 0

    def get(self, config: SpotMarketConfig) -> tuple[SpotPriceSnapshot | None, bool]:
        with self._lock:
            if config != self._config:
                self._generation += 1
                self._config = config
                self._snapshot = None
                self._failed = False
                self._next_fetch = 0.0
            if not config.enabled:
                return None, False
            now = time.monotonic()
            if now >= self._next_fetch and not self._refreshing:
                self._next_fetch = now + self._refresh_seconds
                self._refreshing = True
                threading.Thread(
                    target=self._refresh, args=(config, self._generation), daemon=True,
                    name="spot-snapshot-refresh",
                ).start()
            return self._snapshot, self._failed

    def _refresh(self, config: SpotMarketConfig, generation: int) -> None:
        try:
            snapshot = self._fetch(config)
            failed = False
        except SpotMarketNotAvailableYet:
            snapshot = None
            failed = False
        except SpotMarketError:
            snapshot = None
            failed = True
        except Exception:
            log.exception("Spot snapshot refresh failed unexpectedly")
            snapshot = None
            failed = True
        with self._lock:
            self._refreshing = False
            if generation == self._generation:
                if snapshot is not None:
                    self._snapshot = snapshot
                self._failed = failed
