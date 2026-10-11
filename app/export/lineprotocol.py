#!/usr/bin/env python3
#
# app/export/lineprotocol.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""InfluxDB line protocol, accepted by InfluxDB 2 (/api/v2/write) and QuestDB (/write)."""

import math
from collections.abc import Iterable, Mapping

MAX_LINES_PER_BATCH = 500


def _clean(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ")


def escape_measurement(name: str) -> str:
    return _clean(name).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def escape_key(name: str) -> str:
    """Tag keys, tag values and field keys."""
    return _clean(name).replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _field(value: float) -> str:
    return repr(float(value))  # always a float literal; mixing integer and float columns breaks QuestDB


def build_lines(
    samples: Iterable[tuple[str, Mapping[str, str], float]], measurement: str, timestamp_ns: int
) -> list[str]:
    """One line per distinct tag set; every series becomes a float field of that line."""
    grouped: dict[tuple[tuple[str, str], ...], dict[str, float]] = {}
    for series, tags, value in samples:
        if not math.isfinite(value):
            continue
        key = tuple(sorted((k, v) for k, v in tags.items() if v != ""))
        grouped.setdefault(key, {})[series] = value
    head = escape_measurement(measurement)
    lines = []
    for tags, fields in sorted(grouped.items()):
        tag_part = "".join(f",{escape_key(k)}={escape_key(v)}" for k, v in tags)
        field_part = ",".join(f"{escape_key(name)}={_field(v)}" for name, v in sorted(fields.items()))
        lines.append(f"{head}{tag_part} {field_part} {timestamp_ns}")
    return lines


def batches(lines: list[str], size: int = MAX_LINES_PER_BATCH) -> list[str]:
    return ["\n".join(lines[i : i + size]) + "\n" for i in range(0, len(lines), size)]
