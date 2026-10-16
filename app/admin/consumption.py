#!/usr/bin/env python3
#
# app/admin/consumption.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Household-consumption historization: a light sample sink plus a loop-owned SQLite drain.

The sink implements the gateway's :class:`~app.gateway.sample_sink.MetricSampleSink` port. It runs
inside the frame-receiver path, so it does only very light work: filter for ``household_load_power``
and append ``(device, instant, watts)`` to a BOUNDED buffer. A non-finite or negative value is
kept as a NaN gap marker so the aggregator never integrates across a known-bad measurement.
No SQLite write and no blocking I/O happen there; an observer failure can never damage the gateway
connection or stop the cache update (the gateway isolates the call anyway).

A separate background task drains the buffer off the receiver path: it integrates the buffered power
samples into fixed 15-minute energy buckets (time-weighted, boundary-correct, see
``app.optimizer.consumption``) and upserts them, and prunes history beyond the retention window. The
buffer is bounded, so a stalled drain or a disk problem drops the oldest samples and logs rather
than growing without limit.

This module lives in ``app.admin`` (not ``app.gateway``) on purpose: it knows about SQLite and the
admin store, so placing it here keeps the gateway free of any import of the store or the optimizer
and avoids a gateway <-> admin/optimizer cycle.
"""

import asyncio
import logging
import math
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.admin.consumption_backfill import WINDOW_DAYS, resolve_zone
from app.admin.forecast_refresh import _store_thread
from app.admin.store import AdminStore
from app.clock import Clock
from app.optimizer.adapters import StoredConsumptionForecastProvider
from app.optimizer.adaptive import METHOD_ADAPTIVE, METHOD_MEDIAN
from app.optimizer.consumption import PowerSample, aggregate_samples, buckets_to_rows, floor_to_bucket
from app.optimizer.meter import COUNTER_METRICS, DAY_COUNTER, TOTAL_COUNTER, MeterStats, attribute_counter, meter_rows
from app.protocol.values import ScalarValue

log = logging.getLogger(__name__)

HOUSE_LOAD_METRIC = "household_load_power"
# The default local retention of consumption history. A code default for this first version, not an
# operator setting. Pruning is scoped to the consumption table, never price or solar-forecast rows.
RETENTION = timedelta(days=60)
# A hard cap on the in-memory buffer: at a 30 s periodic interval and a handful of devices this is
# well over a day of samples, yet it can never grow without bound. On overflow the oldest sample is
# dropped (fail-safe) and the drop is logged.
MAX_BUFFERED_SAMPLES = 20_000
# How often the drain integrates the buffer into buckets and writes them.
DRAIN_INTERVAL_SECONDS = 30.0
# How often retention pruning runs (far less often than the drain).
PRUNE_INTERVAL_SECONDS = 3600.0
MISMATCH_LOG_INTERVAL_SECONDS = 900.0
# Upper bound on the final drain at a regular shutdown, so a hung disk cannot hold the stop.
FINAL_DRAIN_TIMEOUT_SECONDS = 5.0
# Write batches that failed are kept for retry, bounded; beyond this the oldest is dropped and logged.
MAX_PENDING_BATCHES = 500
MAX_BUFFERED_COUNTER_READINGS = 5_000
# The next-day forecast is stored once per target day after this local hour, so the evening's
# measurements (a possible return) are already part of it.
FORECAST_ISSUE_HOUR = 22
# Stored runs of this age are compared with their measured actuals and logged.
FORECAST_REVIEW_DAYS = 3


def _finite_watts(value: ScalarValue) -> float | None:
    """A usable house-load power value: a real, finite, non-negative number; else ``None``.

    The caller turns ``None`` into a gap marker; the integration in the drain guards the same contract.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        return None
    return number


