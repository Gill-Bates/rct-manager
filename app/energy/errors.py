#!/usr/bin/env python3
#
# app/energy/errors.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The Energy Manager's own refusals.

``EnergyRejected`` is a ``DeviceApiError``, so ``app.api.problems.normalize_code()`` maps it by its
``code`` like every other domain error — the manager raises business codes and never builds HTTP
responses itself.
"""

from app.errors import DeviceApiError


class EnergyRejected(DeviceApiError):
    """Codes the manager raises: energy_manager_off, energy_manager_not_external,
    energy_manager_external, energy_write_support_required, energy_action_unavailable,
    invalid_request, value_not_finite, value_out_of_range, dispatch_limits_missing, and the dispatch
    codes it re-raises on behalf of the dispatch layer (dispatch_store_unavailable,
    dispatch_restore_required).
    """

    code = "energy_action_unavailable"
