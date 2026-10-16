#!/usr/bin/env python3
#
# app/optimizer/adaptive.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Adaptive next-day household-consumption forecast: everyday profile plus a consumption phase.

The plain median forecast (:class:`~app.optimizer.consumption.HistoricalConsumptionForecaster`)
treats every stored day as an everyday day. This forecaster separates two things:

* the **everyday profile**: a robust median per local weekday and time of day, learned only from
  clean, closed, sufficiently measured buckets while the household behaves as usual;
* the **consumption phase**: ``normal`` or ``suspected_low_activity``, a contiguous stretch of
  clearly reduced consumption relative to that profile. It is deliberately not called "absence": the
  data cannot tell a holiday from a hospital stay or a switched-off house.

The state is never stored. It is **replayed** from the stored 15-minute history on every call, one
closed bucket after another and without looking ahead, so a restart reconstructs exactly the state
the running process had, and a forecast made at time T uses only buckets that ended before T.

Data rules: only COMPLETE buckets, and PARTIAL ones that pass :func:`usable_value`, are observations.
ESTIMATED and MISSING buckets are no observation at all: they never teach the profile and never
enter a detection window, and a measurement outage is therefore neither vacation nor zero
consumption (the windows simply hold fewer observations, and every decision demands a minimum).

The rules, thresholds and their justification are documented in ``docs/development/architecture.md``
(section "Adaptive next-day consumption forecast"); the constants below carry the same names.
"""

import json
import statistics
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.optimizer.consumption import (
    BUCKET_WIDTH,
    BucketQuality,
    HistoricalConsumptionForecaster,
    HistoryBucket,
    floor_to_bucket,
)
from app.optimizer.models import ConsumptionForecast, ConsumptionForecastInterval

METHOD_ADAPTIVE = "adaptive-v1"
METHOD_MEDIAN = "median-v1"

# --- Observations -------------------------------------------------------------------------------
# A PARTIAL bucket (measured 600..810 s) is extrapolated to the full width, so it is only trusted
# from 750 s (83 %) on: the extrapolation factor then stays below 1.2.
MIN_PARTIAL_COVERAGE_SECONDS = 750.0

# --- Everyday profile ---------------------------------------------------------------------------
# A bucket is learned this long after it ended, and only while the state is normal. The delay equals
# the entry window, so when a low phase is detected nothing of its onset has been learned yet.
LEARN_LAG = timedelta(hours=24)
# Newest normal samples kept per same-weekday slot (eight weeks) and per time-of-day slot (three
# weeks); a median needs at least PROFILE_MIN_SAMPLES of them. Counting samples, not calendar days,
# keeps the profile intact across a long phase.
PROFILE_WEEK_SAMPLES = 8
PROFILE_DAY_SAMPLES = 21
PROFILE_MIN_SAMPLES = 3
PROFILE_CAP = 1.5  # a sample counts at most this multiple of the slot's median
# The profile is ready (detection starts) after this many local days with at least
# PROFILE_DAY_MIN_BUCKETS learned buckets each (about 70 % of 96).
PROFILE_READY_DAYS = 14
PROFILE_DAY_MIN_BUCKETS = 64

# --- Entering suspected low activity ------------------------------------------------------------
ENTER_WINDOW = timedelta(hours=24)
ENTER_MIN_BUCKETS = 68  # 70 % of the 96 buckets in the window must be observed
# Observed over expected energy of the window, each bucket capped at RATIO_CAP times its expected
# energy: a single consumer (oven, wallbox) can then add at most one extra bucket of expected energy
# per bucket it runs, and an everyday window stays near 1.0 with the capped-mean profile below.
ENTER_RATIO = 0.60
RATIO_CAP = 2.0
# Buckets whose expected energy is below this are too small for a meaningful ratio.
MIN_EXPECTED_WH = 3.0

# --- Return detection ---------------------------------------------------------------------------
# The verdict looks at the newest informative buckets, not at a fixed time span: a span would stay
# diluted by the hours before the return and delay it by half a day. Evidence older than
# RETURN_MAX_AGE is dropped, so a measurement outage cannot join two unrelated evenings.
RETURN_EVIDENCE_BUCKETS = 16
RETURN_MAX_AGE = timedelta(hours=8)
# A bucket is informative when the profile expects clearly more than the low level (two times and at
# least this much more); only those can tell "returned" from "still away".
INFORMATIVE_FACTOR = 2.0
INFORMATIVE_MIN_SEPARATION_WH = 15.0
RETURN_MIN_FRACTION = 0.75  # share of them closer to the profile than to the low level (12 of 16)
RETURN_CONFIRM_STEPS = 2  # consecutive informative buckets after which the verdict must hold
# A household that is back also reproduces the profile's peaks (morning, evening), not only its
# quiet hours: at least RETURN_MIN_PEAK_BUCKETS of the evidence must be buckets where the profile
# expects PEAK_FACTOR times the low level and the measurement reaches PEAK_POSITION of the way from
# the low level to the profile. A few hours of a moderate load (TV, a PC) in an empty house
# is not enough.
PEAK_FACTOR = 4.0
PEAK_POSITION = 0.7
RETURN_MIN_PEAK_BUCKETS = 4
# A bucket above this multiple of the profile is one big consumer (oven, wallbox, heater), not a
# household going about its day: it is neither evidence for nor against a return.
RETURN_MAX_EXCESS = 3.0
LOW_BASE_MIN_BUCKETS = 48  # twelve hours: the standby level is re-estimated once the phase has this many
LOW_BASE_REFRESH_BUCKETS = 16

# --- Low-phase forecast and promotion -----------------------------------------------------------
LOW_LEARN_WINDOW = timedelta(days=7)
LOW_SLOT_MIN_SAMPLES = 3
# A phase this long is the new normal: the profile is rebuilt from it instead of staying frozen.
PROMOTE_AFTER = timedelta(days=28)

# --- Permanent level changes (normal state) -----------------------------------------------------
LEVEL_DAYS = 5  # the median of the last five daily ratios; three days are needed
LEVEL_MIN_DAYS = 3
LEVEL_DEADBAND = 0.10
LEVEL_MIN = 0.2
LEVEL_MAX = 5.0
DAY_MIN_BUCKETS = 64

# --- Next-day recording and comparison ----------------------------------------------------------
ACTUAL_MIN_SHARE = 0.90  # a day is a valid actual when this share of its slots is COMPLETE

_SPREAD_DAYS = 14
_COVERAGE_BUCKETS = 7 * 96


class ConsumptionPhase(StrEnum):
    INSUFFICIENT_HISTORY = "insufficient_history"
    NORMAL = "normal"
    SUSPECTED_LOW_ACTIVITY = "suspected_low_activity"


def usable_value(bucket: HistoryBucket) -> float | None:
    """The bucket's full-width energy when it is a clean observation, else ``None``.

    COMPLETE buckets count as measured; PARTIAL ones only from MIN_PARTIAL_COVERAGE_SECONDS on and
    scaled to the full width. ESTIMATED, MISSING and thinly covered buckets are not observations.
    """
    quality = bucket.quality
    if quality is BucketQuality.COMPLETE:
        return bucket.energy_wh
    if quality is BucketQuality.PARTIAL and bucket.coverage_seconds >= MIN_PARTIAL_COVERAGE_SECONDS:
        return bucket.normalized_wh
    return None


@dataclass(frozen=True, slots=True)
class PhaseEvent:
    """One traceable state change of the replay."""

    at: datetime
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class AdaptiveAnalysis:
    """What the forecast is based on, for display, logs and the stored explanation."""

    phase: ConsumptionPhase
    method: str
    profile_basis: str
    profile_days: int
    level_factor: float
    phase_detected_at: datetime | None
    last_return_at: datetime | None
    promoted_at: datetime | None
    data_coverage: float
    daily_spread: float | None
    return_evidence: float | None
    reasons: tuple[str, ...]
    events: tuple[PhaseEvent, ...]

    def to_dict(self) -> dict:
        def iso(moment: datetime | None) -> str | None:
            return moment.astimezone(UTC).isoformat() if moment else None

        return {
            "phase": self.phase.value,
            "method": self.method,
            "profile_basis": self.profile_basis,
            "profile_days": self.profile_days,
            "level_factor": round(self.level_factor, 3),
            "phase_detected_at": iso(self.phase_detected_at),
            "last_return_at": iso(self.last_return_at),
            "promoted_at": iso(self.promoted_at),
            "data_coverage": round(self.data_coverage, 3),
            "daily_spread": None if self.daily_spread is None else round(self.daily_spread, 3),
            "return_evidence": None if self.return_evidence is None else round(self.return_evidence, 3),
            "reasons": list(self.reasons),
            "events": [{"at": iso(e.at), "kind": e.kind, "detail": e.detail} for e in self.events],
        }

    def explanation(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class NextDayForecast:
    """The quarter-hour forecast of the next local calendar day, with its analysis."""

    target_date: str
    values: tuple[tuple[str, float], ...]
    analysis: AdaptiveAnalysis


@dataclass(frozen=True, slots=True)
class ForecastEvaluation:
    """A stored forecast compared with sufficiently measured actuals of the same slots."""

    slots: int
    forecast_wh: float
    actual_wh: float
    daily_error_pct: float
    mae_wh: float
    rmse_wh: float


def evaluate_forecast(
    values: Sequence[tuple[str, float]], history: Sequence[HistoryBucket]
) -> ForecastEvaluation | None:
    """Compare stored forecast values with measured actuals; ``None`` when the actuals are too thin.

    Only COMPLETE buckets are actuals, and at least ACTUAL_MIN_SHARE of the forecast slots must have
    one; the comparison then uses exactly the matched slots on both sides.
    """
    actual = {b.start: b.energy_wh for b in history if b.quality is BucketQuality.COMPLETE}
    pairs = []
    for start, energy in values:
        moment = datetime.fromisoformat(start).astimezone(UTC)
        if moment in actual:
            pairs.append((energy, actual[moment]))
    if not values or len(pairs) < ACTUAL_MIN_SHARE * len(values):
        return None
    forecast_wh = sum(f for f, _ in pairs)
    actual_wh = sum(a for _, a in pairs)
    errors = [f - a for f, a in pairs]
    return ForecastEvaluation(
        slots=len(pairs),
        forecast_wh=forecast_wh,
        actual_wh=actual_wh,
        daily_error_pct=100.0 * (forecast_wh - actual_wh) / actual_wh if actual_wh > 0 else 0.0,
        mae_wh=sum(abs(e) for e in errors) / len(errors),
        rmse_wh=(sum(e * e for e in errors) / len(errors)) ** 0.5,
    )


class _Replay:
    """One chronological pass over the closed buckets; holds the final state and the profile."""

    def __init__(self, zone: ZoneInfo, observed: dict[datetime, float]) -> None:
        self.zone = zone
        self.observed = observed
        self.state = "learning"  # learning | normal | low
        self.level = 1.0
        self.week: dict[tuple[int, int, int], deque[float]] = {}
        self.slot: dict[tuple[int, int], deque[float]] = {}
        self.learned_per_date: Counter = Counter()
        self.quarantine_until: datetime | None = None
        self.events: list[PhaseEvent] = []
        self.win: deque[tuple[datetime, float, float]] = deque()
        self.win_actual = self.win_expected = 0.0
        self.detected_at: datetime | None = None
        self.last_return_at: datetime | None = None
        self.promoted_at: datetime | None = None
        self.low: deque[tuple[datetime, float, float | None]] = deque()
        self.low_base: float | None = None
        self.evidence: deque[tuple[datetime, bool, bool]] = deque(maxlen=RETURN_EVIDENCE_BUCKETS)
        self.confirm = 0
        self.last_fraction: float | None = None
        self.day = None
        self.day_actual = self.day_expected = 0.0
        self.day_buckets = 0
        self.level_ratios: deque[float] = deque(maxlen=LEVEL_DAYS)
        self.recent_ratios: deque[float] = deque(maxlen=_SPREAD_DAYS)
        self.end: datetime | None = None

    # -- profile ------------------------------------------------------------------------------
    def keys(self, moment: datetime) -> tuple[tuple[int, int, int], tuple[int, int]]:
        local = moment.astimezone(self.zone)
        return (local.weekday(), local.hour, local.minute), (local.hour, local.minute)

    def base_profile(self, moment: datetime) -> float | None:
        week_key, slot_key = self.keys(moment)
        samples = self.week.get(week_key)
        if samples is None or len(samples) < PROFILE_MIN_SAMPLES:
            samples = self.slot.get(slot_key)
            if samples is None or len(samples) < PROFILE_MIN_SAMPLES:
                return None
        # Capped mean: a lone spike counts at most twice the median, yet everyday random loads still
        # contribute their expected share (a plain median would systematically under-forecast energy).
        cap = PROFILE_CAP * statistics.median(samples)
        return sum(min(sample, cap) for sample in samples) / len(samples)

    def expected(self, moment: datetime) -> float | None:
        base = self.base_profile(moment)
        return None if base is None else base * self.level

    def _learn(self, moment: datetime) -> None:
        value = self.observed.get(moment)
        if value is None or (self.quarantine_until is not None and moment < self.quarantine_until):
            return
        week_key, slot_key = self.keys(moment)
        normalized = value / self.level
        self.week.setdefault(week_key, deque(maxlen=PROFILE_WEEK_SAMPLES)).append(normalized)
        self.slot.setdefault(slot_key, deque(maxlen=PROFILE_DAY_SAMPLES)).append(normalized)
        self.learned_per_date[moment.astimezone(self.zone).date()] += 1

    def profile_days(self) -> int:
        return sum(1 for n in self.learned_per_date.values() if n >= PROFILE_DAY_MIN_BUCKETS)

    # -- replay -------------------------------------------------------------------------------
    def run(self, now: datetime) -> "_Replay":
        self.end = floor_to_bucket(now)
        if not self.observed:
            return self
        moment = min(self.observed)
        while moment < self.end:
            self._step(moment)
            moment += BUCKET_WIDTH
        return self

    def _step(self, moment: datetime) -> None:
        date = moment.astimezone(self.zone).date()
        if self.day != date:
            self._close_day()
            self.day = date
        if self.state != "low":
            self._learn(moment - LEARN_LAG)
        if self.state == "learning":
            if self.profile_days() >= PROFILE_READY_DAYS:
                self.state = "normal"
                self._event(moment, "profile_ready", f"{self.profile_days()} learned days")
            else:
                return
        value = self.observed.get(moment)
        expected = self.expected(moment)
        cutoff = moment - ENTER_WINDOW
        while self.win and self.win[0][0] <= cutoff:
            _, old_actual, old_expected = self.win.popleft()
            self.win_actual -= old_actual
            self.win_expected -= old_expected
        if value is not None and expected is not None and expected >= MIN_EXPECTED_WH:
            capped = min(value, RATIO_CAP * expected)
            self.win.append((moment, capped, expected))
            self.win_actual += capped
            self.win_expected += expected
            self.day_actual += capped
            self.day_expected += expected / self.level
            self.day_buckets += 1
        if self.state == "normal":
            self._check_enter(moment)
        else:
            self._low_step(moment, value, expected)

    def _event(self, at: datetime, kind: str, detail: str) -> None:
        self.events.append(PhaseEvent(at + BUCKET_WIDTH, kind, detail))

    def _check_enter(self, moment: datetime) -> None:
        if len(self.win) < ENTER_MIN_BUCKETS or self.win_expected <= 0.0:
            return
        ratio = self.win_actual / self.win_expected
        if ratio >= ENTER_RATIO:
            return
        self.state = "low"
        self.detected_at = moment + BUCKET_WIDTH
        self.low.clear()
        # The newest twelve hours of the window are inside the phase (the ratio fell below the
        # threshold only because of them); their median is the first estimate of the standby level.
        self.low_base = statistics.median(actual for _, actual, _ in list(self.win)[-LOW_BASE_MIN_BUCKETS:])
        self.evidence.clear()
        self.confirm = 0
        self.last_fraction = None
        self._reset_day()
        self.level_ratios.clear()
        self._event(moment, "low_activity_detected", f"24h consumption {ratio:.0%} of the everyday profile")

    def _low_step(self, moment: datetime, value: float | None, expected: float | None) -> None:
        window_start = moment - RETURN_MAX_AGE
        while self.evidence and self.evidence[0][0] <= window_start:
            self.evidence.popleft()
        if value is None:
            return
        self.low.append((moment, value, expected))
        if len(self.low) % LOW_BASE_REFRESH_BUCKETS == 0 and len(self.low) >= LOW_BASE_MIN_BUCKETS:
            recent = [v for t, v, _ in self.low if t > moment - LOW_LEARN_WINDOW]
            self.low_base = statistics.median(recent)
        if self.detected_at is not None and moment + BUCKET_WIDTH - self.detected_at >= PROMOTE_AFTER:
            self._promote(moment)
            return
        base = self.low_base
        if base is None or expected is None:
            return
        if expected < INFORMATIVE_FACTOR * base or expected - base < INFORMATIVE_MIN_SEPARATION_WH:
            return
        if value > RETURN_MAX_EXCESS * expected:
            return
        like = value >= base + 0.5 * (expected - base)
        peak = like and expected >= PEAK_FACTOR * base and value >= base + PEAK_POSITION * (expected - base)
        self.evidence.append((moment, like, peak))
        fraction = sum(1 for _, liked, _ in self.evidence if liked) / len(self.evidence)
        peaks = sum(1 for _, _, peaked in self.evidence if peaked)
        self.last_fraction = fraction
        if (
            len(self.evidence) == RETURN_EVIDENCE_BUCKETS
            and fraction >= RETURN_MIN_FRACTION
            and peaks >= RETURN_MIN_PEAK_BUCKETS
        ):
            self.confirm += 1
        else:
            self.confirm = 0
        if self.confirm >= RETURN_CONFIRM_STEPS:
            self._return(moment, fraction)

    def _return(self, moment: datetime, fraction: float) -> None:
        self.state = "normal"
        self.last_return_at = moment + BUCKET_WIDTH
        self.quarantine_until = moment + BUCKET_WIDTH  # nothing of the phase or its edge is learned
        self.win.clear()
        self.win_actual = self.win_expected = 0.0
        self._reset_day()
        self.level_ratios.clear()
        self._event(
            moment, "return_confirmed",
            f"{fraction:.0%} of {len(self.evidence)} newest informative buckets match the everyday profile",
        )
        self.detected_at = None
        self.low.clear()
        self.evidence.clear()

    def _promote(self, moment: datetime) -> None:
        """A phase of PROMOTE_AFTER becomes the new normal: the profile is rebuilt from it."""
        self.week.clear()
        self.slot.clear()
        self.learned_per_date.clear()
        self.level = 1.0
        for start, value, _ in self.low:
            week_key, slot_key = self.keys(start)
            self.week.setdefault(week_key, deque(maxlen=PROFILE_WEEK_SAMPLES)).append(value)
            self.slot.setdefault(slot_key, deque(maxlen=PROFILE_DAY_SAMPLES)).append(value)
            self.learned_per_date[start.astimezone(self.zone).date()] += 1
        self.state = "normal"
        self.promoted_at = moment + BUCKET_WIDTH
        self.quarantine_until = moment + BUCKET_WIDTH
        self.win.clear()
        self.win_actual = self.win_expected = 0.0
        self._reset_day()
        self.level_ratios.clear()
        self.detected_at = None
        self.low.clear()
        self.evidence.clear()
        self._event(moment, "promoted_to_normal", f"low activity lasted {PROMOTE_AFTER.days} days; profile rebuilt")

    # -- level --------------------------------------------------------------------------------
    def _reset_day(self) -> None:
        self.day_actual = self.day_expected = 0.0
        self.day_buckets = 0

    def _close_day(self) -> None:
        if self.state == "normal" and self.day_buckets >= DAY_MIN_BUCKETS and self.day_expected > 0.0:
            ratio = self.day_actual / self.day_expected
            self.level_ratios.append(ratio)
            self.recent_ratios.append(ratio)
            if len(self.level_ratios) >= LEVEL_MIN_DAYS:
                median = statistics.median(self.level_ratios)
                if abs(median / self.level - 1.0) > LEVEL_DEADBAND:
                    new_level = min(LEVEL_MAX, max(LEVEL_MIN, median))
                    self.events.append(
                        PhaseEvent(
                            datetime.combine(self.day, datetime.min.time(), tzinfo=self.zone).astimezone(UTC)
                            + timedelta(days=1),
                            "level_changed",
                            f"level factor {self.level:.2f} -> {new_level:.2f} (median of {len(self.level_ratios)} daily ratios)",
                        )
                    )
                    self.level = new_level
        self._reset_day()

    # -- low-phase estimate -------------------------------------------------------------------
    def low_estimate(self, moment: datetime, cache: dict) -> float | None:
        if "slots" not in cache:
            cutoff = (self.end or moment) - LOW_LEARN_WINDOW
            slots: dict[tuple[int, int], list[float]] = {}
            for start, value, _ in self.low:
                if start >= cutoff:
                    slots.setdefault(self.keys(start)[1], []).append(value)
            cache["slots"] = slots
        samples = cache["slots"].get(self.keys(moment)[1], [])
        if len(samples) >= LOW_SLOT_MIN_SAMPLES:
            return statistics.median(samples)
        return self.low_base


class AdaptiveConsumptionForecaster:
    """Next-day consumption forecast from an everyday profile and the current consumption phase.

    Offers the same ``build`` / ``horizon_coverage_ratio`` / ``coverage_end`` interface as the median
    forecaster. While the history is too short for a profile it delegates to the median forecaster and
    says so in :meth:`analyze`.
    """

    def __init__(self, timezone: str = "UTC") -> None:
        self._zone = ZoneInfo(timezone)
        self._median = HistoricalConsumptionForecaster(timezone)
        self._cache_key: tuple | None = None
        self._cache: _Replay | None = None

    # -- replay -------------------------------------------------------------------------------
    def _replay(self, history: Sequence[HistoryBucket], now: datetime) -> _Replay:
        now = now.astimezone(UTC)
        end = floor_to_bucket(now)
        observed: dict[datetime, float] = {}
        for bucket in history:
            if bucket.end > now:
                continue
            value = usable_value(bucket)
            if value is not None:
                observed[bucket.start] = value
        key = (end, len(observed), min(observed, default=None), round(sum(observed.values()), 3))
        if self._cache is not None and key == self._cache_key:
            return self._cache
        replay = _Replay(self._zone, observed).run(now)
        self._cache_key, self._cache = key, replay
        return replay

    def analyze(self, history: Sequence[HistoryBucket], now: datetime) -> AdaptiveAnalysis:
        replay = self._replay(history, now)
        end = replay.end or floor_to_bucket(now)
        recent = sum(1 for moment in replay.observed if end - timedelta(days=7) <= moment < end)
        coverage = min(1.0, recent / _COVERAGE_BUCKETS)
        ratios = list(replay.recent_ratios)
        spread = statistics.median(abs(r - statistics.median(ratios)) for r in ratios) if len(ratios) >= 3 else None
        days = replay.profile_days()
        reasons: list[str] = []
        if replay.state == "learning":
            phase = ConsumptionPhase.INSUFFICIENT_HISTORY
            method = "median-fallback"
            reasons.append(
                f"only {days} of {PROFILE_READY_DAYS} required days with enough measured buckets; "
                "the median forecast is used"
            )
        elif replay.state == "low":
            phase = ConsumptionPhase.SUSPECTED_LOW_ACTIVITY
            method = METHOD_ADAPTIVE
            reasons.append("consumption is clearly below the everyday profile; the profile is frozen")
        else:
            phase = ConsumptionPhase.NORMAL
            method = METHOD_ADAPTIVE
        if coverage < 0.7:
            reasons.append(f"only {coverage:.0%} of the last seven days were measured")
        return AdaptiveAnalysis(
            phase=phase,
            method=method,
            profile_basis=(
                f"median of the newest {PROFILE_WEEK_SAMPLES} normal samples per weekday and quarter hour "
                f"(time-of-day fallback), {days} learned days, level factor {replay.level:.2f}"
            ),
            profile_days=days,
            level_factor=replay.level,
            phase_detected_at=replay.detected_at,
            last_return_at=replay.last_return_at,
            promoted_at=replay.promoted_at,
            data_coverage=coverage,
            daily_spread=spread,
            return_evidence=replay.last_fraction if replay.state == "low" else None,
            reasons=tuple(reasons),
            events=tuple(replay.events[-10:]),
        )

    # -- forecast -----------------------------------------------------------------------------
    def _slot_value(self, replay: _Replay, start: datetime, cache: dict) -> float | None:
        if replay.state == "low":
            return replay.low_estimate(start, cache)
        return replay.expected(start)

    def build(self, history: Sequence[HistoryBucket], now: datetime, horizon: timedelta) -> ConsumptionForecast:
        now = now.astimezone(UTC)
        replay = self._replay(history, now)
        median = self._median.build(history, now, horizon)
        if replay.state == "learning":
            return median
        fallback = {interval.start: interval.energy_wh for interval in median.intervals}
        cache: dict = {}
        intervals: list[ConsumptionForecastInterval] = []
        start = floor_to_bucket(now)
        horizon_end = now + horizon
        while start < horizon_end:
            value = self._slot_value(replay, start, cache)
            if value is None:
                value = fallback.get(start)
            if value is not None:
                intervals.append(ConsumptionForecastInterval(start, start + BUCKET_WIDTH, max(0.0, value)))
            start += BUCKET_WIDTH
        return ConsumptionForecast(created_at=now, intervals=tuple(intervals))

    def _backed(self, history: Sequence[HistoryBucket], now: datetime, horizon: timedelta) -> list[bool] | None:
        """Per horizon slot: backed by the profile. ``None`` while the median forecaster must answer."""
        replay = self._replay(history, now)
        if replay.state == "learning":
            return None
        now = now.astimezone(UTC)
        cache: dict = {}
        flags = []
        start = floor_to_bucket(now)
        while start < now + horizon:
            flags.append(self._slot_value(replay, start, cache) is not None)
            start += BUCKET_WIDTH
        return flags

    def horizon_coverage_ratio(self, history: Sequence[HistoryBucket], now: datetime, horizon: timedelta) -> float:
        flags = self._backed(history, now, horizon)
        if flags is None:
            return self._median.horizon_coverage_ratio(history, now, horizon)
        return sum(flags) / len(flags) if flags else 0.0

    def coverage_end(self, history: Sequence[HistoryBucket], now: datetime, horizon: timedelta) -> datetime:
        flags = self._backed(history, now, horizon)
        if flags is None:
            return self._median.coverage_end(history, now, horizon)
        now = now.astimezone(UTC)
        reached = now
        start = floor_to_bucket(now)
        for flag in flags:
            if not flag:
                break
            reached = start + BUCKET_WIDTH
            start += BUCKET_WIDTH
        return reached

    # -- next day -----------------------------------------------------------------------------
    def next_day_forecast(
        self, history: Sequence[HistoryBucket], now: datetime, *, method: str = METHOD_ADAPTIVE
    ) -> NextDayForecast | None:
        """The quarter-hour values of the next local calendar day, or ``None`` without any estimate.

        ``method`` ``METHOD_MEDIAN`` returns the old median forecast for the same day (comparison).
        """
        now = now.astimezone(UTC)
        local = now.astimezone(self._zone)
        target = local.date() + timedelta(days=1)
        midnight = datetime.combine(target, datetime.min.time(), tzinfo=self._zone).astimezone(UTC)
        next_midnight = datetime.combine(
            target + timedelta(days=1), datetime.min.time(), tzinfo=self._zone
        ).astimezone(UTC)
        horizon = next_midnight - now
        if method == METHOD_MEDIAN:
            forecast = self._median.build(history, now, horizon)
        else:
            forecast = self.build(history, now, horizon)
        values = tuple(
            (interval.start.isoformat(), interval.energy_wh)
            for interval in forecast.intervals
            if midnight <= interval.start < next_midnight
        )
        if not values:
            return None
        return NextDayForecast(target.isoformat(), values, self.analyze(history, now))


__all__ = [
    "METHOD_ADAPTIVE",
    "METHOD_MEDIAN",
    "AdaptiveAnalysis",
    "AdaptiveConsumptionForecaster",
    "ConsumptionPhase",
    "ForecastEvaluation",
    "NextDayForecast",
    "PhaseEvent",
    "evaluate_forecast",
    "usable_value",
]