class ConsumptionSampleSink:
    """A :class:`MetricSampleSink` that buffers household-load samples for the drain.

    Only ``household_load_power`` is kept; every other metric is ignored. The buffer is a bounded
    deque: ``sample`` is O(1) and never blocks. The drain is the only reader.
    """

    def __init__(self, *, max_buffered: int = MAX_BUFFERED_SAMPLES) -> None:
        self._pending: deque[tuple[str, datetime, float]] = deque()
        self._counters: deque[tuple[str, str, datetime, float]] = deque()
        self._max_buffered = max_buffered
        self._dropped = 0
        self._dropped_counters = 0

    def _drop_oldest_counter(self) -> None:
        """Drop the oldest buffered counter reading on overflow and log it sparsely (as power does)."""
        self._counters.popleft()
        self._dropped_counters += 1
        if self._dropped_counters == 1 or self._dropped_counters % 1000 == 0:
            log.warning(
                "Consumption counter buffer full (%d); dropping oldest counter readings (%d dropped so far)",
                MAX_BUFFERED_COUNTER_READINGS, self._dropped_counters,
            )

    def sample(self, device_id: str, metric_name: str, value: ScalarValue, measured_at: datetime) -> None:
        if metric_name in COUNTER_METRICS:
            reading = _finite_watts(value)  # same contract: finite and non-negative, else not a reading
            if reading is not None:
                if len(self._counters) >= MAX_BUFFERED_COUNTER_READINGS:
                    self._drop_oldest_counter()
                self._counters.append((device_id, metric_name, measured_at, reading))
            return
        if metric_name != HOUSE_LOAD_METRIC:
            return
        watts = _finite_watts(value)
        if watts is None:
            watts = math.nan  # known measurement gap: reaches the aggregator, adds no energy or count
        if len(self._pending) >= self._max_buffered:
            # Fail-safe on overload: drop the oldest rather than grow unbounded, and log sparsely.
            self._pending.popleft()
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                log.warning(
                    "Consumption sample buffer full (%d); dropping oldest samples (%d dropped so far)",
                    self._max_buffered, self._dropped,
                )
        self._pending.append((device_id, measured_at, watts))

    def drain(self) -> dict[str, list[PowerSample]]:
        """Remove and return all buffered samples, grouped by device. Called only by the drain task."""
        grouped: dict[str, list[PowerSample]] = {}
        while self._pending:
            device_id, measured_at, watts = self._pending.popleft()
            grouped.setdefault(device_id, []).append(PowerSample(measured_at, watts))
        return grouped

    def drain_counters(self) -> dict[str, dict[str, list[tuple[datetime, float]]]]:
        """Remove and return buffered counter readings as ``{device: {metric: [(instant, Wh)]}}``."""
        grouped: dict[str, dict[str, list[tuple[datetime, float]]]] = {}
        while self._counters:
            device_id, metric, measured_at, value = self._counters.popleft()
            grouped.setdefault(device_id, {}).setdefault(metric, []).append((measured_at, value))
        return grouped

    def restore_counters(self, device_id: str, counters: dict[str, list[tuple[datetime, float]]]) -> None:
        """Put drained readings back at the front of the buffer (oldest first), bounded."""
        for metric, series in counters.items():
            for instant, value in sorted(series, reverse=True):
                self._counters.appendleft((device_id, metric, instant, value))
        while len(self._counters) > MAX_BUFFERED_COUNTER_READINGS:
            self._drop_oldest_counter()

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def dropped_counters(self) -> int:
        return self._dropped_counters


@dataclass(slots=True)
class _Batch:
    """One write unit; the id makes a retry idempotent in the store."""

    batch_id: str
    power_rows: list[dict]
    meter_rows: list[dict]
    readings: list[tuple[str, str, float]]
    state: list[tuple[str, str, float]]


