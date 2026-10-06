#!/usr/bin/env python3
#
# app/observability/names.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Prometheus metric names for registry metrics; formed and checked at startup (Requirement 20.10 to 20.14)."""

import re
from collections.abc import Iterable

from app.catalog.registry import RegistryEntry
from app.errors import ConfigError

_PROMETHEUS_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_UNSAFE = re.compile(r"[^a-z0-9_]")
_BASE_UNITS = {
    "w": "watts",
    "wh": "watt_hours",
    "v": "volts",
    "a": "amperes",
    "hz": "hertz",
    "°c": "celsius",
    "s": "seconds",
    "va": "volt_amperes",
    "var": "var",
    "ah": "ampere_hours",
    "ohm": "ohms",
    "ratio": "ratio",
}
RESERVED_PREFIXES = ("rct_api_", "rct_device_", "rct_transport_")


def normalize(name: str) -> str:
    """Lower-case, replace everything outside ``[a-z0-9_]``, collapse and trim underscores (20.12)."""
    return re.sub(r"_+", "_", _UNSAFE.sub("_", name.lower())).strip("_")


def prometheus_name(entry: RegistryEntry) -> str:
    if entry.prometheus_name:
        return entry.prometheus_name
    base = f"rct_{normalize(entry.name)}"
    suffix = _BASE_UNITS.get(entry.unit.lower())
    return f"{base}_{suffix}" if suffix and not base.endswith(f"_{suffix}") else base


def metric_help(entry: RegistryEntry) -> str:
    """HELP text from the registry description; a missing or name-only description falls back to a readable name."""
    text = " ".join(entry.description.split())
    if text and normalize(text) != normalize(entry.name):
        return text
    readable = entry.name.replace("_", " ").capitalize()
    return f"{readable} ({entry.unit})" if entry.unit else readable


def build_metric_names(entries: Iterable[RegistryEntry]) -> dict[str, str]:
    """Map metric name to Prometheus name; abort on an invalid name or a collision (20.13, 20.14)."""
    result: dict[str, str] = {}
    owner: dict[str, str] = {}
    for entry in entries:
        name = prometheus_name(entry)
        if not _PROMETHEUS_NAME.fullmatch(name) or name.startswith(RESERVED_PREFIXES):
            raise ConfigError("invalid_metric_name", detail=f"metric {entry.name}: rejected Prometheus name {name!r}")
        if name in owner:
            raise ConfigError(
                "metric_name_collision",
                detail=f"metrics {owner[name]} and {entry.name} both map to the Prometheus name {name}",
            )
        owner[name] = entry.name
        result[entry.name] = name
    return result
