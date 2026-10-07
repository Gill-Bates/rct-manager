#!/usr/bin/env python3
#
# tests/test_soc_target_policy.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The business stop goal and the device-level SoC target are two different numbers.

The policy says which rule derives the second from the first (vendor-neutral,
``app/dispatch/soc_policy.py``); the rule lives in the RCT adapter
(``RctSocTargetConvention``) and nothing above the adapter ever computes a register value.
"""

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.dispatch.capabilities import CapabilityRegistry
from app.dispatch.models import DeviceControlSnapshot, DispatchMode, PowerSetpoint
from app.dispatch.soc_policy import (
    NOTE_MAX_LENGTH,
    SocTargetMode,
    SocTargetPolicy,
    SocTargetPolicyRegistry,
)
from app.errors import DeviceApiError
from app.gateway.base import WriteOutcome
from app.gateway.conventions import RctSocTargetConvention
from app.gateway.rct_dispatch import RctDispatchGateway

# --- the registry ------------------------------------------------------------------------------


def test_registry_returns_the_shipped_business_target_default_for_an_unknown_device() -> None:
    """Nothing stored means today's behaviour, not an invented accommodation."""
    registry = SocTargetPolicyRegistry()
    policy = registry.policy("main")
    assert policy == SocTargetPolicy(device_id="main")
    assert policy.mode is SocTargetMode.BUSINESS_TARGET
    assert policy.below_margin_percent == 5.0


def test_registry_is_built_from_the_stores_mapping_and_stays_per_device() -> None:
    stored = SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC, below_margin_percent=7.5)
    registry = SocTargetPolicyRegistry({"main": stored})
    assert registry.policy("main") is stored
    # A policy of device A never applies to device B.
    assert registry.policy("slave1").mode is SocTargetMode.BUSINESS_TARGET


def test_registry_replace_takes_a_policy_over() -> None:
    registry = SocTargetPolicyRegistry()
    replacement = SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC, note="candidate only")
    registry.replace(replacement)
    assert registry.policy("main") is replacement
    registry.replace(SocTargetPolicy("main"))
    assert registry.policy("main").mode is SocTargetMode.BUSINESS_TARGET


def test_policy_note_is_bounded_printable_ascii() -> None:
    SocTargetPolicy("main", note="x" * NOTE_MAX_LENGTH)
    with pytest.raises(ValueError, match="at most"):
        SocTargetPolicy("main", note="x" * (NOTE_MAX_LENGTH + 1))
    with pytest.raises(ValueError, match="printable ASCII"):
        SocTargetPolicy("main", note="charge\nverified")
    with pytest.raises(ValueError, match="printable ASCII"):
        SocTargetPolicy("main", note="Ladeziel 80 %\u00a0")


# --- the derivation (design test plan item 3: AC-14, AC-15) ------------------------------------


@given(target=st.floats(min_value=0, max_value=100), soc=st.floats(min_value=0, max_value=100))
def test_business_target_reproduces_the_stop_goal_exactly(target: float, soc: float) -> None:
    """AC-14: the shipped policy is bit-identical to what the service has always written."""
    convention = RctSocTargetConvention()
    for mode in (DispatchMode.CHARGE_FROM_GRID, DispatchMode.DISCHARGE_TO_LOAD):
        assert convention.register_ratio(
            mode, stop_target_percent=target, soc_percent=soc
        ) == target / 100.0


@given(soc=st.floats(min_value=0, max_value=100), margin=st.floats(min_value=0, max_value=50))
def test_below_current_soc_charge_derives_from_the_measured_soc(soc: float, margin: float) -> None:
    """AC-15: the charge direction follows the measured SoC, clamped into [0, 1]."""
    ratio = RctSocTargetConvention(
        SocTargetMode.BELOW_CURRENT_SOC, below_margin_percent=margin
    ).register_ratio(DispatchMode.CHARGE_FROM_GRID, stop_target_percent=80.0, soc_percent=soc)
    assert ratio == pytest.approx(max(soc - margin, 0.0) / 100.0)
    assert 0.0 <= ratio <= 1.0


def test_below_current_soc_clamps_at_the_floor_and_leaves_discharge_alone() -> None:
    convention = RctSocTargetConvention(SocTargetMode.BELOW_CURRENT_SOC, below_margin_percent=5.0)
    assert convention.register_ratio(
        DispatchMode.CHARGE_FROM_GRID, stop_target_percent=80.0, soc_percent=2.0
    ) == 0.0
    # No claim exists for the discharge direction, so nothing is invented there.
    assert convention.register_ratio(
        DispatchMode.DISCHARGE_TO_LOAD, stop_target_percent=30.0, soc_percent=90.0
    ) == 0.3


