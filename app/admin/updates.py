#!/usr/bin/env python3
#
# app/admin/updates.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Check the latest RCT Manager release for the administration page."""

import json
import logging
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app import __version__

_log = logging.getLogger(__name__)
_RELEASE_API = "https://api.github.com/repos/Gill-Bates/rct-manager/releases/latest"
_CACHE_TTL = 3600
_cache: dict | None = None
_cache_time = 0.0
_lock = threading.Lock()


def _version_parts(value: str) -> tuple[int, ...]:
    match = re.match(r"^v?(\d+(?:\.\d+)*)", value, re.IGNORECASE)
    if match is None:
        return (0,)
    parts = tuple(int(part) for part in match.group(1).split("."))
    return parts + (0,) * max(0, 3 - len(parts))


def check_for_updates(force: bool = False) -> dict:
    """Return the latest GitHub release, using a one-hour cache unless forced."""
    global _cache, _cache_time

    with _lock:
        if not force and _cache is not None and time.monotonic() - _cache_time < _CACHE_TTL:
            return _cache.copy()

        result = {
            "update_available": False,
            "current_version": __version__,
            "latest_version": None,
            "release_url": None,
            "published_at": None,
            "error": None,
        }
        request = Request(_RELEASE_API, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"rct-manager/{__version__}",
        })
        try:
            with urlopen(request, timeout=10) as response:
                data = json.load(response)
            if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str):
                # TypeError, not ValueError: this is a shape mismatch, and the handler below catches
                # both into the same "Invalid release response: ..." result, so nothing observable
                # changes. The version check further down keeps ValueError, which is a value problem.
                raise TypeError("GitHub returned an invalid release")
            latest = data["tag_name"].removeprefix("v")
            if not latest or _version_parts(latest) == (0, 0, 0):
                raise ValueError("GitHub returned an invalid release version")
            result["latest_version"] = latest
            result["release_url"] = data.get("html_url")
            result["published_at"] = data.get("published_at")
            result["update_available"] = _version_parts(latest) > _version_parts(__version__)
        except HTTPError as exc:
            result["error"] = f"GitHub API error: {exc.code}"
        except (URLError, TimeoutError) as exc:
            result["error"] = f"Network error: {exc.reason if isinstance(exc, URLError) else 'Connection timeout'}"
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            result["error"] = f"Invalid release response: {exc}"
        except OSError as exc:
            _log.debug("Release check failed: %s", exc)
            result["error"] = "Network error while checking releases"

        _cache = result.copy()
        _cache_time = time.monotonic()
        return result