class ConsumptionHistorian:
    """Owns the sink and the periodic SQLite drain/prune off the receiver path.

    Two sources feed one history. The power samples are integrated time-weighted; the cumulative
    load counter is differenced, so consumption during an app stop is recovered as a flagged
    ESTIMATE (see ``app.optimizer.meter``). The drain keeps the last power sample and the last counter
    reading of each device as carryover, and the counter reading is also persisted, so a span
    straddling two drains or an app restart is attributed exactly once. A failed write stays queued
    under its batch id and is retried; the store recognises an already-applied batch. SQLite errors
    are caught and logged; they must never reach the receiver path or stop the loop.
    """

    def __init__(
        self, store: AdminStore, clock: Clock, *, max_buffered: int = MAX_BUFFERED_SAMPLES,
        timezone: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._timezone = timezone or (lambda: "UTC")
        self._reviewed: set[tuple[str, str, str]] = set()
        self.sink = ConsumptionSampleSink(max_buffered=max_buffered)
        self._carryover: dict[str, PowerSample] = {}
        self._anchors: dict[str, dict[str, tuple[datetime, float]]] = {}
        self._anchor_loaded: set[str] = set()
        self._logged_bucket: dict[tuple[str, str], datetime] = {}
        self._mismatch_log: dict[str, tuple[float, int]] = {}
        self._queue: dict[str, deque[_Batch]] = {}
        self._instance = uuid.uuid4().hex[:8]
        self._sequence = 0
        self._last_prune_monotonic: float | None = None
        self.meter_stats = MeterStats()

    async def run(self) -> None:
        """Drain loop: integrate buffered samples into buckets and prune retention, forever."""
        try:
            while True:
                await self._clock.sleep(DRAIN_INTERVAL_SECONDS)
                try:
                    await self.drain_once()
                except Exception:
                    log.exception("Consumption history drain failed")
                try:
                    await self._maybe_prune()
                except Exception:
                    log.exception("Consumption history prune failed")
                try:
                    await self.record_forecasts_once()
                except Exception:
                    log.exception("Consumption forecast recording failed")
        except asyncio.CancelledError:
            await self.final_drain()
            raise

    async def final_drain(self, timeout: float = FINAL_DRAIN_TIMEOUT_SECONDS) -> None:
        """Flush what is buffered and queued once, bounded; used on a regular shutdown."""
        try:
            await asyncio.wait_for(self.drain_once(), timeout)
        except Exception:  # TimeoutError included; shutdown must proceed regardless
            log.warning("Final consumption drain did not complete; buffered samples may be lost", exc_info=True)

    async def drain_once(self) -> None:
        grouped = self.sink.drain()
        counters = self.sink.drain_counters()
        for device_id in sorted(set(grouped) | set(counters) | {d for d, q in self._queue.items() if q}):
            try:
                await self._drain_device(device_id, grouped.get(device_id, []), counters.get(device_id, {}))
            except Exception:
                log.exception("Consumption drain for device %s failed", device_id)

    async def _drain_device(
        self, device_id: str, samples: list[PowerSample], counters: dict[str, list[tuple[datetime, float]]]
    ) -> None:
        power_rows: list[dict] = []
        if samples:
            samples.sort(key=lambda sample: sample.measured_at)
            # The previous drain's last sample joins the integration so a straddling interval is
            # covered, but is not counted again; a gap beyond the maximum integration span adds no energy.
            carry = self._carryover.get(device_id)
            self._carryover[device_id] = samples[-1]
            power_rows = buckets_to_rows(aggregate_samples(samples, carry=carry))
        meter: list[dict] = []
        readings: list[tuple[str, str, float]] = []
        state: list[tuple[str, str, float]] = []
        if counters:
            try:
                await self._load_anchors(device_id)
            except Exception:
                # Without the persisted anchor a stop could not be bridged: keep the readings for the next drain.
                log.exception("Could not restore the load counter state of %s; retrying", device_id)
                self.sink.restore_counters(device_id, counters)
            else:
                meter, readings, state = self._attribute_counters(device_id, counters)
        if power_rows or meter or readings or state:
            self._sequence += 1
            self._queue.setdefault(device_id, deque()).append(
                _Batch(f"{self._instance}:{self._sequence}", power_rows, meter, readings, state)
            )
        await self._flush(device_id)

    def _attribute_counters(
        self, device_id: str, counters: dict[str, list[tuple[datetime, float]]]
    ) -> tuple[list[dict], list[tuple[str, str, float]], list[tuple[str, str, float]]]:
        anchors = self._anchors.setdefault(device_id, {})
        total = sorted(counters.get(TOTAL_COUNTER, []))
        day = sorted(counters.get(DAY_COUNTER, []))
        day_with_anchor = ([anchors[DAY_COUNTER]] if DAY_COUNTER in anchors else []) + day
        attribution = attribute_counter(total, day_with_anchor, anchor=anchors.get(TOTAL_COUNTER))
        self.meter_stats.merge(attribution.stats)
        stats = attribution.stats
        if stats.discarded_negative or stats.discarded_implausible or stats.discarded_stalled or stats.discarded_long_gap:
            log.warning(
                "Load counter readings of %s were partly rejected (reset/negative %d, implausible %d, "
                "stalled %d, too-long gap %d)",
                device_id, stats.discarded_negative, stats.discarded_implausible, stats.discarded_stalled,
                stats.discarded_long_gap,
            )
        if stats.cross_check_mismatches:
            now = self._clock.monotonic()
            last, suppressed = self._mismatch_log.get(device_id, (None, 0))
            if last is None or now - last >= MISMATCH_LOG_INTERVAL_SECONDS:
                # INFO, not WARNING: a cross-check mismatch discards nothing. The total counter is the
                # primary source and is retained, so there is no data loss and no operator action; on
                # devices whose day-counter semantics are unverified this is a permanent, expected
                # state. The per-interval raw total/day divergence is at DEBUG in app.optimizer.meter.
                log.info(
                    "Load counter day/total cross-check mismatch for %s: %d observed (%d previously suppressed); "
                    "total counter retained",
                    device_id, stats.cross_check_mismatches + suppressed, suppressed,
                )
                self._mismatch_log[device_id] = (now, 0)
            else:
                self._mismatch_log[device_id] = (last, suppressed + stats.cross_check_mismatches)
        readings: list[tuple[str, str, float]] = []
        state: list[tuple[str, str, float]] = []
        for metric, series in counters.items():
            series.sort()
            for instant, value in series:
                bucket = floor_to_bucket(instant)
                if self._logged_bucket.get((device_id, metric)) != bucket:
                    self._logged_bucket[(device_id, metric)] = bucket
                    readings.append((metric, instant.astimezone(UTC).isoformat(), value))
            newest = series[-1]
            anchors[metric] = newest
            state.append((metric, newest[0].astimezone(UTC).isoformat(), newest[1]))
        return meter_rows(attribution.buckets), readings, state

    async def _load_anchors(self, device_id: str) -> None:
        """Restore the newest persisted counter readings once, so a stop is bridged after a restart."""
        if device_id in self._anchor_loaded:
            return
        stored = await _store_thread(self._store.get_consumption_counter_state, device_id)
        anchors = self._anchors.setdefault(device_id, {})
        for metric, (measured_at, value) in stored.items():
            anchors.setdefault(metric, (datetime.fromisoformat(measured_at), float(value)))
        self._anchor_loaded.add(device_id)

    async def _flush(self, device_id: str) -> None:
        queue = self._queue.get(device_id)
        while queue and len(queue) > MAX_PENDING_BATCHES:
            queue.popleft()
            log.error("Consumption write queue for %s overflowed; dropped the oldest batch", device_id)
        while queue:
            batch = queue[0]
            try:
                await _store_thread(
                    self._store.upsert_consumption_intervals,
                    device_id, batch.power_rows, batch.batch_id, batch.meter_rows, batch.readings, batch.state,
                )
            except Exception:
                log.exception("Consumption write failed; %d batch(es) kept for retry", len(queue))
                return
            queue.popleft()

    async def record_forecasts_once(self) -> None:
        """Store tomorrow's forecast per known device once per target day, and review older runs."""
        now = self._clock.now().astimezone(UTC)
        zone = self._timezone() or "UTC"
        if now.astimezone(ZoneInfo(zone)).hour < FORECAST_ISSUE_HOUR:
            return
        for device_id in sorted(set(self._carryover) | set(self._anchors)):
            await _store_thread(self._record_forecasts_sync, device_id, now, zone)

    def _record_forecasts_sync(self, device_id: str, now: datetime, zone: str) -> None:
        provider = StoredConsumptionForecastProvider(self._store, device_id=device_id, timezone=zone)
        for method in (METHOD_ADAPTIVE, METHOD_MEDIAN):
            forecast = provider.next_day(now, method)
            if forecast is None or self._store.has_consumption_forecast_run(device_id, forecast.target_date, method):
                continue
            self._store.record_consumption_forecast(
                device_id, now.isoformat(), forecast.target_date, method, forecast.analysis.explanation(),
                forecast.values,
            )
        local_today = now.astimezone(ZoneInfo(zone)).date().isoformat()
        for run, result in provider.evaluate_runs(now, now - timedelta(days=FORECAST_REVIEW_DAYS)):
            key = (device_id, run["target_date"], run["method"])
            if run["target_date"] >= local_today or result is None or key in self._reviewed:
                continue
            self._reviewed.add(key)
            log.info(
                "Consumption forecast %s for %s: %+.1f%% daily energy, quarter-hour MAE %.1f Wh (%d slots)",
                run["method"], run["target_date"], result.daily_error_pct, result.mae_wh, result.slots,
            )

    async def _maybe_prune(self) -> None:
        now_monotonic = self._clock.monotonic()
        if (
            self._last_prune_monotonic is not None
            and now_monotonic - self._last_prune_monotonic < PRUNE_INTERVAL_SECONDS
        ):
            return
        self._last_prune_monotonic = now_monotonic
        cutoff = (self._clock.now().astimezone(UTC) - RETENTION).isoformat()
        await _store_thread(self._store.prune_consumption_intervals, cutoff)
        # Day totals are keyed by local date and kept for the same 60-day window.
        local_today = self._clock.now().astimezone(resolve_zone(self._timezone())).date()
        await _store_thread(self._store.prune_consumption_daily, (local_today - timedelta(days=WINDOW_DAYS)).isoformat())
