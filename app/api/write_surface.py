#!/usr/bin/env python3
#
# app/api/write_surface.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The single authoritative description of the write-only HTTP surface (Requirement 25).

The three write routers (``writes``, ``dispatch``, ``energy``) are registered together and gated
on the live write switch via ``require_write_enabled``. The method-and-path shapes of those routes
are protocol knowledge that used to be encoded a second time by hand in ``problems.py`` to turn a
404/405 on a disabled write route into the ``write_disabled`` problem. That duplicate had already
diverged (it matched ``/energy/command`` for every method, while the router defines only ``POST``).

This module is the one place that knowledge lives. ``problems._is_write_path`` consumes it, so a
change to a write route's method or path is made here and nowhere else.
"""

from collections.abc import Sequence

# Each entry is (allowed methods, path-segment matcher). ``segments`` is the request path split on
# "/" with a trailing slash stripped, so the leading empty string is index 0 and ``/api/v1/devices``
# occupies indices 0..3 with the device id at index 4. A matcher inspects only the segments after
# the device id and must tolerate a short path without raising: it runs inside the global exception
# handler, where an IndexError would turn a plain 404 into an unhandled 500.
_PREFIX = ("", "api", "v1", "devices")


def _metrics(segments: Sequence[str]) -> bool:
    # PUT /api/v1/devices/{id}/metrics/{metric_name}
    return len(segments) == 7 and segments[5] == "metrics"


def _actions(segments: Sequence[str]) -> bool:
    # POST /api/v1/devices/{id}/actions/{action_name}
    return len(segments) == 7 and segments[5] == "actions"


def _dispatch(segments: Sequence[str]) -> bool:
    # POST|GET|DELETE /api/v1/devices/{id}/battery/dispatch
    return len(segments) == 7 and tuple(segments[5:7]) == ("battery", "dispatch")


def _energy_status(segments: Sequence[str]) -> bool:
    # GET /api/v1/devices/{id}/energy
    return len(segments) == 6 and segments[5] == "energy"


def _energy_command(segments: Sequence[str]) -> bool:
    # POST /api/v1/devices/{id}/energy/command
    return len(segments) == 7 and tuple(segments[5:7]) == ("energy", "command")


# Methods are a frozenset so the match is method-exact; an undefined method on a write path is a
# 405 that must be classified like the defined ones, not pass through a method-agnostic clause.
WRITE_ROUTES: tuple[tuple[frozenset[str], object], ...] = (
    (frozenset({"PUT"}), _metrics),
    (frozenset({"POST"}), _actions),
    (frozenset({"POST", "GET", "DELETE"}), _dispatch),
    (frozenset({"GET"}), _energy_status),
    (frozenset({"POST"}), _energy_command),
)


def is_write_path(method: str, path: str) -> bool:
    """True when ``method`` on ``path`` targets a route that only exists with write support."""
    segments = path.rstrip("/").split("/")
    if tuple(segments[:4]) != _PREFIX:
        return False
    return any(
        method in methods and matcher(segments) for methods, matcher in WRITE_ROUTES
    )
