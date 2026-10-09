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
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app import __version__

_log = logging.getLogger(__name__)
_RELEASE_API = "https://api.github.com/repos/Gill-Bates/rct-manager/releases/latest"
_CACHE_TTL = 3600
_ERROR_TTL = 60  # failures are retried soon instead of being cached for the full hour
_FORCE_MIN_INTERVAL = 30  # a forced refresh may not hit GitHub more often than this
# Same scheme/host/path allowlist the GUI applies in about.js releaseUrl(): the release link is
# attacker-influenced (it comes verbatim from the GitHub response), so a mismatching html_url is
# dropped server-side instead of being served to the browser (SEC-05).
_RELEASE_URL_HOST = "github.com"
_RELEASE_URL_PATH_PREFIX = "/Gill-Bates/rct-manager/releases/tag/"
_cache: dict | None = None
_cache_time = 0.0
_lock = threading.Lock()  # guards the cache state only
_fetch_lock = threading.Lock()  # serializes the network request


# PEP 440 style pre-release markers; rank orders them below the final release of the same numbers.
_PRERELEASE_RANK = {"dev": -1, "a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}
_VERSION_RE = re.compile(
    r"^v?(\d+(?:\.\d+)*)(?:[-._]?(dev|alpha|a|beta|b|preview|pre|rc|c)[-._]?(\d+)?)?", re.IGNORECASE
)


def _version_parts(value: str) -> tuple[int, ...]:
    match = _VERSION_RE.match(value)
    if match is None:
        return (0,)
    parts = tuple(int(part) for part in match.group(1).split("."))
    return parts + (0,) * max(0, 3 - len(parts))


def _version_key(value: str) -> tuple[tuple[int, ...], int, int]:
    """Sort key where ``2.0.0rc1`` < ``2.0.0``; a final release outranks every pre-release."""
    match = _VERSION_RE.match(value)
    if match is None or match.group(2) is None:
        return _version_parts(value), 3, 0
    return _version_parts(value), _PRERELEASE_RANK[match.group(2).lower()], int(match.group(3) or 0)


def _cached(force: bool) -> dict | None:
    """Return a copy of the cached result while it is fresh enough, else None."""
    with _lock:
        if _cache is None:
            return None
        if force:
            ttl = _FORCE_MIN_INTERVAL
        else:
            ttl = _ERROR_TTL if _cache["error"] else _CACHE_TTL
        if time.monotonic() - _cache_time < ttl:
            return _cache.copy()
    return None


def _release_url(value: object) -> str | None:
    """Return the release URL only if it matches the GitHub release-tag allowlist, else None."""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme == "https" and parts.hostname == _RELEASE_URL_HOST and parts.path.startswith(
        _RELEASE_URL_PATH_PREFIX
    ):
        return value
    return None


def _fetch() -> dict:
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
            raise TypeError("GitHub returned an invalid release")
        latest = data["tag_name"].removeprefix("v")
        if not latest or _version_parts(latest) == (0, 0, 0):
            raise ValueError("GitHub returned an invalid release version")
        result["latest_version"] = latest
        result["release_url"] = _release_url(data.get("html_url"))
        result["published_at"] = data.get("published_at")
        result["update_available"] = _version_key(latest) > _version_key(__version__)
    except HTTPError as exc:
        result["error"] = f"GitHub API error: {exc.code}"
    except (URLError, TimeoutError) as exc:
        result["error"] = f"Network error: {exc.reason if isinstance(exc, URLError) else 'Connection timeout'}"
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        result["error"] = f"Invalid release response: {exc}"
    except OSError as exc:
        _log.debug("Release check failed: %s", exc)
        result["error"] = "Network error while checking releases"
    return result


def check_for_updates(force: bool = False) -> dict:
    """Return the latest GitHub release from a cache; a forced refresh is rate-limited too."""
    global _cache, _cache_time

    cached = _cached(force)
    if cached is not None:
        return cached
    # One request in flight at a time; waiting callers re-check the cache and share its result.
    # The state lock is never held during network I/O.
    with _fetch_lock:
        cached = _cached(force)
        if cached is not None:
            return cached
        result = _fetch()
        with _lock:
            _cache = result.copy()
            _cache_time = time.monotonic()
        return result
