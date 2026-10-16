#!/usr/bin/env python3
#
# app/optimizer/adapters.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Domain adapters between persisted/local data and the pure Dry-Run optimizer.

This is the impure side of the optimizer boundary. It may read the admin store, the gateway cache
(through the energy-readings port) and operator configuration; it builds a
:class:`~app.optimizer.models.PlanningInput` from already-local data and hands it to the pure
:class:`~app.optimizer.planner.DynamicChargeOptimizer`. No module here is on the pure-algorithm
isolation list, and nothing here performs a provider network request or an inverter read: a plan
preview is built exclusively from SQLite and the periodic-read cache.

Missing hard preconditions are reported as explicit statuses, never as a 500. A preview with no
usable consumption history uses the configured household-load baseline and marks its uncertainty.
"""

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.clock import Clock
from app.optimizer.adaptive import (
    METHOD_ADAPTIVE,
    AdaptiveAnalysis,
    AdaptiveConsumptionForecaster,
    ForecastEvaluation,
    NextDayForecast,
    evaluate_forecast,
)
from app.optimizer.consumption import (
    MIN_HORIZON_COVERAGE_RATIO,
    MIN_PRIMARY_SAMPLES,
    BaselineConsumptionForecastProvider,
    rows_to_history,
)
from app.optimizer.models import (
    BatteryState,
    ConsumptionForecast,
    ForecastBasis,
    ForecastConfidence,
    PlanningInput,
    SolarForecast,
    SolarForecastInterval,
)
from app.optimizer.planner import DynamicChargeOptimizer, PlanParameters, planning_start
from app.spotmarket.models import SpotMarketConfig, SpotPriceInterval, SpotPriceSnapshot

log = logging.getLogger(__name__)

# Dry-Run horizon: enough to use the published day-ahead curve (which reaches to the end of
# tomorrow) without claiming more than the forecasts can back. 24..36 hours; 30 h is the default.
DEFAULT_HORIZON = timedelta(hours=30)
# Below this many well-covered buckets, the preview marks a history forecast as low confidence.
MIN_HISTORY_BUCKETS = MIN_PRIMARY_SAMPLES
# How much consumption history to read back for the forecast. Matches the forecaster's own lookback.
HISTORY_LOOKBACK = timedelta(days=60)
# What the plan's forecasts are made of, echoed into the plan's assumptions.
FORECAST_BASIS = (
    "consumption: adaptive profile (everyday profile plus phase detection; median of stored history "
    "as fallback when history is insufficient); solar: stored weather-based forecast; "
    "prices: stored day-ahead spot prices"
)


class PlanStatus(StrEnum):
    """Why a Dry-Run plan could or could not be produced; mapped straight onto the admin API."""

    OK = "ok"
    INSUFFICIENT_HISTORY = "insufficient_history"
    SPOT_DISABLED = "spot_disabled"
    SPOT_PRICES_UNAVAILABLE = "spot_prices_unavailable"
    SOLAR_NOT_CONFIGURED = "solar_not_configured"
    SOLAR_FORECAST_UNAVAILABLE = "solar_forecast_unavailable"
    BATTERY_STATE_UNAVAILABLE = "battery_state_unavailable"


@dataclass(frozen=True, slots=True)
class ForecastWindow:
    """The time ranges the plan's inputs actually cover, for the preview to show honestly."""

    consumption_from: datetime | None = None
    consumption_until: datetime | None = None
    solar_from: datetime | None = None
    solar_until: datetime | None = None
    prices_from: datetime | None = None
    prices_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class DryRunResult:
    """The outcome of one Dry-Run plan build: a status, an optional plan, and the input windows.

    ``horizon`` is the span the build actually chose: the common honest contiguous coverage of
    prices and solar from the planner's start boundary, capped at :data:`MAX_PLANNING_HORIZON`.
    Consumption does not trim it: its history fallback or configured first-run baseline fills
    slots without representative history. It is reported so the preview can state the real horizon
    instead of a fixed figure. When the build fails closed it is the span that was evaluated
    (0 for the per-input none-at-all preconditions and the battery precondition).
    """

    status: PlanStatus
    plan: "object | None"  # EnergyPlan, kept loose so this module needs no plan type at the boundary
    window: ForecastWindow
    detail: str | None = None
    horizon: timedelta = timedelta(0)
    forecast_basis: ForecastBasis | None = None
    consumption_history_available: bool | None = None
    forecast_confidence: ForecastConfidence | None = None