@pytest.mark.parametrize("mode", [SocTargetMode.BUSINESS_TARGET, SocTargetMode.BELOW_CURRENT_SOC])
@pytest.mark.parametrize("dispatch_mode", [DispatchMode.HOLD, DispatchMode.EXPORT_TO_GRID])
def test_a_mode_without_a_soc_target_register_value_raises(
    mode: SocTargetMode, dispatch_mode: DispatchMode
) -> None:
    """HOLD omits the step and EXPORT_TO_GRID never reaches the adapter: no silent fallback."""
    with pytest.raises(ValueError, match="no SoC target register value"):
        RctSocTargetConvention(mode).register_ratio(
            dispatch_mode, stop_target_percent=80.0, soc_percent=50.0
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_input_raises_instead_of_surviving_the_clamp(bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        RctSocTargetConvention().register_ratio(
            DispatchMode.CHARGE_FROM_GRID, stop_target_percent=bad, soc_percent=50.0
        )
    with pytest.raises(ValueError, match="finite"):
        RctSocTargetConvention(SocTargetMode.BELOW_CURRENT_SOC).register_ratio(
            DispatchMode.CHARGE_FROM_GRID, stop_target_percent=80.0, soc_percent=bad
        )


def test_a_target_above_a_hundred_percent_still_clamps_into_the_register_range() -> None:
    assert RctSocTargetConvention().register_ratio(
        DispatchMode.CHARGE_FROM_GRID, stop_target_percent=140.0, soc_percent=50.0
    ) == 1.0


# --- the adapter: the same two policies through RctDispatchGateway ------------------------------


class FakeRctGateway:
    """Records what the adapter writes. No device, no network, no transaction."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, object]] = []

    async def write_metric(self, device_id: str, name: str, value) -> WriteOutcome:
        self.writes.append((device_id, name, value))
        return WriteOutcome(name, value, value, True, False, datetime(2026, 1, 1, tzinfo=UTC))


def adapter(policies: SocTargetPolicyRegistry) -> tuple[RctDispatchGateway, FakeRctGateway]:
    rct = FakeRctGateway()
    return (
        RctDispatchGateway(
            rct,  # type: ignore[arg-type]
            capabilities=CapabilityRegistry(),
            soc_target_policies=policies,
        ),
        rct,
    )


async def test_adapter_writes_the_business_target_ratio_under_the_shipped_policy() -> None:
    dispatch, rct = adapter(SocTargetPolicyRegistry())
    await dispatch.apply_soc_target(
        "main", dispatch_mode=DispatchMode.CHARGE_FROM_GRID, stop_target_percent=80.0, soc_percent=54.9
    )
    assert rct.writes == [("main", "power_mng_soc_target_set", 0.8)]


async def test_adapter_derives_per_device_and_per_call_from_the_policy_registry() -> None:
    """An operator's policy change takes effect on the next apply, without a restart, and only for
    the device it was made for."""
    policies = SocTargetPolicyRegistry()
    dispatch, rct = adapter(policies)
    policies.replace(SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC, below_margin_percent=5.0))
    await dispatch.apply_soc_target(
        "main", dispatch_mode=DispatchMode.CHARGE_FROM_GRID, stop_target_percent=80.0, soc_percent=54.9
    )
    await dispatch.apply_soc_target(
        "slave1", dispatch_mode=DispatchMode.CHARGE_FROM_GRID, stop_target_percent=80.0, soc_percent=54.9
    )
    assert rct.writes[0] == ("main", "power_mng_soc_target_set", pytest.approx(0.499))
    assert rct.writes[1] == ("slave1", "power_mng_soc_target_set", 0.8)


async def test_adapter_turns_an_impossible_derivation_into_a_protocol_error() -> None:
    dispatch, rct = adapter(SocTargetPolicyRegistry())
    with pytest.raises(DeviceApiError) as excinfo:
        await dispatch.apply_soc_target(
            "main", dispatch_mode=DispatchMode.HOLD, stop_target_percent=80.0, soc_percent=50.0
        )
    assert excinfo.value.code == "protocol_error"
    assert rct.writes == []


async def test_the_restore_path_replays_the_measured_ratio_verbatim_under_every_policy() -> None:
    """A restore writes back what was read from the device; a measured value is never re-derived."""
    policies = SocTargetPolicyRegistry()
    policies.replace(SocTargetPolicy("main", SocTargetMode.BELOW_CURRENT_SOC))
    dispatch, rct = adapter(policies)
    snapshot = DeviceControlSnapshot(
        PowerSetpoint(), 0.42, 4, False, datetime(2026, 1, 1, tzinfo=UTC), all_fresh=True
    )
    await dispatch.restore("main", snapshot, 2)
    assert rct.writes == [("main", "power_mng_soc_target_set", 0.42)]


