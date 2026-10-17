#!/usr/bin/env python3
#
# tests/test_consumption_adaptive.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The adaptive next-day consumption forecast: profile, low-activity phase, return and recording.

Every scenario generates a synthetic household (``tests/consumption_scenarios.py``) and asks for a
forecast at a time T with ONLY the buckets that had ended by T; the truth of the forecast day is
generated separately and only used to score. The old median forecaster is scored on the same data.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.admin.consumption import FORECAST_ISSUE_HOUR, ConsumptionHistorian
from app.admin.store import AdminStore
from app.optimizer.adapters import StoredConsumptionForecastProvider
from app.optimizer.adaptive import (
    METHOD_ADAPTIVE,
    METHOD_MEDIAN,
    PROFILE_READY_DAYS,
    AdaptiveConsumptionForecaster,
    ConsumptionPhase,
    evaluate_forecast,
)
from app.optimizer.consumption import (
    BUCKET_WIDTH,
    HistoricalConsumptionForecaster,
    HistoryBucket,
)
from tests.conftest import ManualClock
from tests.consumption_scenarios import LOW, NORMAL, OUTAGE, Household, Mode

BERLIN = "Europe/Berlin"
ZONE = ZoneInfo(BERLIN)


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A Berlin wall-clock instant."""
    return datetime(year, month, day, hour, minute, tzinfo=ZONE)


START = datetime(2026, 1, 5, tzinfo=UTC)
ABSENCE_START = at(2026, 2, 16)  # local, a Monday after six weeks of everyday life
ABSENCE_END = at(2026, 3, 2)  # fourteen days later


def _away(start: datetime, end: datetime) -> Callable[[datetime], Mode]:
    def mode(local: datetime) -> Mode:
        return LOW if start <= local < end else NORMAL

    return mode


def _forecast(
    h: Household,
    f: AdaptiveConsumptionForecaster,
    issued: datetime,
    method: str = METHOD_ADAPTIVE,
):
    """Forecast the next local day at ``issued`` and score it against the generated truth."""
    forecast = f.next_day_forecast(h.history_until(issued), issued, method=method)
    assert forecast is not None
    target = date.fromisoformat(forecast.target_date)
    end = datetime.combine(
        target + timedelta(days=1), datetime.min.time(), tzinfo=h.zone
    ).astimezone(UTC)
    return forecast, evaluate_forecast(forecast.values, h.history_until(end))


def _day_energy(forecast) -> float:
    return sum(v for _, v in forecast.values)


def _events(f: AdaptiveConsumptionForecaster, h: Household, at: datetime, kind: str):
    return [e for e in f.analyze(h.history_until(at), at).events if e.kind == kind]


# --- scenario 1: a return -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("return_hour", "max_delay_hours"), [(8, 5.0), (14, 5.0), (18, 5.0)]
)
def test_return_after_two_weeks_restores_the_everyday_profile(
    return_hour: int, max_delay_hours: float
) -> None:
    returned = ABSENCE_END.replace(hour=return_hour)
    h = Household(START, _away(ABSENCE_START, returned))
    f = AdaptiveConsumptionForecaster(BERLIN)
    zone = ZoneInfo(BERLIN)
    return_at = returned.replace(tzinfo=zone).astimezone(UTC)
    horizon = return_at + timedelta(days=3)

    detected = _events(f, h, horizon, "low_activity_detected")
    assert len(detected) == 1
    onset = ABSENCE_START.replace(tzinfo=zone).astimezone(UTC)
    assert timedelta(0) < detected[0].at - onset <= timedelta(hours=24)

    confirmed = _events(f, h, horizon, "return_confirmed")
    assert len(confirmed) == 1, "exactly one return, no flip-flop"
    delay = confirmed[0].at - return_at
    assert timedelta(0) < delay <= timedelta(hours=max_delay_hours)

    # Before the first return signal nothing can be known: the forecast is still the low level.
    just_before = confirmed[0].at - timedelta(minutes=15)
    assert (
        f.analyze(h.history_until(just_before), just_before).phase
        is ConsumptionPhase.SUSPECTED_LOW_ACTIVITY
    )

    # After the detection the next day is the everyday profile again, like the one before the absence.
    issued = confirmed[0].at + timedelta(minutes=30)
    after, score = _forecast(h, f, issued)
    assert f.analyze(h.history_until(issued), issued).phase is ConsumptionPhase.NORMAL
    reference_issue = issued - timedelta(
        days=28
    )  # same weekday, before the absence began
    before, _ = _forecast(h, AdaptiveConsumptionForecaster(BERLIN), reference_issue)
    assert after.target_date != before.target_date
    assert _day_energy(after) == pytest.approx(_day_energy(before), rel=0.08)
    shape_gap = sum(
        abs(a - b) for (_, a), (_, b) in zip(after.values, before.values, strict=True)
    ) / len(after.values)
    assert shape_gap < 12.0  # Wh per quarter hour; the everyday level is about 40
    assert abs(score.daily_error_pct) < 25.0


def test_vacation_days_never_lower_the_everyday_profile() -> None:
    h = Household(START, _away(ABSENCE_START, ABSENCE_END.replace(hour=8)))
    f = AdaptiveConsumptionForecaster(BERLIN)
    zone = ZoneInfo(BERLIN)
    frozen = ABSENCE_START.replace(tzinfo=zone).astimezone(UTC) + timedelta(
        days=1, hours=20
    )
    later = ABSENCE_END.replace(hour=20, tzinfo=zone).astimezone(UTC)
    inside = f.analyze(h.history_until(frozen), frozen)
    assert inside.phase is ConsumptionPhase.SUSPECTED_LOW_ACTIVITY
    end = f.analyze(h.history_until(later), later)
    assert end.phase is ConsumptionPhase.NORMAL
    assert end.level_factor == pytest.approx(inside.level_factor)
    assert end.profile_days == inside.profile_days  # nothing of the phase was learned


# --- scenario 2: no return ----------------------------------------------------------------------


def test_without_a_return_the_forecast_stays_at_the_low_level() -> None:
    h = Household(START, _away(ABSENCE_START, at(2026, 4, 30)))
    f = AdaptiveConsumptionForecaster(BERLIN)
    zone = ZoneInfo(BERLIN)
    first = ABSENCE_START.replace(tzinfo=zone).astimezone(UTC)
    for offset in (3, 6, 10, 14, 18, 24):
        issued = (
            ABSENCE_START.replace(hour=22, tzinfo=zone) + timedelta(days=offset)
        ).astimezone(UTC)
        forecast, score = _forecast(h, f, issued)
        _, median_score = _forecast(h, f, issued, METHOD_MEDIAN)
        assert forecast.analysis.phase is ConsumptionPhase.SUSPECTED_LOW_ACTIVITY
        assert abs(score.daily_error_pct) < 15.0
        # The median forecaster is still anchored to the old everyday weeks.
        if offset >= 6:
            assert abs(median_score.daily_error_pct) > 100.0
    assert not _events(f, h, first + timedelta(days=24), "return_confirmed")


# --- scenario 3: single high consumers ---------------------------------------------------------


def test_single_high_consumers_during_low_activity_do_not_switch() -> None:
    zone = ZoneInfo(BERLIN)
    spikes: dict[datetime, float] = {}
    day = ABSENCE_START.replace(tzinfo=zone)
    # One bucket of 6 kWh; an oven-like 3 kW for 90 minutes; a dishwasher and a TV evening
    # (about 300 W for three hours); all inside the quiet weeks.
    spikes[(day + timedelta(days=3, hours=12)).astimezone(UTC)] = 6000.0
    for k in range(6):
        spikes[(day + timedelta(days=5, hours=18, minutes=15 * k)).astimezone(UTC)] = (
            750.0
        )
    for k in range(12):
        spikes[(day + timedelta(days=8, hours=18, minutes=15 * k)).astimezone(UTC)] = (
            70.0
        )
    h = Household(START, _away(ABSENCE_START, ABSENCE_END), spikes=spikes)
    f = AdaptiveConsumptionForecaster(BERLIN)
    end = ABSENCE_END.replace(tzinfo=zone).astimezone(UTC) - timedelta(hours=1)
    assert len(_events(f, h, end, "low_activity_detected")) == 1
    assert not _events(f, h, end, "return_confirmed")
    assert (
        f.analyze(h.history_until(end), end).phase
        is ConsumptionPhase.SUSPECTED_LOW_ACTIVITY
    )
    # The day after the oven the forecast is still the low level, not pulled up by the spike.
    issued = (day + timedelta(days=5)).replace(hour=22).astimezone(UTC)
    _, score = _forecast(h, f, issued)
    assert abs(score.daily_error_pct) < 25.0


# --- scenario 4: measurement outage ------------------------------------------------------------


def test_outage_in_everyday_life_is_neither_vacation_nor_zero() -> None:
    def mode(local: datetime) -> Mode:
        if at(2026, 2, 9) <= local < at(2026, 2, 15):
            return OUTAGE
        if at(2026, 2, 15) <= local < at(2026, 2, 16):
            return ("estimated", 1.0)
        return NORMAL

    h = Household(START, mode)
    f = AdaptiveConsumptionForecaster(BERLIN)
    zone = ZoneInfo(BERLIN)
    scored = 0
    for issued in (
        at(2026, 2, 12, 22),
        at(2026, 2, 15, 22),
        at(2026, 2, 17, 22),
        at(2026, 2, 20, 22),
    ):
        moment = issued.replace(tzinfo=zone).astimezone(UTC)
        forecast, score = _forecast(h, f, moment)
        assert forecast.analysis.phase is ConsumptionPhase.NORMAL
        assert min(v for _, v in forecast.values) > 0.0
        if score is not None:  # the first probe targets a day inside the outage
            assert abs(score.daily_error_pct) < 30.0
            scored += 1
    assert scored == 3
    assert not _events(f, h, datetime(2026, 2, 28, tzinfo=UTC), "low_activity_detected")
    analysis = f.analyze(
        h.history_until(datetime(2026, 2, 13, 12, tzinfo=UTC)),
        datetime(2026, 2, 13, 12, tzinfo=UTC),
    )
    assert analysis.data_coverage < 0.7
    assert any("measured" in reason for reason in analysis.reasons)


def test_outage_during_low_activity_neither_returns_nor_forgets() -> None:
    zone = ZoneInfo(BERLIN)

    def mode(local: datetime) -> Mode:
        if at(2026, 2, 21) <= local < at(2026, 2, 26):
            return OUTAGE
        return LOW if ABSENCE_START <= local else NORMAL

    h = Household(START, mode)
    f = AdaptiveConsumptionForecaster(BERLIN)
    moment = datetime(2026, 2, 26, 8, tzinfo=zone).astimezone(UTC)
    assert (
        f.analyze(h.history_until(moment), moment).phase
        is ConsumptionPhase.SUSPECTED_LOW_ACTIVITY
    )
    assert not _events(f, h, moment, "return_confirmed")


# --- scenario 5: permanent changes --------------------------------------------------------------


@pytest.mark.parametrize("factor", [1.4, 0.75])
def test_a_permanent_level_change_is_learned_traceably(factor: float) -> None:
    change = at(2026, 2, 16)

    def mode(local: datetime) -> Mode:
        return ("normal", factor) if local >= change else NORMAL

    h = Household(START, mode)
    f = AdaptiveConsumptionForecaster(BERLIN)
    zone = ZoneInfo(BERLIN)
    start = change.replace(tzinfo=zone).astimezone(UTC)
    changed = _events(f, h, start + timedelta(days=12), "level_changed")
    assert changed, "the level change is recorded as an event"
    assert changed[0].at - start <= timedelta(days=5)
    _, score = _forecast(h, f, (start + timedelta(days=12)).replace(hour=22))
    assert abs(score.daily_error_pct) < 20.0
    assert f.analyze(
        h.history_until(start + timedelta(days=12)), start + timedelta(days=12)
    ).level_factor == pytest.approx(factor, rel=0.2)


def test_a_phase_longer_than_four_weeks_becomes_the_new_normal() -> None:
    h = Household(START, _away(ABSENCE_START, at(2026, 12, 31)))
    f = AdaptiveConsumptionForecaster(BERLIN)
    zone = ZoneInfo(BERLIN)
    moment = datetime(2026, 4, 5, 22, tzinfo=zone).astimezone(UTC)
    analysis = f.analyze(h.history_until(moment), moment)
    assert analysis.promoted_at is not None
    assert analysis.phase is ConsumptionPhase.NORMAL
    _, score = _forecast(h, f, moment)
    assert abs(score.daily_error_pct) < 15.0


# --- scenario 6: restart ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "moment", [at(2026, 2, 24, 15), at(2026, 3, 2, 17, 30), at(2026, 3, 2, 19)]
)
def test_a_restart_reconstructs_the_state_by_replay(moment: datetime) -> None:
    # 17:30 is mid-detection of an 14:00 return, 19:00 is after the verdict; 24 Feb is mid-absence.
    h = Household(START, _away(ABSENCE_START, ABSENCE_END.replace(hour=14)))
    running = AdaptiveConsumptionForecaster(BERLIN)
    zone = ZoneInfo(BERLIN)
    for step in range(6):  # the process keeps being asked as time passes
        past = moment.replace(tzinfo=zone).astimezone(UTC) - timedelta(
            minutes=15 * (5 - step)
        )
        running.build(h.history_until(past), past, timedelta(hours=30))
    now = moment.replace(tzinfo=zone).astimezone(UTC)
    restarted = AdaptiveConsumptionForecaster(BERLIN)
    history = h.history_until(now)
    assert running.analyze(history, now) == restarted.analyze(history, now)
    assert running.build(history, now, timedelta(hours=30)) == restarted.build(
        history, now, timedelta(hours=30)
    )


def test_the_state_survives_the_store_and_a_new_provider(tmp_path: Path) -> None:
    h = Household(START, _away(ABSENCE_START, ABSENCE_END.replace(hour=14)))
    zone = ZoneInfo(BERLIN)
    now = datetime(2026, 2, 25, 12, tzinfo=zone).astimezone(UTC)
    store = AdminStore(tmp_path / "rct.db", "x" * 32)
    store.initialize()
    store.upsert_consumption_intervals(
        "main",
        [
            {
                "interval_start": b.start.isoformat(),
                "interval_end": b.end.isoformat(),
                "energy_wh": b.energy_wh,
                "sample_count": 30,
                "coverage_seconds": 900.0,
            }
            for b in h.history_until(now)
        ],
    )
    first = StoredConsumptionForecastProvider(store, device_id="main", timezone=BERLIN)
    reopened = AdminStore(tmp_path / "rct.db", "x" * 32)
    second = StoredConsumptionForecastProvider(
        reopened, device_id="main", timezone=BERLIN
    )
    assert first.analysis(now).phase is ConsumptionPhase.SUSPECTED_LOW_ACTIVITY
    assert first.analysis(now) == second.analysis(now)
    assert first.forecast(now, timedelta(hours=30)) == second.forecast(
        now, timedelta(hours=30)
    )


# --- scenario 7: DST, devices, thin history -----------------------------------------------------


@pytest.mark.parametrize(
    ("history_start", "issued", "slots"),
    [
        (datetime(2026, 2, 1, tzinfo=UTC), at(2026, 3, 28, 22), 92),
        (datetime(2026, 8, 20, tzinfo=UTC), at(2026, 10, 24, 22), 100),
    ],
)
def test_dst_days_have_92_and_100_slots_and_cause_no_false_phase(
    history_start, issued, slots
) -> None:
    h = Household(history_start, lambda local: NORMAL)
    f = AdaptiveConsumptionForecaster(BERLIN)
    moment = issued.replace(tzinfo=ZoneInfo(BERLIN)).astimezone(UTC)
    forecast, score = _forecast(h, f, moment)
    assert len(forecast.values) == slots
    assert forecast.analysis.phase is ConsumptionPhase.NORMAL
    assert not _events(f, h, moment + timedelta(days=2), "low_activity_detected")
    assert (
        abs(score.daily_error_pct) < 35.0
    )  # one noisy day with random loads; the bias is about -13 %


def test_devices_are_judged_independently(tmp_path: Path) -> None:
    zone = ZoneInfo(BERLIN)
    away = Household(START, _away(ABSENCE_START, at(2026, 4, 1)), seed=3)
    home = Household(START, lambda local: NORMAL, seed=5)
    now = datetime(2026, 2, 25, 12, tzinfo=zone).astimezone(UTC)
    store = AdminStore(tmp_path / "rct.db", "x" * 32)
    store.initialize()
    for device, household in (("a", away), ("b", home)):
        store.upsert_consumption_intervals(
            device,
            [
                {
                    "interval_start": b.start.isoformat(),
                    "interval_end": b.end.isoformat(),
                    "energy_wh": b.energy_wh,
                    "sample_count": 30,
                    "coverage_seconds": 900.0,
                }
                for b in household.history_until(now)
            ],
        )
    phases = {
        d: StoredConsumptionForecastProvider(store, device_id=d, timezone=BERLIN)
        .analysis(now)
        .phase
        for d in ("a", "b")
    }
    assert phases == {
        "a": ConsumptionPhase.SUSPECTED_LOW_ACTIVITY,
        "b": ConsumptionPhase.NORMAL,
    }


def test_insufficient_history_is_reported_and_the_median_answers() -> None:
    h = Household(START, lambda local: NORMAL)
    f = AdaptiveConsumptionForecaster(BERLIN)
    now = START + timedelta(days=PROFILE_READY_DAYS - 3)
    history = h.history_until(now)
    analysis = f.analyze(history, now)
    assert analysis.phase is ConsumptionPhase.INSUFFICIENT_HISTORY
    assert analysis.method == "median-fallback"
    assert any("required days" in reason for reason in analysis.reasons)
    horizon = timedelta(hours=30)
    assert f.build(history, now, horizon) == HistoricalConsumptionForecaster(
        BERLIN
    ).build(history, now, horizon)
    assert f.horizon_coverage_ratio(
        history, now, horizon
    ) == HistoricalConsumptionForecaster(BERLIN).horizon_coverage_ratio(
        history, now, horizon
    )
    assert f.analyze([], now).phase is ConsumptionPhase.INSUFFICIENT_HISTORY


def test_learning_state_median_fallback_excludes_estimated_energy() -> None:
    # Item 2: while the adaptive forecaster is still learning it delegates straight to the median
    # forecaster. With the unified usable_value rule that fallback must NOT admit ESTIMATED energy,
    # so flipping well-measured buckets to ESTIMATED (with inflated energy) cannot raise the result.
    f = AdaptiveConsumptionForecaster(BERLIN)
    now = START + timedelta(
        days=PROFILE_READY_DAYS - 3
    )  # still learning -> median answers
    h = Household(START, lambda local: NORMAL)
    history = h.history_until(now)
    assert f.analyze(history, now).phase is ConsumptionPhase.INSUFFICIENT_HISTORY

    inflated: list[HistoryBucket] = []
    for bucket in history:
        if bucket.start.hour in (18, 19):
            # Keep the original (large) energy but mark it as estimated: ESTIMATED must be ignored.
            inflated.append(
                HistoryBucket(
                    bucket.start,
                    bucket.end,
                    bucket.energy_wh + 9000.0,
                    bucket.sample_count,
                    0.0,
                    estimated_seconds=900.0,
                )
            )
        else:
            inflated.append(bucket)
    horizon = timedelta(hours=30)
    clean = f.build([b for b in history if b.start.hour not in (18, 19)], now, horizon)
    mixed = f.build(inflated, now, horizon)
    clean_by_start = {iv.start: iv.energy_wh for iv in clean.intervals}
    # Every evening slot present in the ESTIMATED-contaminated build matches the clean build: the
    # +9000 Wh estimated spikes never leak into the median fallback.
    for interval in mixed.intervals:
        if (
            interval.start.astimezone(ZONE).hour in (18, 19)
            and interval.start in clean_by_start
        ):
            assert interval.energy_wh == pytest.approx(clean_by_start[interval.start])


def test_estimated_and_missing_buckets_are_never_observations() -> None:
    h = Household(START, lambda local: NORMAL)
    f = AdaptiveConsumptionForecaster(BERLIN)
    now = START + timedelta(days=30)
    history = h.history_until(now)
    degraded: list[HistoryBucket] = []
    for bucket in history:
        if bucket.start.hour in (18, 19):
            degraded.append(
                HistoryBucket(
                    bucket.start, bucket.end, 0.0, 0, 0.0, estimated_seconds=900.0
                )
            )
        else:
            degraded.append(bucket)
    clean = f.analyze([b for b in history if b.start.hour not in (18, 19)], now)
    mixed = f.analyze(degraded, now)
    assert mixed.phase is clean.phase is ConsumptionPhase.NORMAL
    assert mixed.profile_days == clean.profile_days
    assert mixed.events == clean.events


# --- comparison with the median method ----------------------------------------------------------


def test_everyday_forecasts_are_not_worse_than_the_median_method() -> None:
    h = Household(START, lambda local: NORMAL)
    f = AdaptiveConsumptionForecaster(BERLIN)
    zone = ZoneInfo(BERLIN)
    adaptive = []
    median = []
    for day in range(20, 56):
        issued = (
            datetime(2026, 1, 5, 22, tzinfo=zone) + timedelta(days=day)
        ).astimezone(UTC)
        _, a = _forecast(h, f, issued)
        _, m = _forecast(h, f, issued, METHOD_MEDIAN)
        adaptive.append(a)
        median.append(m)
    adaptive_mae = sum(s.mae_wh for s in adaptive) / len(adaptive)
    median_mae = sum(s.mae_wh for s in median) / len(median)
    adaptive_daily = sum(abs(s.daily_error_pct) for s in adaptive) / len(adaptive)
    median_daily = sum(abs(s.daily_error_pct) for s in median) / len(median)
    assert adaptive_mae <= median_mae * 1.05
    assert adaptive_daily <= median_daily * 1.05


# --- recording and later comparison ------------------------------------------------------------


async def test_the_historian_stores_one_forecast_per_target_day_and_reviews_it(
    tmp_path: Path, caplog
) -> None:
    h = Household(START, lambda local: NORMAL)
    zone = ZoneInfo(BERLIN)
    store = AdminStore(tmp_path / "rct.db", "x" * 32)
    store.initialize()
    clock = ManualClock()
    historian = ConsumptionHistorian(store, clock, timezone=lambda: BERLIN)
    historian._anchors["main"] = {}  # the device is known to the drain
    evening = datetime(2026, 2, 20, FORECAST_ISSUE_HOUR, 5, tzinfo=zone).astimezone(UTC)
    store.upsert_consumption_intervals(
        "main",
        [
            {
                "interval_start": b.start.isoformat(),
                "interval_end": b.end.isoformat(),
                "energy_wh": b.energy_wh,
                "sample_count": 30,
                "coverage_seconds": 900.0,
            }
            for b in h.history_until(evening)
        ],
    )
    clock.advance((evening - clock.now()).total_seconds())
    await historian.record_forecasts_once()
    await historian.record_forecasts_once()  # a second pass must not add a second run
    runs = store.get_consumption_forecast_runs(
        "main", "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"
    )
    assert sorted(run["method"] for run in runs) == [METHOD_ADAPTIVE, METHOD_MEDIAN]
    adaptive = next(run for run in runs if run["method"] == METHOD_ADAPTIVE)
    assert adaptive["target_date"] == "2026-02-21"
    assert len(adaptive["values"]) == 96
    assert '"phase":"normal"' in adaptive["explanation"]
    # Two days later the measured day exists, so the stored run can be compared with it.
    later = datetime(2026, 2, 22, 8, tzinfo=zone).astimezone(UTC)
    store.upsert_consumption_intervals(
        "main",
        [
            {
                "interval_start": b.start.isoformat(),
                "interval_end": b.end.isoformat(),
                "energy_wh": b.energy_wh,
                "sample_count": 30,
                "coverage_seconds": 900.0,
            }
            for b in h.history_until(later)
            if b.start >= evening
        ],
    )
    provider = StoredConsumptionForecastProvider(
        store, device_id="main", timezone=BERLIN
    )
    results = {
        run["method"]: result
        for run, result in provider.evaluate_runs(later, evening - timedelta(hours=1))
    }
    assert results[METHOD_ADAPTIVE] is not None
    assert results[METHOD_ADAPTIVE].slots == 96
    assert abs(results[METHOD_ADAPTIVE].daily_error_pct) < 30.0


def test_evaluation_needs_sufficiently_measured_actuals() -> None:
    start = datetime(2026, 2, 21, tzinfo=UTC)
    values = [((start + BUCKET_WIDTH * k).isoformat(), 40.0) for k in range(96)]
    thin = [
        HistoryBucket(
            start + BUCKET_WIDTH * k, start + BUCKET_WIDTH * (k + 1), 40.0, 5, 900.0
        )
        for k in range(60)
    ]
    assert evaluate_forecast(values, thin) is None
    full = thin + [
        HistoryBucket(
            start + BUCKET_WIDTH * k, start + BUCKET_WIDTH * (k + 1), 50.0, 5, 900.0
        )
        for k in range(60, 96)
    ]
    result = evaluate_forecast(values, full)
    assert result is not None
    assert result.slots == 96
    assert result.mae_wh == pytest.approx(36 * 10.0 / 96)
    assert result.daily_error_pct == pytest.approx(
        100.0 * (96 * 40 - (60 * 40 + 36 * 50)) / (60 * 40 + 36 * 50)
    )