class BatteryCapacityUnavailable(Exception):
    """The usable battery capacity precondition is unmet; the plan is battery_state_unavailable."""


# ----------------------------------------------------------------------------------------------
# Battery state
# ----------------------------------------------------------------------------------------------


def _usable_live_limit(value: float | None) -> float | None:
    """A live limit only when it is a finite positive number; anything else counts as absent."""
    if value is None or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


class EnergyReadingsBatteryStateProvider:
    """Builds a :class:`BatteryState` from already-safe local data: cache SoC and dispatch limits.

    Usable capacity is NOT guessed: it comes only from the resolved battery facts (expert override
    or an evidence-backed source). When it is absent, or
    when SoC/power limits are missing or stale, the provider raises
    :class:`BatteryCapacityUnavailable` / returns ``None`` and the Dry-Run plan fails closed to
    ``battery_state_unavailable``. It never reads the inverter and never writes.
    """

    def __init__(
        self,
        readings_port,
        *,
        device_id: str,
        usable_capacity_wh: float | None,
        charge_power_w: float | None,
        discharge_power_w: float | None,
        clock: Clock,
        live_charge_limit_w: float | None = None,
    ) -> None:
        self._readings = readings_port
        self._device_id = device_id
        self._capacity_wh = usable_capacity_wh
        self._charge_power_w = charge_power_w
        self._discharge_power_w = discharge_power_w
        self._clock = clock
        # A verified, fresh live charge limit, or None. None never means unlimited: the static
        # limit above stays authoritative, and a usable live limit only lowers the permitted power.
        self._live_charge_limit_w = _usable_live_limit(live_charge_limit_w)

    def current(self) -> BatteryState:
        if self._capacity_wh is None or not math.isfinite(self._capacity_wh) or self._capacity_wh <= 0:
            raise BatteryCapacityUnavailable("usable battery capacity is not configured")
        if (
            self._charge_power_w is None
            or self._discharge_power_w is None
            or not math.isfinite(self._charge_power_w)
            or not math.isfinite(self._discharge_power_w)
            or self._charge_power_w <= 0
            or self._discharge_power_w <= 0
        ):
            # Validate finiteness in the provider so NaN/Inf fails closed to the contract's
            # BatteryCapacityUnavailable instead of leaking a raw ValueError from BatteryState.
            raise BatteryCapacityUnavailable("battery power limits are not configured")
        readings = self._readings.readings(self._device_id)
        soc = readings.battery_soc_percent
        if soc.value is None or soc.stale or not math.isfinite(soc.value) or not 0.0 <= soc.value <= 100.0:
            raise BatteryCapacityUnavailable("battery SoC is unavailable or stale")
        return BatteryState(
            soc_percent=float(soc.value),
            usable_capacity_wh=float(self._capacity_wh),
            max_charge_power_w=float(self._charge_power_w),
            max_discharge_power_w=float(self._discharge_power_w),
            measured_at=self._clock.now(),
            live_charge_limit_w=self._live_charge_limit_w,
        )


# ----------------------------------------------------------------------------------------------
# Consumption forecast
# ----------------------------------------------------------------------------------------------


