#!/usr/bin/env python3
#
# tests/test_dynamic_adapters.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Input safety and forecast quality for the Dry-Run plan adapters.

These are the impure boundary between local data and the pure optimizer. The invariants under test:
missing price, solar and battery inputs report explicit unavailable statuses; missing consumption
history uses the configured baseline with low confidence. Non-finite battery limits fail closed.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.energy.readings import DeviceReading, EnergyReadings
from app.optimizer.adapters import (
    BatteryCapacityUnavailable,
    DryRunInputs,
    EnergyReadingsBatteryStateProvider,
    PlanStatus,
    build_dry_run_plan,
)
from app.optimizer.models import (
    ConsumptionForecast,
    ConsumptionForecastInterval,
    SolarForecast,
    SolarForecastInterval,
)
from app.spotmarket.models import SpotMarketProvider, SpotPriceInterval, SpotPriceSnapshot

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
SLOT = timedelta(minutes=15)


class _Clock:
    def now(self) -> datetime:
        return NOW


class _ReadingsPort:
    def __init__(self, soc: DeviceReading) -> None:
        self._soc = soc

    def readings(self, device_id: str) -> EnergyReadings:
        absent = DeviceReading(None, None, True)
        return EnergyReadings(
            battery_soc_percent=self._soc,
            grid_power_w=absent,
            pv_power_w=absent,
            house_load_w=absent,
        )


def _provider(**overrides) -> EnergyReadingsBatteryStateProvider:
    values = {
        "device_id": "main",
        "usable_capacity_wh": 10_000.0,
        "charge_power_w": 3000.0,
        "discharge_power_w": 3000.0,
        "clock": _Clock(),
    }
    values.update(overrides)
    port = _ReadingsPort(DeviceReading(50.0, 1.0, False))
    return EnergyReadingsBatteryStateProvider(port, **values)


def test_live_limit_is_passed_through_only_when_usable() -> None:
    assert _provider(live_charge_limit_w=1500.0).current().live_charge_limit_w == 1500.0
    assert _provider(live_charge_limit_w=1500.0).current().permitted_charge_power_w <= 1500.0
    for bad in (None, 0.0, -5.0, float("nan"), float("inf")):
        state = _provider(live_charge_limit_w=bad).current()
        assert state.live_charge_limit_w is None
        assert state.permitted_charge_power_w == state.max_charge_power_w  # missing is not unlimited


# --- finding 7: non-finite power limits must fail closed to the provider contract --------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_finding_7_non_finite_charge_power_fails_closed(bad: float) -> None:
    with pytest.raises(BatteryCapacityUnavailable):
        _provider(charge_power_w=bad).current()


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_finding_7_non_finite_discharge_power_fails_closed(bad: float) -> None:
    with pytest.raises(BatteryCapacityUnavailable):
        _provider(discharge_power_w=bad).current()


def test_finding_7_valid_limits_still_build_a_battery_state() -> None:
    battery = _provider().current()
    assert battery.soc_percent == pytest.approx(50.0)
    assert battery.max_charge_power_w == pytest.approx(3000.0)


# --- P2-8: usable capacity is per-device, operator-supplied, fail-closed when absent -----------


@pytest.mark.parametrize("capacity_wh", [8_000.0, 20_000.0])
def test_p2_8_per_device_capacity_drives_the_battery_state(capacity_wh: float) -> None:
    # The capacity threaded in per device is exactly what the planner uses to convert SoC<->Wh;
    # two different capacities yield two different battery states in the same install.
    battery = _provider(usable_capacity_wh=capacity_wh).current()
    assert battery.usable_capacity_wh == pytest.approx(capacity_wh)


def test_p2_8_two_devices_get_their_own_capacity() -> None:
    small = _provider(device_id="main", usable_capacity_wh=8_000.0).current()
    large = _provider(device_id="slave1", usable_capacity_wh=20_000.0).current()
    assert small.usable_capacity_wh == pytest.approx(8_000.0)
    assert large.usable_capacity_wh == pytest.approx(20_000.0)
    assert small.usable_capacity_wh != large.usable_capacity_wh


@pytest.mark.parametrize("missing", [None, 0.0, float("nan"), float("inf")])
def test_p2_8_device_without_capacity_fails_closed(missing: float | None) -> None:
    # A device with no (or an invalid) per-device capacity fails closed; it is never defaulted to a
    # global or guessed value.
    with pytest.raises(BatteryCapacityUnavailable):
        _provider(usable_capacity_wh=missing).current()


