#!/usr/bin/env python3
#
# app/observability/exporter.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Prometheus text exposition as a pure projection of cache and counters (Requirement 20).

Only a ValueStore, EndpointCounters and read-only callables are injected; there is no reference to
the gateway or the access serializer, so a scrape can never cause a device transaction.
"""

import logging
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.cache import CacheFreshness, ValueStore, age_seconds
from app.errors import ConfigError
from app.observability.stats import Histogram, ServiceCounters
from app.transport.counters import EndpointCounters

log = logging.getLogger(__name__)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@dataclass(frozen=True, slots=True)
class EndpointView:
    endpoint_id: str
    counters: EndpointCounters
    queue_length: Callable[[], int]
    budget_remaining: Callable[[], int]
    foreign_access_suspected: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class DeviceView:
    device_id: str
    durations: Histogram
    errors: Callable[[], int]
    last_success_at: Callable[[], datetime | None]
    periodic_registrations: Callable[[], int]
    cache_hits: Callable[[], int]
    cache_misses: Callable[[], int]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _number(value: float) -> str:
    return repr(float(value)) if not float(value).is_integer() else str(int(value))


class _Labels(str):
    """Rendered label set that keeps the pairs for the push export."""

    pairs: dict[str, str]


def _labels(**labels: str) -> str:
    if not labels:
        return ""
    rendered = _Labels("{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}")
    rendered.pairs = dict(labels)
    return rendered


class MetricsExporter:
    def __init__(
        self,
        store: ValueStore,
        monotonic: Callable[[], float],
        service: ServiceCounters,
        devices: Sequence[DeviceView],
        endpoints: Sequence[EndpointView],
        metric_names: Mapping[str, str],
        exposed: Sequence[str],
        enum_labels: Mapping[str, Mapping[int, str]] | None = None,
        help_texts: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._monotonic = monotonic
        self._service = service
        self._devices = list(devices)
        self._endpoints = list(endpoints)
        # Device/endpoint counters live on the per-request bindings and the transport endpoint, both
        # rebuilt from zero on a live device-list reload. Without a carry-over a _total counter would
        # fall across the swap within one process lifetime, which Prometheus reads as a reset. The
        # outgoing totals are folded in here on every view swap so each _total stays monotonic.
        self._carry_cache_hits = 0
        self._carry_cache_misses = 0
        self._carry_device_errors: dict[str, int] = {}
        self._carry_device_durations: dict[str, Histogram] = {}
        self._carry_endpoint: dict[tuple[str, str], int] = {}
        self._names = dict(metric_names)
        self._help = dict(help_texts or {})
        # Dropping unknown or duplicate entries would hide a configuration error as missing or
        # duplicated telemetry; build_metric_names() rejects bad names at startup for the same reason.
        unknown = [name for name in exposed if name not in self._names]
        if unknown:
            raise ConfigError("invalid_metrics_exposed_names", detail=f"not exportable: {', '.join(unknown)}")
        duplicates = sorted(name for name, count in Counter(exposed).items() if count > 1)
        if duplicates:
            raise ConfigError("invalid_metrics_exposed_names", detail=f"named more than once: {', '.join(duplicates)}")
        self._exposed = list(exposed)
        # StateSet: label -> codes sharing it (several codes may carry one label); order is stable.
        self._states: dict[str, dict[str, set[int]]] = {}
        for metric, labels in (enum_labels or {}).items():
            if labels and metric in self._names:
                states: dict[str, set[int]] = {}
                for code, label in labels.items():
                    states.setdefault(label, set()).add(int(code))
                self._states[metric] = states
        self._warned_codes: set[tuple[str, int]] = set()

    def set_exposed(self, names: list[str]) -> None:
        """Replace the exposed metric selection; the caller has validated the names."""
        self._exposed = list(names)

    def set_devices(self, views: Sequence[DeviceView]) -> None:
        """Replace the device views (live device-list reload); fold the outgoing totals forward."""
        for d in self._devices:
            self._carry_cache_hits += d.cache_hits()
            self._carry_cache_misses += d.cache_misses()
            self._carry_device_errors[d.device_id] = self._carry_device_errors.get(d.device_id, 0) + d.errors()
            carried = self._carry_device_durations.setdefault(d.device_id, Histogram(d.durations.bounds))
            self._accumulate_histogram(carried, d.durations)
        self._devices = list(views)

    def set_endpoints(self, views: Sequence[EndpointView]) -> None:
        """Replace the transport-endpoint views (live device-list reload); fold the outgoing totals forward."""
        for e in self._endpoints:
            for metric, value in self._endpoint_totals(e):
                key = (e.endpoint_id, metric)
                self._carry_endpoint[key] = self._carry_endpoint.get(key, 0) + value
        self._endpoints = list(views)

    @staticmethod
    def _accumulate_histogram(into: "Histogram", source: "Histogram") -> None:
        """Add one histogram's observations into a carry-over histogram sharing the same bounds."""
        for i, n in enumerate(source.counts):
            into.counts[i] += n
        into.total += source.total
        into.count += source.count

    @staticmethod
    def _endpoint_totals(e: EndpointView) -> tuple[tuple[str, int], ...]:
        """The transport ``*_total`` counter values of one endpoint view, keyed by metric name."""
        return (
            ("rct_transport_bytes_discarded_total", e.counters.discarded_bytes),
            ("rct_transport_crc_errors_total", e.counters.crc_errors),
            ("rct_transport_framing_errors_total", e.counters.framing_errors),
            ("rct_transport_unexpected_frames_total", e.counters.unexpected_frames),
        )

    def _state_samples(self, metric: str, device: str, value: float) -> list[tuple[str, str, float]]:
        # An unlabelled code maps to the fixed state "unknown"; the code stays out of the labels
        # to keep the cardinality bounded and goes to the log once instead.
        states, name = self._states[metric], self._names[metric]
        code = int(value) if value.is_integer() else None
        current = next((label for label, codes in states.items() if code in codes), None)
        if current is None and code is not None and (metric, code) not in self._warned_codes:
            self._warned_codes.add((metric, code))
            log.warning("Metric %s: enum code %d has no registry label; exporting state=unknown", metric, code)
        rows = [(label, 1.0 if label == current else 0.0) for label in states]
        if current is None:
            rows.append(("unknown", 1.0))
        return [(name, _labels(device=device, state=label), v) for label, v in rows]

    def render(self) -> str:
        out: list[str] = []

        def family(name: str, kind: str, help_text: str, samples: list[tuple[str, str, float]]) -> None:
            if not samples:
                return
            out.append(f"# HELP {name} {_help(help_text)}")
            out.append(f"# TYPE {name} {kind}")
            out.extend(f"{series}{labels} {_number(value)}" for series, labels, value in samples)

        self._families(family)
        return "\n".join(out) + "\n"

    def collect(self) -> list[tuple[str, dict[str, str], float]]:
        """The same samples as ``render`` as (series, tags, value); histogram buckets are left out."""
        rows: list[tuple[str, dict[str, str], float]] = []

        def family(name: str, kind: str, help_text: str, samples: list[tuple[str, str, float]]) -> None:
            for series, labels, value in samples:
                if not series.endswith("_bucket"):
                    rows.append((series, dict(getattr(labels, "pairs", {})), float(value)))

        self._families(family)
        return rows

    def _families(self, family) -> None:
        self._service_families(family)
        self._device_families(family)
        self._transport_families(family)
        self._value_families(family)

    def _service_families(self, family) -> None:
        hits = self._carry_cache_hits + sum(d.cache_hits() for d in self._devices)
        misses = self._carry_cache_misses + sum(d.cache_misses() for d in self._devices)
        family(
            "rct_api_requests_total",
            "counter",
            "HTTP requests handled.",
            [("rct_api_requests_total", "", self._service.requests)],
        )
        family(
            "rct_api_cache_hits_total",
            "counter",
            "Reads answered from the cache.",
            [("rct_api_cache_hits_total", "", hits)],
        )
        family(
            "rct_api_cache_misses_total",
            "counter",
            "Reads that needed the device.",
            [("rct_api_cache_misses_total", "", misses)],
        )
        if self._service.export_enabled:
            name = "rct_export_pushes_total"
            family(
                name, "counter", "Push export attempts by result.",
                [(name, _labels(result="ok"), self._service.export_success),
                 (name, _labels(result="error"), self._service.export_failures)],
            )  # fmt: skip
            if self._service.export_last_success_unix is not None:
                family(
                    "rct_export_last_success_timestamp_seconds", "gauge", "Unix time of the last successful push.",
                    [("rct_export_last_success_timestamp_seconds", "", self._service.export_last_success_unix)],
                )

    def _device_families(self, family) -> None:
        name = "rct_device_request_duration_seconds"
        samples: list[tuple[str, str, float]] = []
        for d in self._devices:
            carried = self._carry_device_durations.get(d.device_id)
            hist, cumulative = d.durations, 0
            for i, (bound, n) in enumerate(zip(hist.bounds, hist.counts, strict=True)):
                cumulative += n + (carried.counts[i] if carried is not None else 0)
                samples.append((f"{name}_bucket", _labels(device=d.device_id, le=_number(bound)), cumulative))
            total_count = hist.count + (carried.count if carried is not None else 0)
            total_sum = hist.total + (carried.total if carried is not None else 0.0)
            samples.append((f"{name}_bucket", _labels(device=d.device_id, le="+Inf"), total_count))
            samples.append((f"{name}_sum", _labels(device=d.device_id), total_sum))
            samples.append((f"{name}_count", _labels(device=d.device_id), total_count))
        family(name, "histogram", "Duration of device transactions.", samples)
        family(
            "rct_device_errors_total", "counter", "Failed device transactions.",
            [("rct_device_errors_total", _labels(device=d.device_id),
              self._carry_device_errors.get(d.device_id, 0) + d.errors()) for d in self._devices],
        )  # fmt: skip
        succeeded = [(d, d.last_success_at()) for d in self._devices]
        family(
            "rct_device_last_success_timestamp_seconds", "gauge", "Unix time of the last successful transaction.",
            [("rct_device_last_success_timestamp_seconds", _labels(device=d.device_id), t.timestamp())
             for d, t in succeeded if t is not None],
        )  # fmt: skip
        family(
            "rct_device_periodic_registrations", "gauge", "Registered periodic reads.",
            [("rct_device_periodic_registrations", _labels(device=d.device_id), d.periodic_registrations())
             for d in self._devices],
        )  # fmt: skip

    def _transport_families(self, family) -> None:
        rows = (
            ("rct_transport_queue_length", "gauge", "Queued transactions.", lambda e: e.queue_length()),
            ("rct_transport_budget_remaining", "gauge", "Remaining work budget.", lambda e: e.budget_remaining()),
            (
                "rct_transport_bytes_discarded_total",
                "counter",
                "Discarded bytes.",
                lambda e: e.counters.discarded_bytes,
            ),
            (
                "rct_transport_crc_errors_total",
                "counter",
                "Frames dropped for a CRC mismatch.",
                lambda e: e.counters.crc_errors,
            ),
            (
                "rct_transport_framing_errors_total",
                "counter",
                "Frames dropped for invalid framing or escaping.",
                lambda e: e.counters.framing_errors,
            ),
            (
                "rct_transport_unexpected_frames_total",
                "counter",
                "Unexpected frames.",
                lambda e: e.counters.unexpected_frames,
            ),
            (
                "rct_transport_foreign_access_suspected",
                "gauge",
                "1 when a foreign client is suspected.",
                lambda e: int(e.foreign_access_suspected()),
            ),
        )
        for name, kind, help_text, getter in rows:
            carried = kind == "counter"  # only the *_total counters carry over a view swap
            family(
                name, kind, help_text,
                [(name, _labels(endpoint=e.endpoint_id),
                  getter(e) + (self._carry_endpoint.get((e.endpoint_id, name), 0) if carried else 0))
                 for e in self._endpoints],
            )  # fmt: skip

    def _value_families(self, family) -> None:
        now = self._monotonic()
        ages: list[tuple[str, str, float]] = []
        for metric in self._exposed:
            samples: list[tuple[str, str, float]] = []
            for d in self._devices:
                key = (d.device_id, metric)
                entry = self._store.get(key)
                if entry is None or isinstance(entry.value, str):
                    continue
                freshness = self._store.classify(key, entry, now_monotonic=now)
                if freshness is CacheFreshness.EXPIRED:
                    continue
                value = float(entry.value)
                if not math.isfinite(value):
                    continue
                if metric in self._states:
                    samples.extend(self._state_samples(metric, d.device_id, value))
                else:
                    samples.append((self._names[metric], _labels(device=d.device_id), value))
                if freshness is CacheFreshness.GRACE:
                    ages.append(
                        (
                            "rct_device_metric_age_seconds",
                            _labels(device=d.device_id, metric=metric),
                            age_seconds(entry, now),
                        )
                    )
            text = self._help.get(metric) or f"Value of {metric}."
            if metric in self._states:
                text = text.rstrip(".") + (
                    ". One series per state label (label `state`): 1 for the current state, 0 for all others."
                )
            family(self._names[metric], "gauge", text, samples)
        # Requirement 20.26: exported for an entry whose age exceeds the ttl but keeps the grace
        # period. The value is the entry's total age, not the part beyond the ttl.
        family(
            "rct_device_metric_age_seconds",
            "gauge",
            "Total age of a cached value; exported only while the value is inside its grace period.",
            ages,
        )
