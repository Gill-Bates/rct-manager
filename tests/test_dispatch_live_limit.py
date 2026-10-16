#!/usr/bin/env python3
#
# tests/test_dispatch_live_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Live Charge Envelope: the ChargePowerLimit contract, freshness, the charge clamp and the
immediate-derate safety write. Vendor-neutral — no RCT register is exercised here.
"""

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.dispatch.gating import should_write
from app.dispatch.live_limit import (
    ChargePowerLimit,
    NullLiveChargeLimitProvider,
)
from app.dispatch.models import (
    DispatchConfig,
    PowerDirection,
    PowerSetpoint,
)
from app.dispatch.strategy import effective_charge_limit_w

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# ChargePowerLimit contract


def test_charge_power_limit_rejects_non_positive_and_non_finite_value() -> None:
    for bad in (0.0, -1.0, math.nan, math.inf):
        with pytest.raises(ValueError):
            ChargePowerLimit(bad, NOW, "test")


def test_charge_power_limit_rejects_naive_timestamp_and_empty_source() -> None:
    with pytest.raises(ValueError):
        ChargePowerLimit(1000.0, datetime(2026, 1, 1, 12, 0), "test")  # noqa: DTZ001
    with pytest.raises(ValueError):
        ChargePowerLimit(1000.0, NOW, "")


# Freshness


def test_is_fresh_at_the_age_boundary_and_beyond() -> None:
    limit = ChargePowerLimit(2000.0, NOW, "test")
    assert limit.is_fresh(NOW, 5.0)  # age 0
    assert limit.is_fresh(NOW + timedelta(seconds=5), 5.0)  # exactly max age
    assert not limit.is_fresh(NOW + timedelta(seconds=5.001), 5.0)  # over max age


def test_a_future_measurement_is_not_fresh() -> None:
    """A negative age (clock disagreement) must not read as a brand new reading."""
    limit = ChargePowerLimit(2000.0, NOW, "test")
    assert not limit.is_fresh(NOW - timedelta(seconds=1), 5.0)


def test_null_provider_never_emits_a_limit() -> None:
    assert NullLiveChargeLimitProvider().live_charge_limit("main") is None


# Pure effective-charge-limit clamp


def test_live_limit_below_the_static_bound_clamps_down() -> None:
    assert effective_charge_limit_w(3000, 1800) == 1800


def test_live_limit_above_the_static_bound_is_ignored() -> None:
    assert effective_charge_limit_w(3000, 9000) == 3000


def test_absent_live_limit_falls_back_to_the_static_bound() -> None:
    # None is never invented into a number — the durable bound stays in force.
    assert effective_charge_limit_w(3000, None) == 3000


# Charge-derate writes


def test_charge_derate_bypasses_deadband_and_interval_for_a_reduction() -> None:
    config = DispatchConfig()
    current = PowerSetpoint(PowerDirection.CHARGE, 3000)
    desired = PowerSetpoint(PowerDirection.CHARGE, 2900)  # 100 W drop, inside the 200 W deadband
    kwargs = {"now": NOW, "last_write_at": NOW, "config": config}
    # Without the derate flag the small reduction is suppressed by the deadband.
    assert not should_write(desired, current, **kwargs)
    # A safety derate writes immediately regardless of deadband/interval.
    assert should_write(desired, current, charge_derate=True, **kwargs)


def test_charge_increase_still_obeys_the_deadband_even_with_the_derate_flag() -> None:
    config = DispatchConfig()
    current = PowerSetpoint(PowerDirection.CHARGE, 2900)
    desired = PowerSetpoint(PowerDirection.CHARGE, 3000)  # 100 W increase, inside the deadband
    kwargs = {"now": NOW, "last_write_at": NOW, "config": config}
    assert not should_write(desired, current, charge_derate=True, **kwargs)


def test_charge_derate_flag_false_behaves_like_before() -> None:
    config = DispatchConfig()
    current = PowerSetpoint(PowerDirection.CHARGE, 3000)
    desired = PowerSetpoint(PowerDirection.CHARGE, 2500)  # beyond the deadband
    # last_write_at None -> first write path is allowed, as for any other setpoint.
    assert should_write(desired, current, now=NOW, last_write_at=None, config=config)