class StoredConsumptionForecastProvider:
    """A concrete ConsumptionForecastProvider over the stored 15-minute consumption history.

    Reads the device's buckets for the lookback window from the admin store and defers to the
    deterministic :class:`AdaptiveConsumptionForecaster` (which answers with the median forecast
    while the history is too short for an everyday profile). Raises nothing on an empty history; the
    caller decides whether the available data clears :data:`MIN_HISTORY_BUCKETS`.
    """

    def __init__(self, store, *, device_id: str, timezone: str = "UTC", clock: Clock | None = None) -> None:
        self._store = store
        self._device_id = device_id
        self._forecaster = AdaptiveConsumptionForecaster(timezone)
        self._clock = clock

    def well_covered_count(self, now: datetime) -> int:
        return sum(1 for bucket in self._load_history(now) if bucket.well_covered)

    def horizon_coverage_ratio(self, now: datetime, horizon: timedelta) -> float:
        """Share of the horizon's slots backed by representative per-slot history (not the fallback)."""
        return self._forecaster.horizon_coverage_ratio(self._load_history(now), now, horizon)

    def coverage_end(self, now: datetime, horizon: timedelta) -> datetime:
        """How far from ``now`` the horizon is contiguously backed by per-slot history (not fallback)."""
        return self._forecaster.coverage_end(self._load_history(now), now, horizon)

    def forecast(self, now: datetime, horizon: timedelta) -> ConsumptionForecast:
        history = self._load_history(now)
        return self._forecaster.build(history, now, horizon)

    def analysis(self, now: datetime) -> AdaptiveAnalysis:
        """Profile basis, consumption phase, data coverage and uncertainty behind the forecast."""
        return self._forecaster.analyze(self._load_history(now), now)

    def next_day(self, now: datetime, method: str = METHOD_ADAPTIVE) -> NextDayForecast | None:
        """The next local calendar day's quarter-hour forecast (``method`` selects adaptive or median)."""
        return self._forecaster.next_day_forecast(self._load_history(now), now, method=method)

    def evaluate_runs(self, now: datetime, first_created: datetime) -> list[tuple[dict, ForecastEvaluation | None]]:
        """Stored forecast runs created since ``first_created`` against sufficiently measured actuals.

        A run whose target day is not yet over, or whose actuals are too thin, maps to ``None``.
        """
        runs = self._store.get_consumption_forecast_runs(
            self._device_id, first_created.astimezone(UTC).isoformat(), now.astimezone(UTC).isoformat()
        )
        history = self._load_history(now)
        return [(run, evaluate_forecast(run["values"], history)) for run in runs]

    def _load_history(self, now: datetime):
        first = (now - HISTORY_LOOKBACK).astimezone(UTC).isoformat()
        last = now.astimezone(UTC).isoformat()
        rows = self._store.get_consumption_intervals(self._device_id, first, last)
        meter = self._store.get_consumption_meter_intervals(self._device_id, first, last)
        return rows_to_history(rows, meter)


# ----------------------------------------------------------------------------------------------
# Spot prices and solar forecast: convert already-persisted rows into optimizer inputs
# ----------------------------------------------------------------------------------------------


def spot_snapshot_from_rows(config: SpotMarketConfig, rows: Sequence[dict]) -> SpotPriceSnapshot | None:
    """Build a :class:`SpotPriceSnapshot` from stored spot rows; ``None`` when none are stored.

    This never calls the provider: the plan preview must not trigger an Energy-Charts request.
    """
    from decimal import Decimal

    if not rows:
        return None
    intervals = tuple(
        sorted(
            (
                SpotPriceInterval(
                    start=datetime.fromisoformat(row["interval_start"]),
                    end=datetime.fromisoformat(row["interval_end"]),
                    price_eur_per_kwh=Decimal(str(row["price_eur_per_kwh"])),
                )
                for row in rows
            ),
            key=lambda item: item.start,
        )
    )
    return SpotPriceSnapshot(
        provider=config.provider,
        bidding_zone=config.bidding_zone,
        currency="EUR",
        fetched_at=max(datetime.fromisoformat(row["fetched_at"]) for row in rows),
        intervals=intervals,
        timezone=rows[0]["timezone"],
    )


