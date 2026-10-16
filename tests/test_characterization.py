#!/usr/bin/env python3
#
# tests/test_characterization.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Read-only Charge Characterization: the vendor-neutral sample contract and the RCT adapter.

Three properties are pinned. The sample never fabricates a field — an absent, non-numeric or
unverified register stays explicitly ``None`` AND is named in ``unavailable_fields`` (so None is
distinguishable from "not available"). Per-field cache ages are transported (not collapsed onto one
synchronous timestamp), with ``max_sample_age_seconds`` exposing the skew. And the raw cumulative
energy counters are recorded VERBATIM: nothing here differences them or computes any efficiency.
"""

import math
from datetime import UTC, datetime

import pytest

from app.dispatch.capabilities import (
    CapabilityName,
    CapabilityRecord,
    CapabilityRegistry,
)
from app.energy.characterization import MEASURED_FIELDS, CharacterizationSample, MeasuredValue
from app.gateway.cache import CacheFreshness
from app.gateway.characterization_readings import RctCharacterizationReadings
from tests.conftest import ManualClock
from tests.test_dispatch_core import verified_registry

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# Contract


def consistent_sample(**values: MeasuredValue | None) -> CharacterizationSample:
    """Build a sample whose unavailable_fields names exactly the fields left absent, so the
    bijection holds and a test can focus on just the values it passes.
    """
    unavailable = frozenset(name for name in MEASURED_FIELDS if values.get(name) is None)
    return CharacterizationSample(timestamp=NOW, unavailable_fields=unavailable, **values)


def test_timestamp_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError):
        CharacterizationSample(
            timestamp=datetime(2026, 1, 1, 12, 0),  # noqa: DTZ001
            unavailable_fields=frozenset(MEASURED_FIELDS),
        )


def test_measured_value_must_be_finite_with_a_non_negative_age() -> None:
    with pytest.raises(ValueError):
        MeasuredValue(math.nan)
    with pytest.raises(ValueError):
        MeasuredValue(10.0, -1.0)


def test_unavailable_fields_must_name_known_fields_and_not_contradict_a_value() -> None:
    with pytest.raises(ValueError):
        CharacterizationSample(
            timestamp=NOW,
            unavailable_fields=frozenset(MEASURED_FIELDS) | {"not_a_field"},
        )
    with pytest.raises(ValueError):
        # grid_power_w is populated AND flagged unavailable — contradictory.
        CharacterizationSample(
            timestamp=NOW,
            grid_power_w=MeasuredValue(1200.0, 1.0),
            unavailable_fields=frozenset(MEASURED_FIELDS),
        )


def test_max_sample_age_is_the_oldest_contributing_cache_age() -> None:
    sample = consistent_sample(
        grid_power_w=MeasuredValue(1200.0, 0.1),
        battery_soc=MeasuredValue(50.0, 8.0),
        requested_power_w=MeasuredValue(4000.0),  # caller value, no age
    )
    assert sample.max_sample_age_seconds == 8.0


def test_max_sample_age_is_none_when_only_caller_values_are_present() -> None:
    sample = consistent_sample(requested_power_w=MeasuredValue(4000.0))
    assert sample.max_sample_age_seconds is None


# RCT adapter


class CountingGateway:
    """Answers from a cache and refuses any device transaction (a characterization read must not
    cost the device budget — same rule as the energy-readings adapter).
    """

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self._values: dict[tuple[str, str], tuple[object, float, CacheFreshness]] = {}

    def put(self, name: str, value, *, age: float = 1.0, device_id: str = "main") -> None:
        self._values[(device_id, name)] = (value, age, CacheFreshness.FRESH)

    def cached_sample(self, device_id: str, name: str):
        return self._values.get((device_id, name))

    async def read_system(self, device_id: str, name: str):
        raise AssertionError("the characterization adapter must not read the device")


def adapter(rct: CountingGateway, *, sign_verified: bool = True) -> RctCharacterizationReadings:
    if sign_verified:
        # Reuse the shared verified-device fixture (an operator-attested device) rather than
        # asserting a verification in this file (AC-20).
        registry = verified_registry("main")
    else:
        # Unverified sign capabilities carry only the assumption defaults — not a go-ahead.
        registry = CapabilityRegistry(
            [
                CapabilityRecord(device_id="main", name=CapabilityName.GRID_POWER_SIGN, grid_import_positive=True),
                CapabilityRecord(
                    device_id="main", name=CapabilityName.BATTERY_POWER_SIGN, battery_discharge_positive=True
                ),
            ]
        )
    return RctCharacterizationReadings(rct, capabilities=registry)  # type: ignore[arg-type]


def test_the_sample_contract_enforces_the_availability_bijection() -> None:
    """Finding 3: a field is None IFF its name is in unavailable_fields, both directions."""
    # Absent but NOT flagged is rejected.
    with pytest.raises(ValueError):
        CharacterizationSample(timestamp=NOW, unavailable_fields=frozenset())  # all fields None
    # Flagged-and-absent for every field is the one consistent all-absent sample.
    everything_absent = CharacterizationSample(timestamp=NOW, unavailable_fields=frozenset(MEASURED_FIELDS))
    assert everything_absent.grid_power_w is None


def test_a_populated_cache_fills_verified_fields_and_records_counters_verbatim() -> None:
    rct = CountingGateway(ManualClock())
    rct.put("battery_soc", 0.54, age=2.0)
    rct.put("grid_power", 1200.0, age=0.5)
    rct.put("household_load_power", 800.0, age=0.5)
    rct.put("battery_power", -2000.0, age=0.5)  # discharge_positive convention: negative = charge
    # These four carry unverified catalog semantics; even cached, the adapter must leave them absent.
    rct.put("battery_voltage", 51.2, age=0.5)
    rct.put("battery_current", 39.1, age=0.5)
    rct.put("battery_maximum_charge_current", 50.0, age=0.5)
    rct.put("battery_temperature", 24.5, age=0.5)
    rct.put("energy_grid_load_total", 654321.0, age=3.0)
    rct.put("energy_e_load_total", 123456.0, age=3.0)
    rct.put("battery_stored_energy", 9600.0, age=3.0)

    sample = adapter(rct).sample("main", timestamp=NOW, requested_power_w=4000.0, commanded_power_w=3000.0)

    assert sample.requested_power_w == MeasuredValue(4000.0)
    assert sample.commanded_power_w == MeasuredValue(3000.0)
    assert sample.battery_soc.value == pytest.approx(54.0)
    assert sample.battery_soc.age_seconds == 2.0
    # Verified sign capability -> business-normalized power is populated.
    assert sample.grid_power_w.value == pytest.approx(1200.0)
    assert sample.house_load_w.value == pytest.approx(800.0)
    assert sample.measured_battery_power_w.value == pytest.approx(-2000.0)  # charging is negative
    # Unverified-semantics fields are always absent and flagged, even when cached.
    for name in ("battery_voltage", "battery_current", "maximum_charge_current", "battery_temperature"):
        assert getattr(sample, name) is None
        assert name in sample.unavailable_fields
    # The counters are recorded exactly as read — no differencing, no efficiency.
    assert sample.grid_import_energy_total_raw_wh.value == 654321.0
    assert sample.load_energy_total_raw_wh.value == 123456.0
    assert sample.battery_stored_energy_raw_wh.value == 9600.0
    assert sample.max_sample_age_seconds == 3.0


def test_sign_normalized_power_is_absent_when_the_sign_capability_is_unverified() -> None:
    """Finding 2: the sign-convention defaults are assumptions; unverified is not a go-ahead."""
    rct = CountingGateway(ManualClock())
    rct.put("grid_power", 1200.0)
    rct.put("battery_power", -2000.0)
    sample = adapter(rct, sign_verified=False).sample("main", timestamp=NOW)
    assert sample.grid_power_w is None
    assert sample.measured_battery_power_w is None
    assert "grid_power_w" in sample.unavailable_fields
    assert "measured_battery_power_w" in sample.unavailable_fields


def test_absent_registers_are_none_and_flagged() -> None:
    rct = CountingGateway(ManualClock())
    rct.put("battery_soc", 0.5)  # only SoC is cached
    sample = adapter(rct).sample("main", timestamp=NOW)
    assert sample.battery_soc.value == pytest.approx(50.0)
    assert sample.battery_stored_energy_raw_wh is None
    # None is distinguishable from populated: the absent fields are named explicitly.
    assert "grid_import_energy_total_raw_wh" in sample.unavailable_fields
    assert "battery_stored_energy_raw_wh" in sample.unavailable_fields
    assert "battery_soc" not in sample.unavailable_fields


def test_non_finite_and_non_numeric_cache_values_are_none_and_flagged_without_raising() -> None:
    rct = CountingGateway(ManualClock())
    rct.put("grid_power", math.nan)
    rct.put("energy_e_load_total", "oops")
    rct.put("household_load_power", True)  # a bool is not a measurement
    sample = adapter(rct).sample("main", timestamp=NOW)
    assert sample.grid_power_w is None
    assert sample.load_energy_total_raw_wh is None
    assert sample.house_load_w is None
    assert {"grid_power_w", "load_energy_total_raw_wh", "house_load_w"} <= sample.unavailable_fields


def test_a_non_numeric_caller_value_becomes_absent_rather_than_raising() -> None:
    rct = CountingGateway(ManualClock())
    sample = adapter(rct).sample("main", timestamp=NOW, requested_power_w=math.inf)
    assert sample.requested_power_w is None
    assert "requested_power_w" in sample.unavailable_fields


def test_an_empty_cache_and_a_missing_device_never_raise() -> None:
    rct = CountingGateway(ManualClock())
    empty = adapter(rct).sample("main", timestamp=NOW)
    assert empty.grid_power_w is None
    assert empty.max_sample_age_seconds is None
    missing = adapter(rct).sample("no-such-device", timestamp=NOW)
    assert missing.battery_soc is None
    assert missing.load_energy_total_raw_wh is None
    # With no caller values either, every measured field is unavailable.
    assert len(missing.unavailable_fields) == len(empty.unavailable_fields)


@pytest.mark.parametrize(("raw", "expected"), [(1500.0, 1500.0), (-1500.0, -1500.0), (0.0, 0.0)])
def test_verified_power_follows_the_business_sign_convention(raw: float, expected: float) -> None:
    rct = CountingGateway(ManualClock())
    rct.put("battery_power", raw)
    rct.put("grid_power", raw)
    sample = adapter(rct).sample("main", timestamp=NOW)
    assert sample.measured_battery_power_w.value == pytest.approx(expected)
    assert sample.grid_power_w.value == pytest.approx(expected)


@pytest.mark.parametrize("bad", [True, math.inf, -math.inf, math.nan, "0.5"])
def test_an_unusable_soc_cache_value_is_absent(bad: object) -> None:
    rct = CountingGateway(ManualClock())
    rct.put("battery_soc", bad)
    sample = adapter(rct).sample("main", timestamp=NOW)
    assert sample.battery_soc is None
    assert "battery_soc" in sample.unavailable_fields
