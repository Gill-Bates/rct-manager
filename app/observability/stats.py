#!/usr/bin/env python3
#
# app/observability/stats.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""In-process statistics that the exporter only reads."""

from dataclasses import dataclass, field

DEFAULT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


@dataclass(slots=True)
class Histogram:
    """Cumulative buckets are derived at export time; ``observe`` is O(buckets)."""

    bounds: tuple[float, ...] = DEFAULT_BUCKETS
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    count: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * len(self.bounds)

    def observe(self, seconds: float) -> None:
        for i, bound in enumerate(self.bounds):
            if seconds <= bound:
                self.counts[i] += 1
                break
        self.total += seconds
        self.count += 1


@dataclass(slots=True)
class ServiceCounters:
    requests: int = 0
    export_enabled: bool = False
    export_success: int = 0
    export_failures: int = 0
    export_last_success_unix: float | None = None
