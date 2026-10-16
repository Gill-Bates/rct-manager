#!/usr/bin/env python3
#
# app/optimizer/align.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pure, DST-safe resampling of planning inputs onto a common 15-minute UTC grid.

The optimizer needs the three forecast series (day-ahead spot prices, solar production and
household consumption) expressed on one shared time grid before it can reason slot by slot. The
helpers here build that grid and resample each series onto it without any I/O or clock access:

* ``build_grid`` produces the ordered sequence of ``(start, end)`` UTC slots spanning
  ``[align_up(now, 15min), now + horizon]``.
* ``align_prices`` treats a price curve as a step function: a grid slot takes the price of the
  source interval that contains the slot's **midpoint**, using the half-open convention
  ``start <= midpoint < end`` so a midpoint landing exactly on a coarser boundary belongs to the
  later interval (matching ``active_slot``'s ``slot.start <= now < slot.end``). A slot whose
  midpoint falls in no interval is a *price gap* and resolves to ``None``.
* ``align_energy`` treats solar/consumption as additive: a grid slot receives the sum, over every
  overlapping source interval, of ``energy_wh * (overlap_seconds / source_seconds)`` — the same
  clip-and-prorate reasoning the weather provider already applies to a partial interval.

Every computation is on absolute UTC instants, so a local spring-forward/fall-back never changes
the length of a 900 s slot; local-calendar bucketing (needed only for display/KPIs elsewhere) is
not done here.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.optimizer.models import (
    ConsumptionForecast,
    SolarForecast,
    _require_aware,
)
from app.spotmarket.models import SpotPriceSnapshot

# The common grid resolution, matching the 15-minute quarter-hourly resolution of the solar
# forecast (``_STEP_SECONDS`` in app/weather/open_meteo.py) and the consumption provider.
GRID_STEP_SECONDS = 900
_GRID_STEP = timedelta(seconds=GRID_STEP_SECONDS)


def align_up(moment: datetime, step: timedelta = _GRID_STEP) -> datetime:
    """Round ``moment`` up to the next grid boundary (a no-op when already on a boundary).

    The boundary lattice is anchored at the Unix epoch in UTC, so the result is independent of the
    local calendar and is stable across DST transitions.
    """
    _require_aware(moment, "moment")
    step_seconds = step.total_seconds()
    if step_seconds <= 0:
        raise ValueError("step must be a positive timedelta")
    instant = moment.astimezone(UTC)
    epoch_seconds = instant.timestamp()
    remainder = epoch_seconds % step_seconds
    if remainder == 0:
        return instant
    return datetime.fromtimestamp(epoch_seconds + (step_seconds - remainder), tz=UTC)


def build_grid(
    now: datetime, horizon: timedelta, step: timedelta = _GRID_STEP
) -> tuple[tuple[datetime, datetime], ...]:
    """Return the ordered UTC grid slots spanning ``[align_up(now), now + horizon]``.

    Each element is a ``(start, end)`` pair exactly ``step`` apart. The last slot is the final one
    whose ``end`` does not exceed ``now + horizon``; a horizon shorter than one slot yields an empty
    grid rather than a partial slot.
    """
    _require_aware(now, "now")
    if not isinstance(horizon, timedelta) or horizon <= timedelta():
        raise ValueError("horizon must be a positive timedelta")
    if step <= timedelta():
        raise ValueError("step must be a positive timedelta")
    start = align_up(now, step)
    limit = now.astimezone(UTC) + horizon
    slots: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor + step <= limit:
        slots.append((cursor, cursor + step))
        cursor += step
    return tuple(slots)


def _overlap_seconds(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    """Length in seconds of the intersection of two half-open intervals (0.0 when disjoint)."""
    overlap = (min(a_end, b_end) - max(a_start, b_start)).total_seconds()
    return overlap if overlap > 0 else 0.0


def align_prices(
    prices: SpotPriceSnapshot, grid: tuple[tuple[datetime, datetime], ...]
) -> tuple[Decimal | None, ...]:
    """Resample a price curve onto ``grid`` as a step function (midpoint lookup).

    A grid slot's price is the price of the source interval that contains the slot's midpoint under
    the half-open convention ``start <= midpoint < end``. A slot whose midpoint lies in no source
    interval is a price gap and maps to ``None``.
    """
    if not isinstance(prices, SpotPriceSnapshot):
        raise ValueError("prices must be a SpotPriceSnapshot")
    intervals = prices.intervals
    result: list[Decimal | None] = []
    for slot_start, slot_end in grid:
        midpoint = slot_start + (slot_end - slot_start) / 2
        price: Decimal | None = None
        for interval in intervals:
            if interval.start <= midpoint < interval.end:
                price = interval.price_eur_per_kwh
                break
        result.append(price)
    return tuple(result)


def _align_additive(
    source_intervals: tuple, grid: tuple[tuple[datetime, datetime], ...]
) -> tuple[float, ...]:
    """Prorate an additive energy series (Wh) onto ``grid`` by overlap fraction."""
    result: list[float] = []
    for slot_start, slot_end in grid:
        total = 0.0
        for interval in source_intervals:
            source_seconds = (interval.end - interval.start).total_seconds()
            if source_seconds <= 0:
                continue
            overlap = _overlap_seconds(slot_start, slot_end, interval.start, interval.end)
            if overlap <= 0:
                continue
            total += interval.energy_wh * (overlap / source_seconds)
        result.append(total)
    return tuple(result)


def align_energy(
    forecast: SolarForecast | ConsumptionForecast, grid: tuple[tuple[datetime, datetime], ...]
) -> tuple[float, ...]:
    """Resample an additive energy forecast (solar or consumption) onto ``grid``.

    A grid slot receives the sum, over every overlapping source interval, of
    ``energy_wh * (overlap_seconds / source_seconds)``, so the total energy over the grid equals the
    energy of the covered portion of the source series. A slot with no overlapping source datum maps
    to ``0.0`` (never negative, never invented).
    """
    if not isinstance(forecast, SolarForecast | ConsumptionForecast):
        raise ValueError("forecast must be a SolarForecast or ConsumptionForecast")
    return _align_additive(forecast.intervals, grid)
