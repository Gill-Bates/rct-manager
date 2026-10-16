#!/usr/bin/env python3
#
# tests/test_consumption_meter.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Counter-based consumption capture: attribution, quality flags, persistence across stops, retries."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.admin.consumption import ConsumptionHistorian
from app.admin.store import AdminStore
from app.optimizer.consumption import BucketQuality, floor_to_bucket, rows_to_history
from app.optimizer.meter import DAY_COUNTER, TOTAL_COUNTER, attribute_counter, meter_rows

T0 = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
KWH = 1000.0


def _series(start_wh: float, watts: float, minutes: int, step_s: int = 30, begin: datetime = T0):
    return [
        (begin + timedelta(seconds=i * step_s), start_wh + watts * i * step_s / 3600.0)
        for i in range(minutes * 60 // step_s + 1)
    ]


def _store(tmp_path: Path) -> AdminStore:
    store = AdminStore(tmp_path / "rct.db", "x" * 32)
    store.initialize()
    return store


def _history(store: AdminStore):
    first, last = (T0 - timedelta(days=1)).isoformat(), (T0 + timedelta(days=1)).isoformat()
    return rows_to_history(
        store.get_consumption_intervals("main", first, last), store.get_consumption_meter_intervals("main", first, last)
    )


# --- attribution ---------------------------------------------------------------------------------


def test_contiguous_readings_are_measured_not_estimated() -> None:
    result = attribute_counter(_series(10_000 * KWH, 1200.0, 30))
    rows = {row["interval_start"]: row for row in meter_rows(result.buckets)}
    first = rows[T0.isoformat()]
    assert first["measured_wh"] == pytest.approx(300.0, abs=0.01)  # 1200 W for 15 min
    assert first["measured_seconds"] == pytest.approx(900.0)
    assert first["estimated_seconds"] == 0.0


def test_a_stop_is_spread_as_a_flagged_estimate_and_the_total_is_preserved() -> None:
    before = (T0, 10_000 * KWH)
    after = (T0 + timedelta(hours=2), 10_003 * KWH)  # +3 kWh while the app was stopped
    result = attribute_counter([after], anchor=before)
    rows = meter_rows(result.buckets)
    assert len(rows) == 8
    assert sum(r["estimated_wh"] for r in rows) == pytest.approx(3000.0)
    assert all(r["measured_seconds"] == 0.0 and r["estimated_seconds"] == 900.0 for r in rows)
    assert result.stats.estimated_pairs == 1


def test_estimated_buckets_are_not_learning_data_and_not_missing() -> None:
    result = attribute_counter([(T0 + timedelta(hours=2), 10_003 * KWH)], anchor=(T0, 10_000 * KWH))
    history = rows_to_history([], meter_rows(result.buckets))
    assert {bucket.quality for bucket in history} == {BucketQuality.ESTIMATED}
    assert all(bucket.energy_wh == pytest.approx(375.0) for bucket in history)
    assert not any(bucket.well_covered for bucket in history)  # the legacy forecaster ignores them too


def test_a_decreasing_counter_is_a_reset_and_adds_no_energy_but_the_next_pair_counts() -> None:
    readings = [(T0, 5_000.0), (T0 + timedelta(seconds=30), 5_010.0), (T0 + timedelta(seconds=60), 2.0),
                (T0 + timedelta(seconds=90), 12.0)]
    result = attribute_counter(readings)
    rows = meter_rows(result.buckets)
    assert sum(r["measured_wh"] for r in rows) == pytest.approx(20.0)  # 10 Wh before and 10 Wh after the reset
    assert result.stats.discarded_negative == 1


def test_an_implausible_jump_is_discarded() -> None:
    readings = [(T0, 1_000.0), (T0 + timedelta(seconds=30), 500_000.0)]  # about 60 MW
    result = attribute_counter(readings)
    assert result.buckets == {}
    assert result.stats.discarded_implausible == 1


def test_a_flat_counter_across_a_gap_is_stalled_not_zero_consumption() -> None:
    result = attribute_counter([(T0 + timedelta(hours=1), 7_000.0)], anchor=(T0, 7_000.0))
    assert result.buckets == {}
    assert result.stats.discarded_stalled == 1


def test_a_gap_beyond_a_day_is_left_unattributed() -> None:
    result = attribute_counter([(T0 + timedelta(days=3), 9_000.0)], anchor=(T0, 7_000.0))
    assert result.buckets == {}
    assert result.stats.unattributed_wh == pytest.approx(2_000.0)


def test_the_day_counter_stands_in_when_the_total_counter_was_replaced() -> None:
    total = [(T0, 9_000.0), (T0 + timedelta(seconds=60), 3.0)]  # total drops: unusable
    day = [(T0, 100.0), (T0 + timedelta(seconds=60), 112.0)]
    result = attribute_counter(total, day)
    assert sum(r["measured_wh"] for r in meter_rows(result.buckets)) == pytest.approx(12.0)
    assert result.stats.day_fallbacks == 1


def test_a_day_rollover_does_not_disturb_the_total_and_a_real_mismatch_is_counted() -> None:
    total = [(T0, 9_000.0), (T0 + timedelta(seconds=60), 9_010.0)]
    rollover = [(T0, 5_000.0), (T0 + timedelta(seconds=60), 3.0)]  # midnight: the day counter restarts
    assert attribute_counter(total, rollover).stats.cross_check_mismatches == 0
    disagreeing = [(T0, 100.0), (T0 + timedelta(seconds=60), 400.0)]
    assert attribute_counter(total, disagreeing).stats.cross_check_mismatches == 1


def test_cross_check_mismatch_warnings_are_summarized_without_claiming_rejection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.conftest import ManualClock

    clock = ManualClock()
    historian = ConsumptionHistorian(_store(tmp_path), clock)
    with caplog.at_level(logging.INFO, logger="app.admin.consumption"):
        for index in range(32):
            instant = T0 + timedelta(seconds=30 * index)
            historian._attribute_counters(
                "main",
                {
                    TOTAL_COUNTER: [(instant, 10_000.0 + 10.0 * index)],
                    DAY_COUNTER: [(instant, 100.0 + 300.0 * index)],
                },
            )
            clock.advance(30)

    # A pure cross-check mismatch discards nothing, so it is INFO, never WARNING.
    mismatches = [
        record for record in caplog.records
        if "cross-check mismatch" in record.getMessage() and "total counter retained" in record.getMessage()
    ]
    assert len(mismatches) == 2
    assert all(record.levelno == logging.INFO for record in mismatches)
    assert "29 previously suppressed" in mismatches[1].getMessage()
    assert all("rejected" not in record.getMessage() for record in mismatches)
    assert not [record for record in caplog.records if record.levelno == logging.WARNING]


def test_rejected_counter_pairs_still_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from tests.conftest import ManualClock

    historian = ConsumptionHistorian(_store(tmp_path), ManualClock())
    with caplog.at_level(logging.WARNING, logger="app.admin.consumption"):
        historian._attribute_counters(
            "main", {TOTAL_COUNTER: [(T0, 10_000.0), (T0 + timedelta(seconds=30), 1.0)]}
        )
        historian._attribute_counters(
            "main", {TOTAL_COUNTER: [(T0 + timedelta(minutes=30), 1.0)]}
        )

    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any("reset/negative 1" in message for message in warnings)
    assert any("stalled 1" in message for message in warnings)


def test_meter_wins_over_power_without_double_counting() -> None:
    bucket = floor_to_bucket(T0)
    power = [{"interval_start": bucket.isoformat(), "interval_end": (bucket + timedelta(minutes=15)).isoformat(),
              "energy_wh": 280.0, "sample_count": 30, "coverage_seconds": 900.0}]
    meter = [{"interval_start": bucket.isoformat(), "interval_end": (bucket + timedelta(minutes=15)).isoformat(),
              "measured_wh": 300.0, "measured_seconds": 900.0, "estimated_wh": 0.0, "estimated_seconds": 0.0}]
    (merged,) = rows_to_history(power, meter)
    assert merged.energy_wh == pytest.approx(300.0)
    assert merged.quality is BucketQuality.COMPLETE


def test_partial_coverage_is_classified_and_normalised_not_taken_as_full() -> None:
    bucket = floor_to_bucket(T0)
    row = {"interval_start": bucket.isoformat(), "interval_end": (bucket + timedelta(minutes=15)).isoformat(),
           "energy_wh": 100.0, "sample_count": 20, "coverage_seconds": 675.0}
    (merged,) = rows_to_history([row])
    assert merged.quality is BucketQuality.PARTIAL
    assert merged.normalized_wh == pytest.approx(133.33, abs=0.01)


# --- historian: persistence across a stop, retries, shutdown ---------------------------------------


def _feed(historian: ConsumptionHistorian, readings, metric: str = TOTAL_COUNTER) -> None:
    for instant, value in readings:
        historian.sink.sample("main", metric, value, instant)


def test_a_restart_bridges_the_stop_through_the_persisted_counter(tmp_path: Path) -> None:
    from tests.conftest import ManualClock

    store = _store(tmp_path)
    first = ConsumptionHistorian(store, ManualClock())
    _feed(first, _series(10_000 * KWH, 1200.0, 10))
    asyncio.run(first.drain_once())
    # The app stops; a new process starts two hours later and sees only the new counter value.
    second = ConsumptionHistorian(store, ManualClock())
    resume = T0 + timedelta(hours=2, minutes=10)
    _feed(second, [(resume, 10_000 * KWH + 200.0 + 3_000.0)])
    asyncio.run(second.drain_once())

    history = _history(store)
    estimated = [b for b in history if b.quality is BucketQuality.ESTIMATED]
    assert len(estimated) >= 8
    assert sum(b.energy_wh for b in history) == pytest.approx(3_200.0, abs=1.0)  # nothing lost, nothing doubled


def test_a_failed_write_is_retried_without_double_addition(tmp_path: Path) -> None:
    from tests.conftest import ManualClock

    store = _store(tmp_path)
    original = store.upsert_consumption_intervals
    calls = {"n": 0}

    def flaky(*args):
        calls["n"] += 1
        applied = original(*args)
        if calls["n"] == 1:
            raise OSError("write acknowledged late")  # committed, but the caller saw a failure
        return applied

    store.upsert_consumption_intervals = flaky  # type: ignore[method-assign]
    historian = ConsumptionHistorian(store, ManualClock())
    _feed(historian, _series(1_000.0, 600.0, 5))
    asyncio.run(historian.drain_once())  # fails, batch stays queued
    asyncio.run(historian.drain_once())  # retried; the store recognises the batch
    history = _history(store)
    assert sum(b.energy_wh for b in history) == pytest.approx(50.0, abs=0.1)  # 600 W for 5 min, once


def test_a_batch_that_never_reached_the_store_is_written_by_the_retry(tmp_path: Path) -> None:
    from tests.conftest import ManualClock

    store = _store(tmp_path)
    original = store.upsert_consumption_intervals
    state = {"fail": True}

    def flaky(*args):
        if state["fail"]:
            raise OSError("disk busy")
        return original(*args)

    store.upsert_consumption_intervals = flaky  # type: ignore[method-assign]
    historian = ConsumptionHistorian(store, ManualClock())
    _feed(historian, _series(1_000.0, 600.0, 5))
    asyncio.run(historian.drain_once())
    state["fail"] = False
    asyncio.run(historian.drain_once())
    assert sum(b.energy_wh for b in _history(store)) == pytest.approx(50.0, abs=0.1)


def test_a_regular_shutdown_performs_a_bounded_final_drain(tmp_path: Path) -> None:
    from tests.conftest import ManualClock

    async def scenario() -> None:
        store = _store(tmp_path)
        historian = ConsumptionHistorian(store, ManualClock())
        task = asyncio.create_task(historian.run())
        await asyncio.sleep(0)
        _feed(historian, _series(1_000.0, 600.0, 5))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sum(b.energy_wh for b in _history(store)) == pytest.approx(50.0, abs=0.1)

    asyncio.run(scenario())


def test_counter_readings_are_thinned_and_the_day_counter_is_stored(tmp_path: Path) -> None:
    from tests.conftest import ManualClock

    store = _store(tmp_path)
    historian = ConsumptionHistorian(store, ManualClock())
    _feed(historian, _series(10_000.0, 1200.0, 30))
    _feed(historian, _series(1_000.0, 1200.0, 30), DAY_COUNTER)
    asyncio.run(historian.drain_once())
    stored = store.get_consumption_counter_readings(
        "main", TOTAL_COUNTER, T0.isoformat(), (T0 + timedelta(hours=1)).isoformat()
    )
    assert 2 <= len(stored) <= 4  # about one per quarter hour, not one per poll
    assert set(store.get_consumption_counter_state("main")) == {TOTAL_COUNTER, DAY_COUNTER}
