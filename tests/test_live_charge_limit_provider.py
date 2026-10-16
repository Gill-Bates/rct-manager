#!/usr/bin/env python3
#
# tests/test_live_charge_limit_provider.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""AC-11: the RCT Live Charge Envelope producer is inert-until-verified.

Even though battery_voltage, battery_current and battery_maximum_charge_current physically exist in
the object catalog, the producer must NOT derive a U x I production limit from them while the
register semantics are unverified: it returns None, and dispatch falls back to the configured static
device limit. This pins the seam honest.
"""

import inspect

from app.gateway.live_charge_limit import RctLiveChargeLimitProvider


class _StubGateway:
    """Stands in for RctGateway. Any cache read here would be a bug: the inert producer must not
    read a register to decide it has no verified limit.
    """

    def cached_sample(self, device_id: str, name: str):
        raise AssertionError("the inert producer must not read any register")


def test_inert_producer_returns_none_for_any_device() -> None:
    provider = RctLiveChargeLimitProvider(_StubGateway())  # type: ignore[arg-type]
    assert provider.live_charge_limit("main") is None
    assert provider.live_charge_limit("another-device") is None


def test_inert_producer_does_not_derive_a_limit_from_the_existing_registers() -> None:
    # The stub raises if the producer reads any register; a None result proves it never tried to
    # derive battery_voltage x battery_maximum_charge_current into a production ceiling.
    provider = RctLiveChargeLimitProvider(_StubGateway())  # type: ignore[arg-type]
    assert provider.live_charge_limit("main") is None


def test_provider_has_no_flippable_global_switch() -> None:
    # There is no global 'verified' switch: activation requires a per-device verification contract
    # that does not exist yet, so the producer exposes no misleading intermediate state.
    params = inspect.signature(RctLiveChargeLimitProvider.__init__).parameters
    assert "verified" not in params
