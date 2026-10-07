#!/usr/bin/env python3
#
# tests/test_energy_readings.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The Energy Manager's readings: one cache lookup per figure, never a device transaction.

Two properties are pinned here because the card polls: the adapter reads the cache only, and it
never raises — an absent, expired or non-numeric value becomes ``null`` with ``stale=true`` instead
of an exception that would take the whole status GET down with it.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.allowlist import Allowlist
from app.cache import CacheFreshness, MemoryCache
from app.catalog.registry import RegistryCatalog
from app.dispatch.capabilities import (
    CapabilityName,
    CapabilityRecord,
    CapabilityRegistry,
)
from app.gateway.energy_readings import RctEnergyReadings
from app.gateway.rct import RctGateway
from tests.conftest import ManualClock

CATALOG = RegistryCatalog.from_file(
    Path(__file__).resolve().parents[1] / "app" / "catalog" / "objects.json"
)
TTL = 30.0
GRACE = 30.0


def gateway(clock: ManualClock) -> tuple[RctGateway, MemoryCache]:
    cache = MemoryCache(TTL, GRACE)
    return RctGateway(CATALOG, cache, clock, Allowlist({}, CATALOG)), cache


def seed(cache: MemoryCache, clock: ManualClock, name: str, value, *, device_id: str = "main") -> None:
    cache.put(
        (device_id, name),
        value,
        measured_at=clock.now(),
        received_monotonic=clock.monotonic(),
        origin="transaction",
    )


# --- RctGateway.cached_sample ---------------------------------------------------------------------


def test_a_fresh_entry_is_returned_with_its_age_and_freshness() -> None:
    clock = ManualClock()
    rct, cache = gateway(clock)
    seed(cache, clock, "battery_soc", 0.5)
    clock.advance(4.0)
    value, age, freshness = rct.cached_sample("main", "battery_soc")
    assert value == 0.5
    assert age == pytest.approx(4.0)
    assert freshness is CacheFreshness.FRESH


def test_an_entry_inside_the_grace_window_is_returned_as_grace() -> None:
    clock = ManualClock()
    rct, cache = gateway(clock)
    seed(cache, clock, "battery_soc", 0.5)
    clock.advance(TTL + 1.0)
    value, age, freshness = rct.cached_sample("main", "battery_soc")
    assert value == 0.5
    assert age == pytest.approx(TTL + 1.0)
    assert freshness is CacheFreshness.GRACE


def test_an_expired_entry_and_an_absent_one_are_both_none() -> None:
    """The age of a value we refuse to show is not published at all."""
    clock = ManualClock()
    rct, cache = gateway(clock)
    seed(cache, clock, "battery_soc", 0.5)
    clock.advance(TTL + GRACE + 1.0)
    assert rct.cached_sample("main", "battery_soc") is None
    assert rct.cached_sample("main", "grid_power") is None
    assert rct.cached_sample("unknown-device", "battery_soc") is None


def test_cached_reading_keeps_its_own_shape() -> None:
    """The additive method must not have changed the one app/admin/api.py uses."""
    clock = ManualClock()
    rct, cache = gateway(clock)
    seed(cache, clock, "battery_soc", 0.5)
    assert rct.cached_reading("main", "battery_soc") == (0.5, CacheFreshness.FRESH)


# --- RctEnergyReadings ---------------------------------------------------------------------------


class CountingGateway:
    """A gateway that answers from a cache and records every device transaction it is asked for.

    The point of the fake is the counter: a status GET must not cost transactions, no matter how
    many browsers poll it.
    """

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.transactions: list[tuple[str, str]] = []
        self._values: dict[tuple[str, str], tuple[object, float, CacheFreshness]] = {}

    def put(self, name: str, value, *, age: float = 1.0, freshness=CacheFreshness.FRESH) -> None:
        self._values[("main", name)] = (value, age, freshness)

    def cached_sample(self, device_id: str, name: str):
        return self._values.get((device_id, name))

    async def read_system(self, device_id: str, name: str):
        self.transactions.append((device_id, name))
        raise AssertionError("the readings adapter must not read the device")


def readings(rct: CountingGateway, *, grid_import_positive: bool = True) -> RctEnergyReadings:
    registry = CapabilityRegistry(
        [
            CapabilityRecord(
                device_id="main",
                name=CapabilityName.GRID_POWER_SIGN,
                grid_import_positive=grid_import_positive,
            )
        ]
    )
    return RctEnergyReadings(rct, capabilities=registry)  # type: ignore[arg-type]