def solar_forecast_from_rows(rows: Sequence[dict]) -> SolarForecast | None:
    """Build a :class:`SolarForecast` from stored solar rows; ``None`` when none are stored.

    Never calls Open-Meteo: planning data comes from SQLite only.
    """
    if not rows:
        return None
    return SolarForecast(
        created_at=max(datetime.fromisoformat(row["fetched_at"]) for row in rows),
        intervals=tuple(
            sorted(
                (
                    SolarForecastInterval(
                        start=datetime.fromisoformat(row["interval_start"]),
                        end=datetime.fromisoformat(row["interval_end"]),
                        energy_wh=float(row["energy_wh"]),
                    )
                    for row in rows
                ),
                key=lambda item: item.start,
            )
        ),
    )


# ----------------------------------------------------------------------------------------------
# Horizon-clipping and the Dry-Run plan builder
# ----------------------------------------------------------------------------------------------


# Fail-closed horizon coverage: the planner silently reads a missing price/PV interval as a known
# zero, so before planning we require the inputs to actually span the horizon. The tolerance only
# absorbs sub-second timestamp rounding at an interval edge; it is deliberately far below one
# quarter-hour slot so a single missing 15-minute interval (or a tail that ends one slot short) can
# never be swallowed and read as zero. Fail-closed, never zero-filled.
_COVERAGE_TOLERANCE = timedelta(seconds=1)

# Fail-closed planning horizon (review finding 4). Instead of demanding a fixed span and rejecting
# the whole plan when it is not fully backed, trim the horizon to the span the inputs honestly
# cover in common, capped at MAX_PLANNING_HORIZON, and only refuse when that common span is shorter
# than MIN_PLANNING_HORIZON. This never zero-fills: slots beyond the common coverage are not planned.
MIN_PLANNING_HORIZON = timedelta(hours=6)
MAX_PLANNING_HORIZON = timedelta(hours=36)


def _covers_horizon(intervals: Sequence, now: datetime, horizon: timedelta) -> bool:
    """True when the ordered intervals contiguously cover ``[now, now + horizon)`` within tolerance.

    Intervals that start after ``now + horizon`` or end at/before ``now`` are irrelevant. Among the
    rest the earliest start must be at/before ``now`` and the covered run must reach ``now + horizon``
    (minus one quarter-hour tolerance) with no internal gap wider than the tolerance. A gap or a
    short tail means the horizon is only partially backed, which is reported as unavailable rather
    than filled with zero.
    """
    required_end = now + horizon
    relevant = sorted(
        (iv for iv in intervals if iv.end > now and iv.start < required_end),
        key=lambda iv: iv.start,
    )
    # The leading edge gets the SAME one-second tolerance as the internal gaps and the horizon end
    # (review finding 4): a first interval that starts a sub-second after the anchor is a timestamp
    # rounding artefact, not a missing quarter-hour, so it must not reject the whole horizon.
    if not relevant or relevant[0].start > now + _COVERAGE_TOLERANCE:
        return False
    reached = relevant[0].end
    for iv in relevant[1:]:
        if iv.start > reached + _COVERAGE_TOLERANCE:
            return False
        reached = max(reached, iv.end)
    return reached >= required_end - _COVERAGE_TOLERANCE


def _coverage_end(intervals: Sequence, now: datetime) -> datetime:
    """End of the contiguous run of intervals that starts at/before ``now``.

    Returns how far ``[now, ...)`` is honestly backed: the earliest relevant interval must start at
    or before ``now`` (else nothing is covered and ``now`` is returned), and the run ends at the
    first gap wider than :data:`_COVERAGE_TOLERANCE`. The same one-second tolerance as
    :func:`_covers_horizon` is used, so a single missing quarter-hour ends the run there rather than
    being bridged. The returned instant feeds the common-horizon trim; it never licenses zero-fill.
    """
    relevant = sorted(
        (iv for iv in intervals if iv.end > now),
        key=lambda iv: iv.start,
    )
    # Same leading-edge tolerance as _covers_horizon (review finding 4): a sub-second-late first
    # interval is a rounding artefact, not an uncovered leading slot, so coverage still starts here.
    if not relevant or relevant[0].start > now + _COVERAGE_TOLERANCE:
        return now
    reached = relevant[0].end
    for iv in relevant[1:]:
        if iv.start > reached + _COVERAGE_TOLERANCE:
            break
        reached = max(reached, iv.end)
    return reached


