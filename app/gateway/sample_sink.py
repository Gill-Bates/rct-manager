#!/usr/bin/env python3
#
# app/gateway/sample_sink.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""A tiny observer port the gateway notifies after a decoded periodic value.

The gateway owns no knowledge of what the observer does with a sample. This keeps the historization
of household consumption (an optimizer concern, backed by SQLite) off the transport/receiver path
and out of the gateway's import graph: the gateway depends only on this protocol, never on the
optimizer, admin or dispatch packages. The sink must do only very light, non-blocking work; any
real I/O belongs behind a decoupling mechanism the concrete observer owns.
"""

from datetime import datetime
from typing import Protocol

from app.protocol.values import ScalarValue


class MetricSampleSink(Protocol):
    """Receives one already-decoded periodic metric value, passively, after the cache was filled.

    ``sample`` runs inside the frame-receiver path, so it must return quickly and must never raise:
    a failing observer must not be able to damage the gateway connection or stop the cache update.
    The concrete implementation is responsible for its own error isolation.
    """

    def sample(
        self,
        device_id: str,
        metric_name: str,
        value: ScalarValue,
        measured_at: datetime,
    ) -> None: ...
