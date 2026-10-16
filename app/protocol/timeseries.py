#!/usr/bin/env python3
#
# app/protocol/timeseries.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Strict decoder for the TIMESERIES reply of the logger objects.

Documented wire contract (python-rctclient protocol notes, "The logger"): the request writes a
UNIX timestamp (INT32) to the object and the device answers with the data logged before it. The
reply is a sequence of 4-byte big-endian elements: the echoed query timestamp, followed by pairs of
a UNIX timestamp (unsigned) and an IEEE 754 single-precision value, so ``4 + 8 * n`` bytes. The
documentation names neither the entry order nor a sentinel value, so the decoder keeps the wire
order and assigns no meaning to any value; the consumer judges plausibility.
"""

import math
import struct
from dataclasses import dataclass
from datetime import UTC, datetime

from app.errors import ProtocolError

_HEAD = struct.Struct(">I")
_PAIR = struct.Struct(">If")
# A reply is one long frame (16-bit length), so this bounds a hostile or corrupted payload.
MAX_TIMESERIES_POINTS = (65_535 - 4) // _PAIR.size


@dataclass(frozen=True, slots=True)
class TimeSeriesPoint:
    timestamp: datetime  # timezone-aware UTC
    value: float  # unit as logged by the device object; never converted here


def decode_timeseries(payload: bytes, *, expected_query: int | None = None) -> tuple[TimeSeriesPoint, ...]:
    """Decode a TIMESERIES payload; any structural violation raises ``ProtocolError``.

    ``expected_query`` is the timestamp that was written; a different echo means the reply belongs
    to another request and is rejected instead of being trusted.
    """
    if len(payload) < _HEAD.size:
        raise ProtocolError("timeseries_truncated", size=len(payload))
    body = len(payload) - _HEAD.size
    if body % _PAIR.size:
        raise ProtocolError("timeseries_malformed", size=len(payload))
    if body // _PAIR.size > MAX_TIMESERIES_POINTS:
        raise ProtocolError("timeseries_too_long", size=len(payload))
    (echo,) = _HEAD.unpack_from(payload, 0)
    if expected_query is not None and echo != expected_query:
        raise ProtocolError("timeseries_query_mismatch")
    points: list[TimeSeriesPoint] = []
    for offset in range(_HEAD.size, len(payload), _PAIR.size):
        stamp, value = _PAIR.unpack_from(payload, offset)
        if not math.isfinite(value):
            raise ProtocolError("timeseries_invalid_value")
        points.append(TimeSeriesPoint(datetime.fromtimestamp(stamp, UTC), float(value)))
    return tuple(points)