def _clip_to_horizon(forecast, now: datetime, horizon: timedelta):
    """Window a forecast's intervals to ``[now_bucket, now+horizon)`` so inputs stay bounded.

    Keeps every interval that overlaps the window; the pure optimizer tolerates variable-length and
    partial intervals. Returns a value of the same forecast type.
    """
    horizon_end = now + horizon
    kept = tuple(iv for iv in forecast.intervals if iv.end > now and iv.start < horizon_end)
    return type(forecast)(created_at=forecast.created_at, intervals=kept)


@dataclass(frozen=True, slots=True)
class DryRunInputs:
    """The local data a Dry-Run plan is built from, each already loaded from SQLite/cache."""

    prices: SpotPriceSnapshot | None
    solar: SolarForecast | None
    spot_enabled: bool
    solar_configurable: bool


@dataclass(frozen=True, slots=True)
class ConsumptionResolution:
    """The consumption forecast plus how it was obtained; shared by Dry-Run and the runtime planner."""

    forecast: ConsumptionForecast
    basis: ForecastBasis
    confidence: ForecastConfidence
    description: str
    well_covered: int


def resolve_consumption_forecast(
    consumption_provider: StoredConsumptionForecastProvider,
    *,
    now: datetime,
    start: datetime,
    plannable_span: timedelta,
    planner_horizon: timedelta,
    baseline_household_load_w: float,
) -> ConsumptionResolution:
    """The single consumption policy: no history -> flat baseline, thin history -> median fallback.

    Both paths are LOW confidence; only sufficient, well-covered history yields adaptive/NORMAL.
    """
    well_covered = consumption_provider.well_covered_count(now)
    if well_covered == 0:
        return ConsumptionResolution(
            BaselineConsumptionForecastProvider(baseline_household_load_w).forecast(now, planner_horizon),
            ForecastBasis.BOOTSTRAP_BASELINE,
            ForecastConfidence.LOW,
            f"consumption: configured flat {baseline_household_load_w:g} W bootstrap baseline "
            "(history unavailable, low confidence); solar: stored forecast; prices: stored spot prices",
            well_covered,
        )
    coverage = consumption_provider.horizon_coverage_ratio(start, plannable_span)
    consumption = consumption_provider.forecast(now, planner_horizon)
    analysis = getattr(consumption_provider, "analysis", None)
    learning = analysis(now).method != METHOD_ADAPTIVE if analysis is not None else False
    if well_covered < MIN_HISTORY_BUCKETS or coverage < MIN_HORIZON_COVERAGE_RATIO or learning:
        return ConsumptionResolution(
            consumption,
            ForecastBasis.HISTORY_FALLBACK,
            ForecastConfidence.LOW,
            f"consumption: stored-history median fallback ({coverage:.0%} of horizon backed, "
            "low confidence); solar: stored forecast; prices: stored spot prices",
            well_covered,
        )
    return ConsumptionResolution(
        consumption, ForecastBasis.ADAPTIVE_HISTORY, ForecastConfidence.NORMAL, FORECAST_BASIS, well_covered
    )