def test_a_soc_ratio_becomes_a_percentage() -> None:
    rct = CountingGateway(ManualClock())
    rct.put("battery_soc", 0.54, age=2.0)
    result = readings(rct).readings("main")
    assert result.battery_soc_percent.value == pytest.approx(54.0)
    assert result.battery_soc_percent.age_seconds == 2.0
    assert result.battery_soc_percent.stale is False


def test_a_soc_already_in_percent_is_left_alone() -> None:
    """Same `value <= 1.5` rule the dispatch adapter uses, so the two cannot disagree on the unit."""
    rct = CountingGateway(ManualClock())
    rct.put("battery_soc", 54.0)
    assert readings(rct).readings("main").battery_soc_percent.value == pytest.approx(54.0)


@pytest.mark.parametrize(
    ("import_positive", "expected"), [(True, 1200.0), (False, -1200.0)]
)
def test_the_grid_figure_follows_the_devices_sign_convention(
    import_positive: bool, expected: float
) -> None:
    rct = CountingGateway(ManualClock())
    rct.put("grid_power", 1200.0)
    result = readings(rct, grid_import_positive=import_positive).readings("main")
    assert result.grid_power_w.value == pytest.approx(expected)


def test_the_pv_figure_is_the_sum_of_both_strings() -> None:
    rct = CountingGateway(ManualClock())
    rct.put("solar_a_power", 1500.0, age=1.0)
    rct.put("solar_b_power", 900.0, age=3.0)
    result = readings(rct).readings("main")
    assert result.pv_power_w.value == pytest.approx(2400.0)
    assert result.pv_power_w.age_seconds == 3.0  # the older of the two says how much to trust it
    assert result.pv_power_w.stale is False


def test_one_missing_string_still_yields_a_sum_but_marks_it_stale() -> None:
    rct = CountingGateway(ManualClock())
    rct.put("solar_a_power", 1500.0)
    result = readings(rct).readings("main")
    assert result.pv_power_w.value == pytest.approx(1500.0)
    assert result.pv_power_w.stale is True


def test_a_grace_value_is_published_with_its_age_and_marked_stale() -> None:
    rct = CountingGateway(ManualClock())
    rct.put("household_load_power", 800.0, age=41.0, freshness=CacheFreshness.GRACE)
    result = readings(rct).readings("main")
    assert result.house_load_w.value == pytest.approx(800.0)
    assert result.house_load_w.age_seconds == 41.0
    assert result.house_load_w.stale is True


def test_an_entirely_empty_cache_reads_as_four_absent_figures() -> None:
    result = readings(CountingGateway(ManualClock())).readings("main")
    for reading in (
        result.battery_soc_percent,
        result.grid_power_w,
        result.pv_power_w,
        result.house_load_w,
    ):
        assert reading.value is None and reading.age_seconds is None and reading.stale is True


def test_an_unknown_device_and_a_non_numeric_value_do_not_raise() -> None:
    rct = CountingGateway(ManualClock())
    rct.put("battery_soc", "n/a")
    adapter = readings(rct)
    assert adapter.readings("slave1").battery_soc_percent.value is None
    assert adapter.readings("main").battery_soc_percent.value is None


def test_reading_the_card_costs_no_device_transaction() -> None:
    rct = CountingGateway(ManualClock())
    rct.put("battery_soc", 0.5)
    rct.put("grid_power", 1200.0)
    rct.put("solar_a_power", 1500.0)
    rct.put("solar_b_power", 900.0)
    rct.put("household_load_power", 800.0)
    adapter = readings(rct)
    for _ in range(5):
        adapter.readings("main")
    assert rct.transactions == []


def test_an_empty_capability_registry_uses_the_shipped_assumption() -> None:
    """Without dispatch there is no registry, and the adapter must still answer: the assumption
    default (import positive) is the same one the dispatch adapter works with.
    """
    rct = CountingGateway(ManualClock())
    rct.put("grid_power", 1200.0)
    adapter = RctEnergyReadings(rct, capabilities=CapabilityRegistry())  # type: ignore[arg-type]
    assert adapter.readings("main").grid_power_w.value == pytest.approx(1200.0)


def test_the_measured_at_axis_is_not_used_for_the_age() -> None:
    """The age comes from the monotonic axis the cache records, never from wall time."""
    clock = ManualClock()
    rct, cache = gateway(clock)
    cache.put(
        ("main", "battery_soc"),
        0.5,
        measured_at=datetime(2020, 1, 1, tzinfo=UTC),
        received_monotonic=clock.monotonic(),
        origin="transaction",
    )
    clock.advance(2.0)
    sample = rct.cached_sample("main", "battery_soc")
    assert sample is not None and sample[1] == pytest.approx(2.0)
