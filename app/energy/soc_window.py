#!/usr/bin/env python3
#
# app/energy/soc_window.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The one definition of the effective SoC window shared by the executor and the planner.

The Energy Manager narrows the hard product SoC window (:data:`TARGET_SOC_MIN_PERCENT` ..
:data:`TARGET_SOC_MAX_PERCENT`) with the operator-configured ``min_soc``/``max_soc`` bounds. The
Dry-Run planner must reject exactly the same targets the executor would reject, so both compute the
window through this pure function instead of duplicating the clamp.
"""

from app.limits import TARGET_SOC_MAX_PERCENT, TARGET_SOC_MIN_PERCENT


def effective_target_window(min_soc: float, max_soc: float) -> tuple[float, float]:
    """The configured SoC bounds narrowed to the hard product window.

    Returns ``(lower, upper)`` where ``lower = max(TARGET_SOC_MIN_PERCENT, min_soc)`` and
    ``upper = min(TARGET_SOC_MAX_PERCENT, max_soc)`` — the same clamp
    :meth:`app.energy.manager.EnergyManager._window` applies.
    """
    return (
        max(TARGET_SOC_MIN_PERCENT, min_soc),
        min(TARGET_SOC_MAX_PERCENT, max_soc),
    )