def build_dry_run_plan(
    *,
    now: datetime,
    battery_provider,
    consumption_provider: StoredConsumptionForecastProvider,
    inputs: DryRunInputs,
    baseline_household_load_w: float,
    horizon: timedelta = MAX_PLANNING_HORIZON,
    parameters: PlanParameters | None = None,
    optimizer: DynamicChargeOptimizer | None = None,
) -> DryRunResult:
    """Assemble a :class:`PlanningInput` from local data and run the pure optimizer, or report why not.

    Preconditions are checked in a fixed order and each maps to an explicit status. No provider I/O
    happens here; a missing or stale hard input is reported, never backfilled with zero. The battery
    precondition (including the operator-set usable capacity) comes first because it is the hardest
    requirement and the one most likely to be deliberately unconfigured in this first version.

    The planning horizon is not fixed: it is trimmed to the span prices and solar contiguously cover
    in common from the planner's start boundary (review finding 4), capped at
    :data:`MAX_PLANNING_HORIZON` and at the caller's ``horizon`` request. Consumption does not trim
    the horizon: the forecaster fills slots its per-slot history does not back with a conservative
    whole-history median, or the configured baseline when history is unavailable. Coverage is
    anchored at the next full quarter-hour boundary (the shared :func:`planning_start`), so a partial current
    slot the plan never uses cannot shorten or block it. When the common span is shorter than
    :data:`MIN_PLANNING_HORIZON` the build fails closed with the status of the limiting input; it is
    never padded with zero slots.
    """
    optimizer = optimizer or DynamicChargeOptimizer(parameters)
    window = ForecastWindow()

    try:
        battery = battery_provider.current()
    except BatteryCapacityUnavailable as exc:
        # The public detail is a stable, product-level sentence: the internal exception text (e.g.
        # "not configured") describes only one of several causes and must not leak into the UI. The
        # specific internal wording stays in the exception/log; keep it visible for diagnostics.
        log.debug("Dry-run battery precondition unmet: %s", exc)
        return DryRunResult(
            PlanStatus.BATTERY_STATE_UNAVAILABLE,
            None,
            window,
            "Usable battery capacity could not be verified for this inverter.",
        )

    # An input the operator switched off or never configured will not appear on its own, so it gets
    # its own status instead of the "not available yet" wording that implies waiting helps.
    if not inputs.spot_enabled:
        return DryRunResult(
            PlanStatus.SPOT_DISABLED, None, window, "The spot market integration is disabled."
        )
    if not inputs.solar_configurable:
        return DryRunResult(
            PlanStatus.SOLAR_NOT_CONFIGURED, None, window, "The solar forecast is not configured."
        )

    # The none-at-all preconditions stay first: with no input of a kind there is nothing to trim to.
    if inputs.prices is None or not inputs.prices.intervals:
        return DryRunResult(PlanStatus.SPOT_PRICES_UNAVAILABLE, None, window)
    if inputs.solar is None or not inputs.solar.intervals:
        return DryRunResult(PlanStatus.SOLAR_FORECAST_UNAVAILABLE, None, window)

    # Trim to the honest common horizon. Coverage is anchored at the instant the planner will
    # actually start from — the next full quarter-hour boundary (shared planning_start helper), not
    # ``now`` — so a partial current slot the planner never uses cannot shorten or block the plan.
    # The caller's requested horizon and the product MAX both cap the span.
    requested = min(horizon, MAX_PLANNING_HORIZON)
    start = planning_start(now)
    prices_until = _coverage_end(inputs.prices.intervals, start)
    solar_until = _coverage_end(inputs.solar.intervals, start)
    # Prices and solar are the hard per-slot inputs: the planner reads a missing one as zero, so the
    # plannable span is bounded by their common contiguous reach. Consumption is NOT trimmed to its
    # first per-slot gap here: the forecaster is documented to fill uncovered slots with a
    # conservative whole-history median. Trimming to the first consumption gap would demand
    # 100 % contiguous per-slot history and defeat that fallback.
    # Cap the common reach at the requested span measured from the planner's start boundary.
    available_until = min(prices_until, solar_until, start + requested)
    # ``plannable_span`` is the span of slots the planner can actually fill, from its start boundary
    # to the common coverage end; it drives the MIN gate and is the horizon reported to the preview.
    # The MIN comparison carries the same _COVERAGE_TOLERANCE as the internal gap check (P3-1), so a
    # sub-second shortfall at the 6 h boundary does not flip the decision while an internal gap is
    # tolerated to the same precision.
    plannable_span = available_until - start
    if plannable_span < MIN_PLANNING_HORIZON - _COVERAGE_TOLERANCE:
        min_hours = MIN_PLANNING_HORIZON.total_seconds() / 3600.0
        if prices_until <= solar_until:
            status, source = PlanStatus.SPOT_PRICES_UNAVAILABLE, "spot prices"
        else:
            status, source = PlanStatus.SOLAR_FORECAST_UNAVAILABLE, "solar forecast"
        detail = (
            f"{source} covers only "
            f"{max(0.0, plannable_span.total_seconds()) / 3600.0:.1f} h of the planning "
            f"horizon (need at least {min_hours:.0f} h)"
        )
        return DryRunResult(status, None, window, detail)

    # Belt-and-braces: the trimmed horizon must still be fully, contiguously covered by prices and
    # solar from the planner's start boundary. _coverage_end already guarantees this, but keep the
    # explicit fail-closed gate so a future change to the trim logic cannot silently let a gap
    # through to the zero-reading planner.
    if not _covers_horizon(inputs.prices.intervals, start, plannable_span):
        return DryRunResult(
            PlanStatus.SPOT_PRICES_UNAVAILABLE,
            None,
            window,
            "spot prices do not cover the required planning horizon",
            plannable_span,
        )
    if not _covers_horizon(inputs.solar.intervals, start, plannable_span):
        return DryRunResult(
            PlanStatus.SOLAR_FORECAST_UNAVAILABLE,
            None,
            window,
            "solar forecast does not cover the required planning horizon",
            plannable_span,
        )

    # The pure planner's grid is ``[planning_start(now), now + planner_horizon)``; make it reach
    # exactly ``available_until`` by expressing the span relative to ``now`` (it differs from the
    # reported plannable span only by the dropped partial-slot skew ``start - now``).
    planner_horizon = available_until - now
    resolution = resolve_consumption_forecast(
        consumption_provider,
        now=now,
        start=start,
        plannable_span=plannable_span,
        planner_horizon=planner_horizon,
        baseline_household_load_w=baseline_household_load_w,
    )
    consumption = resolution.forecast
    forecast_basis = resolution.basis
    forecast_confidence = resolution.confidence
    basis_description = resolution.description
    well_covered = resolution.well_covered
    if not consumption.intervals:
        return DryRunResult(
            PlanStatus.INSUFFICIENT_HISTORY, None, window, "no consumption slots could be estimated", plannable_span
        )

    solar = _clip_to_horizon(inputs.solar, now, planner_horizon)
    if not solar.intervals:
        return DryRunResult(
            PlanStatus.SOLAR_FORECAST_UNAVAILABLE,
            None,
            window,
            "solar forecast does not reach the horizon",
            plannable_span,
        )

    planning_input = PlanningInput(
        now=now,
        battery=battery,
        prices=inputs.prices,
        solar_forecast=solar,
        consumption_forecast=consumption,
        horizon=planner_horizon,
        forecast_basis=basis_description,
    )
    plan = optimizer.plan(planning_input)
    window = ForecastWindow(
        consumption_from=consumption.intervals[0].start,
        consumption_until=consumption.intervals[-1].end,
        solar_from=solar.intervals[0].start,
        solar_until=solar.intervals[-1].end,
        prices_from=inputs.prices.intervals[0].start,
        prices_until=inputs.prices.intervals[-1].end,
    )
    return DryRunResult(
        PlanStatus.OK,
        plan,
        window,
        horizon=plannable_span,
        forecast_basis=forecast_basis,
        consumption_history_available=well_covered > 0,
        forecast_confidence=forecast_confidence,
    )