# --- finding 2: fail-closed on partial horizon coverage of prices and solar --------------------


def _prices(hours: float, start: datetime = NOW) -> SpotPriceSnapshot:
    count = int(hours * 4)
    return SpotPriceSnapshot(
        provider=SpotMarketProvider.ENERGY_CHARTS, bidding_zone="DE-LU", currency="EUR", fetched_at=NOW,
        intervals=tuple(
            SpotPriceInterval(start + SLOT * i, start + SLOT * (i + 1), Decimal("0.2")) for i in range(count)
        ),
    )


def _solar(hours: float, start: datetime = NOW) -> SolarForecast:
    count = int(hours * 4)
    return SolarForecast(
        created_at=NOW,
        intervals=tuple(
            SolarForecastInterval(start + SLOT * i, start + SLOT * (i + 1), 0.0) for i in range(count)
        ),
    )


class _BatteryProvider:
    def current(self):
        return _provider().current()


class _ConsumptionProvider:
    """A fake forecast provider. ``coverage_hours`` is how far from ``now`` its per-slot history
    reaches (the finding-4 common-horizon input); ``coverage`` is the finding-6 ratio gate.
    """

    def __init__(self, *, well_covered: int, coverage: float, coverage_hours: float = 48.0) -> None:
        self._well_covered = well_covered
        self._coverage = coverage
        self._coverage_hours = coverage_hours

    def well_covered_count(self, now: datetime) -> int:
        return self._well_covered

    def horizon_coverage_ratio(self, now: datetime, horizon: timedelta) -> float:
        return self._coverage

    def coverage_end(self, now: datetime, horizon: timedelta) -> datetime:
        return min(now + horizon, now + timedelta(hours=self._coverage_hours))

    def forecast(self, now: datetime, horizon: timedelta) -> ConsumptionForecast:
        count = int(horizon.total_seconds() // 900)
        return ConsumptionForecast(
            created_at=now,
            intervals=tuple(
                ConsumptionForecastInterval(now + SLOT * i, now + SLOT * (i + 1), 100.0) for i in range(count)
            ),
        )


def _build(*, prices, solar, well_covered=50, coverage=1.0, coverage_hours=48.0, horizon=timedelta(hours=36)):
    return build_dry_run_plan(
        now=NOW,
        battery_provider=_BatteryProvider(),
        consumption_provider=_ConsumptionProvider(
            well_covered=well_covered, coverage=coverage, coverage_hours=coverage_hours
        ),
        inputs=DryRunInputs(prices=prices, solar=solar, spot_enabled=True, solar_configurable=True),
        horizon=horizon,
        baseline_household_load_w=620.0,
    )


def test_finding_2_prices_short_of_the_horizon_report_spot_prices_unavailable() -> None:
    # Prices reach only 2 h, below MIN_PLANNING_HORIZON: fail closed on the limiting input, no zeros.
    result = _build(prices=_prices(2.0), solar=_solar(36.0), coverage_hours=36.0)
    assert result.status is PlanStatus.SPOT_PRICES_UNAVAILABLE
    assert result.plan is None
    assert "spot prices" in (result.detail or "")


def test_finding_2_solar_short_of_the_horizon_reports_solar_forecast_unavailable() -> None:
    # Prices and consumption reach far; solar only 2 h is the limiter and names the solar status.
    result = _build(prices=_prices(36.0), solar=_solar(2.0), coverage_hours=36.0)
    assert result.status is PlanStatus.SOLAR_FORECAST_UNAVAILABLE
    assert result.plan is None
    assert "solar forecast" in (result.detail or "")


def test_disabled_spot_and_unconfigured_solar_are_distinct_from_unavailable_data() -> None:
    # Stored data is present, yet an input the operator switched off must not be planned with.
    def build(**flags):
        return build_dry_run_plan(
            now=NOW,
            battery_provider=_BatteryProvider(),
            consumption_provider=_ConsumptionProvider(well_covered=50, coverage=1.0, coverage_hours=48.0),
            inputs=DryRunInputs(prices=_prices(36.0), solar=_solar(36.0), **flags),
            baseline_household_load_w=620.0,
        )

    assert build(spot_enabled=False, solar_configurable=True).status is PlanStatus.SPOT_DISABLED
    assert build(spot_enabled=True, solar_configurable=False).status is PlanStatus.SOLAR_NOT_CONFIGURED
    assert build(spot_enabled=True, solar_configurable=True).status is PlanStatus.OK


def test_finding_2_internal_price_gap_fails_closed() -> None:
    # A long covered run, then a one-slot interior gap: the honest price reach ends at the gap, so
    # the common horizon falls below MIN and the build fails closed rather than bridging the hole.
    head = _prices(1.0).intervals
    tail = _prices(1.0, start=NOW + SLOT * 5).intervals  # one 15-min slot missing after the head
    gapped = SpotPriceSnapshot(
        provider=SpotMarketProvider.ENERGY_CHARTS, bidding_zone="DE-LU", currency="EUR", fetched_at=NOW,
        intervals=head + tail,
    )
    result = _build(prices=gapped, solar=_solar(36.0), coverage_hours=36.0)
    assert result.status is PlanStatus.SPOT_PRICES_UNAVAILABLE
    assert result.plan is None


# --- finding 4: honest common horizon, trimmed and capped --------------------------------------


def test_finding_4_horizon_trims_to_common_coverage() -> None:
    # Prices reach +18 h, solar +40 h, consumption +30 h: the plan runs for the common 18 h.
    result = _build(prices=_prices(18.0), solar=_solar(40.0), coverage_hours=30.0)
    assert result.status is PlanStatus.OK
    assert result.horizon == timedelta(hours=18)
    assert result.plan is not None and result.plan.slots


def test_finding_4_capped_at_max_planning_horizon() -> None:
    # Every input reaches well past 36 h: the horizon is capped at MAX_PLANNING_HORIZON (36 h).
    result = _build(prices=_prices(48.0), solar=_solar(48.0), coverage_hours=48.0)
    assert result.status is PlanStatus.OK
    assert result.horizon == timedelta(hours=36)


def test_finding_4_below_min_horizon_reports_unavailable_with_cause() -> None:
    # Prices reach only 2 h: below MIN_PLANNING_HORIZON, fail closed naming the limiting input.
    result = _build(prices=_prices(2.0), solar=_solar(36.0), coverage_hours=36.0)
    assert result.status is PlanStatus.SPOT_PRICES_UNAVAILABLE
    assert result.plan is None
    assert "6 h" in (result.detail or "") and "spot prices" in (result.detail or "")


# --- finding 5: a single missing quarter-hour must fail closed, sub-second edges tolerated ------


def _drop_slot(snapshot: SpotPriceSnapshot, index: int) -> SpotPriceSnapshot:
    kept = tuple(iv for i, iv in enumerate(snapshot.intervals) if i != index)
    return SpotPriceSnapshot(
        provider=snapshot.provider, bidding_zone=snapshot.bidding_zone, currency=snapshot.currency,
        fetched_at=snapshot.fetched_at, intervals=kept,
    )


def _drop_solar_slot(forecast: SolarForecast, index: int) -> SolarForecast:
    kept = tuple(iv for i, iv in enumerate(forecast.intervals) if i != index)
    return SolarForecast(created_at=forecast.created_at, intervals=kept)


def test_finding_5_single_missing_quarter_hour_fails_closed() -> None:
    # One interior quarter-hour removed from an otherwise full price curve ends the honest reach
    # there; the common horizon drops below MIN and the plan fails closed (no zero-filled slot).
    gapped = _drop_slot(_prices(10.0), 20)  # drop the slot at +5 h
    result = _build(prices=gapped, solar=_solar(36.0), coverage_hours=36.0)
    assert result.status is PlanStatus.SPOT_PRICES_UNAVAILABLE
    assert result.plan is None


def test_finding_5_single_missing_quarter_hour_in_solar_fails_closed() -> None:
    gapped = _drop_solar_slot(_solar(10.0), 20)
    result = _build(prices=_prices(36.0), solar=gapped, coverage_hours=36.0)
    assert result.status is PlanStatus.SOLAR_FORECAST_UNAVAILABLE
    assert result.plan is None


def test_finding_5_trailing_quarter_hour_short_fails_closed() -> None:
    # Prices stop exactly one slot before MIN: a short tail is not planned over, it fails closed.
    short = _prices(5.75)  # 23 slots -> 5 h 45 min, one slot short of the 6 h minimum
    result = _build(prices=short, solar=_solar(36.0), coverage_hours=36.0)
    assert result.status is PlanStatus.SPOT_PRICES_UNAVAILABLE
    assert result.plan is None


def test_finding_5_second_level_rounding_tolerated() -> None:
    # A sub-second (0.5 s) gap between consecutive slots is pure timestamp rounding, not a missing
    # quarter-hour: it stays within _COVERAGE_TOLERANCE so the honest reach is unbroken and OK.
    base = _prices(10.0).intervals
    jittered = tuple(
        SpotPriceInterval(iv.start, iv.end - timedelta(milliseconds=500), iv.price_eur_per_kwh)
        for iv in base
    )
    snapshot = SpotPriceSnapshot(
        provider=SpotMarketProvider.ENERGY_CHARTS, bidding_zone="DE-LU", currency="EUR", fetched_at=NOW,
        intervals=jittered,
    )
    result = _build(prices=snapshot, solar=_solar(36.0), coverage_hours=36.0)
    assert result.status is PlanStatus.OK
    assert result.plan is not None


def test_finding_4_sub_second_late_leading_edge_is_tolerated() -> None:
    # Review finding 4: the leading-edge check must use the SAME one-second tolerance as the interior
    # gaps and the horizon end. A first interval that starts 500 ms after the anchor is a rounding
    # artefact, not a missing leading slot, so coverage must still start there.
    from app.optimizer.adapters import _coverage_end, _covers_horizon

    anchor = NOW
    late = tuple(
        SpotPriceInterval(
            anchor + timedelta(milliseconds=500) + SLOT * i,
            anchor + timedelta(milliseconds=500) + SLOT * (i + 1),
            Decimal("0.2"),
        )
        for i in range(40)
    )
    assert _covers_horizon(late, anchor, timedelta(hours=6)) is True
    assert _coverage_end(late, anchor) >= anchor + timedelta(hours=6)


def test_finding_4_a_full_slot_late_leading_edge_still_fails_closed() -> None:
    # The leading-edge tolerance is sub-second only: a first interval a full quarter-hour late leaves
    # the anchor's own slot uncovered and must still fail closed, never be bridged to zero.
    from app.optimizer.adapters import _covers_horizon

    anchor = NOW
    late = tuple(
        SpotPriceInterval(anchor + SLOT * (i + 1), anchor + SLOT * (i + 2), Decimal("0.2"))
        for i in range(40)
    )
    assert _covers_horizon(late, anchor, timedelta(hours=6)) is False


# --- consumption history affects preview quality rather than availability ----------------------


def test_sparse_horizon_coverage_uses_history_with_low_confidence() -> None:
    result = _build(prices=_prices(36.0), solar=_solar(36.0), coverage=0.2, coverage_hours=36.0, well_covered=100)
    assert result.status is PlanStatus.OK
    assert result.forecast_basis == "history_fallback"
    assert result.consumption_history_available is True
    assert result.forecast_confidence == "low"


def test_too_few_well_covered_buckets_use_history_with_low_confidence() -> None:
    result = _build(prices=_prices(36.0), solar=_solar(36.0), well_covered=1, coverage=1.0, coverage_hours=36.0)
    assert result.status is PlanStatus.OK
    assert result.forecast_basis == "history_fallback"
    assert result.forecast_confidence == "low"


def test_zero_history_uses_configured_baseline_and_marks_uncertainty() -> None:
    result = _build(prices=_prices(36.0), solar=_solar(36.0), well_covered=0, coverage=0.0)
    assert result.status is PlanStatus.OK
    assert result.forecast_basis == "bootstrap_baseline"
    assert result.consumption_history_available is False
    assert result.forecast_confidence == "low"
    assert "620 W" in result.plan.assumptions.forecast_basis
    assert result.plan.basis.consumption_wh[0][1] == pytest.approx(155.0)


def test_missing_battery_capacity_still_blocks_bootstrap_preview() -> None:
    class MissingBattery:
        def current(self):
            return _provider(usable_capacity_wh=None).current()

    result = build_dry_run_plan(
        now=NOW,
        battery_provider=MissingBattery(),
        consumption_provider=_ConsumptionProvider(well_covered=0, coverage=0.0),
        inputs=DryRunInputs(prices=_prices(36.0), solar=_solar(36.0), spot_enabled=True, solar_configurable=True),
        baseline_household_load_w=620.0,
    )
    assert result.status is PlanStatus.BATTERY_STATE_UNAVAILABLE
    assert result.plan is None


def test_battery_unavailable_public_detail_is_the_fixed_product_text() -> None:
    # Item 4c: the public dry-run detail must be the stable product sentence, never the internal
    # "not configured" exception wording (which describes only one of several causes).
    class MissingBattery:
        def current(self):
            return _provider(usable_capacity_wh=None).current()

    result = build_dry_run_plan(
        now=NOW,
        battery_provider=MissingBattery(),
        consumption_provider=_ConsumptionProvider(well_covered=0, coverage=0.0),
        inputs=DryRunInputs(prices=_prices(36.0), solar=_solar(36.0), spot_enabled=True, solar_configurable=True),
        baseline_household_load_w=620.0,
    )
    assert result.status is PlanStatus.BATTERY_STATE_UNAVAILABLE
    assert result.detail == "Usable battery capacity could not be verified for this inverter."
    assert "not configured" not in (result.detail or "")


def test_half_horizon_coverage_is_usable_history() -> None:
    result = _build(prices=_prices(36.0), solar=_solar(36.0), well_covered=50, coverage=0.5)
    assert result.status is PlanStatus.OK
    assert result.forecast_basis == "adaptive_history"
    assert result.forecast_confidence == "normal"


def test_unlearned_adaptive_profile_stays_low_confidence_despite_slot_coverage() -> None:
    class LearningConsumption(_ConsumptionProvider):
        def analysis(self, now: datetime):
            return SimpleNamespace(method="median-fallback")

    result = build_dry_run_plan(
        now=NOW,
        battery_provider=_BatteryProvider(),
        consumption_provider=LearningConsumption(well_covered=50, coverage=1.0),
        inputs=DryRunInputs(prices=_prices(36.0), solar=_solar(36.0), spot_enabled=True, solar_configurable=True),
        baseline_household_load_w=620.0,
    )
    assert result.status is PlanStatus.OK
    assert result.forecast_basis == "history_fallback"
    assert result.forecast_confidence == "low"


# --- orchestration happy path (review finding 8: end-to-end plan from local data) --------------


def test_sufficient_local_data_produces_a_dry_run_plan() -> None:
    result = _build(prices=_prices(36.0), solar=_solar(36.0), well_covered=50, coverage=1.0, coverage_hours=36.0)
    assert result.status is PlanStatus.OK
    assert result.plan is not None
    assert result.plan.slots  # a plan was actually assembled
    assert result.window.prices_from is not None and result.window.consumption_until is not None
    assert result.forecast_basis == "adaptive_history"
    assert result.consumption_history_available is True
    assert result.forecast_confidence == "normal"


# --- P2-1/P2-2/P3-1 through the REAL HistoricalConsumptionForecaster (not the fake provider) ----

from app.optimizer.consumption import HistoricalConsumptionForecaster, HistoryBucket
from app.optimizer.planner import planning_start


class _RealConsumptionProvider:
    """A consumption provider backed by the real forecaster over a synthetic in-memory history.

    It delegates every method the adapter uses to the real
    :class:`HistoricalConsumptionForecaster`, so the finding-4 trim path, the finding-6 ratio gate
    and the finding-2 start anchoring are exercised against the actual code, not a stub.
    """

    def __init__(self, history: list[HistoryBucket], *, timezone: str = "UTC") -> None:
        self._history = history
        self._forecaster = HistoricalConsumptionForecaster(timezone)

    def well_covered_count(self, now: datetime) -> int:
        return sum(1 for b in self._history if b.well_covered)

    def horizon_coverage_ratio(self, now: datetime, horizon: timedelta) -> float:
        return self._forecaster.horizon_coverage_ratio(self._history, now, horizon)

    def coverage_end(self, now: datetime, horizon: timedelta) -> datetime:
        return self._forecaster.coverage_end(self._history, now, horizon)

    def forecast(self, now: datetime, horizon: timedelta):
        return self._forecaster.build(self._history, now, horizon)


def _dense_history(anchor: datetime, span_hours: float, *, days: int = 5, skip=None) -> list[HistoryBucket]:
    """Well-covered same-time-of-day buckets backing every slot of ``[anchor, anchor+span)``."""
    skip = skip or set()
    history: list[HistoryBucket] = []
    for i in range(int(span_hours * 4)):
        slot_start = anchor + SLOT * i
        if slot_start in skip:
            continue
        for day in range(1, days + 1):
            start = slot_start - timedelta(days=day)
            history.append(HistoryBucket(start, start + SLOT, 250.0, sample_count=10, coverage_seconds=900.0))
    return history


def _build_real(*, now, prices, solar, consumption_provider, horizon=timedelta(hours=36)):
    return build_dry_run_plan(
        now=now,
        battery_provider=_BatteryProvider(),
        consumption_provider=consumption_provider,
        inputs=DryRunInputs(prices=prices, solar=solar, spot_enabled=True, solar_configurable=True),
        horizon=horizon,
        baseline_household_load_w=620.0,
    )


def test_one_measured_bucket_uses_median_instead_of_configured_baseline() -> None:
    history = [HistoryBucket(NOW - timedelta(days=1), NOW - timedelta(days=1) + SLOT, 250.0, 10, 900.0)]
    result = _build_real(
        now=NOW,
        prices=_prices(36.0),
        solar=_solar(36.0),
        consumption_provider=_RealConsumptionProvider(history),
    )
    assert result.status is PlanStatus.OK
    assert result.forecast_basis == "history_fallback"
    assert result.forecast_confidence == "low"
    assert result.plan.basis.consumption_wh[0][1] == pytest.approx(250.0)


def test_p2_1_single_early_consumption_gap_still_plans_via_fallback() -> None:
    # 36 h of prices/solar, consumption per-slot-backed everywhere except one quarter hour at +4 h.
    # The old coverage_end trim would cut the horizon to ~4 h -> below MIN -> insufficient_history.
    # With the ratio-based gate the single gap is filled by the whole-history median and the plan
    # runs the full span.
    gap = NOW + timedelta(hours=4)
    history = _dense_history(NOW, 36.0, skip={gap})
    provider = _RealConsumptionProvider(history)
    result = _build_real(now=NOW, prices=_prices(36.0), solar=_solar(36.0), consumption_provider=provider)
    assert result.status is PlanStatus.OK
    assert result.plan is not None and result.plan.slots
    assert result.horizon == timedelta(hours=36)


def test_p2_2_mid_slot_now_does_not_let_the_unused_slot_block_the_plan() -> None:
    # now = 12:07; the planner starts at 12:15 and never uses 12:00. Consumption is backed only from
    # 12:15 on. Anchoring coverage at the planner's start (not now) must let the plan proceed.
    now = datetime(2026, 1, 15, 12, 7, tzinfo=UTC)
    start = planning_start(now)
    assert start == datetime(2026, 1, 15, 12, 15, tzinfo=UTC)
    history = _dense_history(start, 24.0)  # backed from 12:15 on, nothing at 12:00
    provider = _RealConsumptionProvider(history)
    # Prices/solar reach 24 h from start; aligned to the quarter-hour grid they cover the span.
    result = _build_real(
        now=now,
        prices=_prices(24.0, start=start),
        solar=_solar(24.0, start=start),
        consumption_provider=provider,
    )
    assert result.status is PlanStatus.OK
    assert result.plan is not None and result.plan.slots


def test_p3_1_sub_second_shortfall_at_the_six_hour_boundary_still_plans() -> None:
    # Prices/solar end 500 ms short of exactly 6 h from start. The plannable span is 6 h minus half
    # a second; the MIN comparison carries _COVERAGE_TOLERANCE so this sub-second shortfall does not
    # flip the decision to refused.
    short_end = NOW + timedelta(hours=6) - timedelta(milliseconds=500)
    base_prices = _prices(7.0)
    base_solar = _solar(7.0)
    prices = SpotPriceSnapshot(
        provider=base_prices.provider, bidding_zone=base_prices.bidding_zone, currency=base_prices.currency,
        fetched_at=base_prices.fetched_at,
        intervals=tuple(
            SpotPriceInterval(iv.start, min(iv.end, short_end), iv.price_eur_per_kwh)
            for iv in base_prices.intervals if iv.start < short_end
        ),
    )
    solar = SolarForecast(
        created_at=base_solar.created_at,
        intervals=tuple(
            SolarForecastInterval(iv.start, min(iv.end, short_end), iv.energy_wh)
            for iv in base_solar.intervals if iv.start < short_end
        ),
    )
    history = _dense_history(NOW, 7.0)
    provider = _RealConsumptionProvider(history)
    result = _build_real(now=NOW, prices=prices, solar=solar, consumption_provider=provider)
    assert result.status is PlanStatus.OK
    assert result.plan is not None


def test_dry_run_and_runtime_policy_share_the_forecast_enums() -> None:
    from app.optimizer import adapters, models

    assert adapters.ForecastBasis is models.ForecastBasis
    assert adapters.ForecastConfidence is models.ForecastConfidence
    assert [m.value for m in models.ForecastBasis] == ["bootstrap_baseline", "history_fallback", "adaptive_history"]
    assert [m.value for m in models.ForecastConfidence] == ["low", "normal"]
    assert isinstance(_build(prices=_prices(36.0), solar=_solar(36.0), well_covered=0).forecast_basis, models.ForecastBasis)
