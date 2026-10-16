#!/usr/bin/env python3
#
# app/gateway/live_charge_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""RCT producer for the Live Charge Envelope seam — INERT on this build.

This is the vendor adapter behind ``app.dispatch.live_limit.LiveChargeLimitProvider``, kept out of
the domain layer exactly like ``RctEnergyReadings`` sits behind the vendor-neutral readings port.

It deliberately always returns ``None``: the object catalog has NO read-only "max charge power"
register, and the only path to a power ceiling would be a
``battery_voltage x battery_maximum_charge_current`` derivation whose operands both carry
"not specified by the protocol" catalog caveats (measuring point, sign, filter). Emitting such a
value as a production safety clamp would rest a safety function on an unproven hardware assumption,
which the task forbids. So the producer stays inert and dispatch falls back to the configured static
``DeviceLimits.max_charge_power_w`` (the RCT/BMS keeps its own internal protection regardless).

There is no "flippable" global switch here on purpose. Hardware verification in this project is
modeled per ``device_id`` through ``CapabilityRecord``, so activating a live limit will require a
real per-device verification contract (a new capability and a confirmed register), designed in a
later package. Until that exists this producer returns ``None`` unconditionally — the seam stays
honest and testable (AC-11) without a misleading intermediate state that only leads to an error.
Nothing here raises and nothing queues a device transaction.
"""

from app.dispatch.live_limit import ChargePowerLimit
from app.gateway.rct import RctGateway


class RctLiveChargeLimitProvider:
    """Inert RCT live charge-limit producer: returns ``None`` for every device on this build.

    The ``gateway`` is held for the future verified path (a cache-only read of a confirmed register,
    gated by a per-device verification contract that does not exist yet); today it is unused on
    purpose, and no value is ever derived or invented.
    """

    def __init__(self, gateway: RctGateway) -> None:
        self._rct = gateway

    def live_charge_limit(self, device_id: str) -> ChargePowerLimit | None:
        # No verified read-only charge-power register exists on this build: stay inert and let the
        # configured static device limit govern. Never derive U x I as a production limit.
        return None
