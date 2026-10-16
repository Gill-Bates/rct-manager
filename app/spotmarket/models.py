#!/usr/bin/env python3
#
# app/spotmarket/models.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Spot-market configuration and the vendor-neutral price types.

Two concerns live here and are deliberately kept apart:

* :class:`SpotMarketConfig` is the operator-editable configuration, validated at the admin trust
  boundary and persisted in the admin store (one key, like ``write_names``), not as environment
  variables.
* :class:`SpotPriceInterval` / :class:`SpotPriceSnapshot` are the neutral internal representation a
  provider returns. Prices are always normalised to EUR/kWh before they leave the provider, so a
  provider-specific unit such as EUR/MWh never reaches a later optimizer.

The exchange *bidding zone* (where a price is formed, e.g. ``DE-LU``) and the *fee region* (the
local grid-fee / tariff area) are two different fields on purpose. The first step models the fee
region as a free, operator-set identifier; it is not resolved against any grid-fee database yet.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import is_valid_iana_zone
from app.clock import require_aware as _require_aware

# A conservative bound on how many day-ahead intervals one snapshot may carry. A full day at
# 15-minute resolution is 96; two days with a little headroom stays well under this.
MAX_INTERVALS = 1000
# EUR/MWh -> EUR/kWh. A single conversion constant, applied once in the provider.
MWH_PER_KWH = Decimal(1000)


class SpotMarketProvider(StrEnum):
    """The spot-price source. Only one provider exists in this first step; the enum keeps the
    stored value closed so an unknown provider is a validation error, not a silent fallback.
    """

    ENERGY_CHARTS = "energy_charts"


class PriceUnit(StrEnum):
    """The unit a provider reports prices in, before normalisation. The internal snapshot is always
    EUR/kWh; this names what the external source delivers so the conversion is explicit. It is
    provider metadata read from the API response, never an operator input.
    """

    EUR_PER_KWH = "EUR/kWh"
    EUR_PER_MWH = "EUR/MWh"


# Fraunhofer Energy-Charts public API, next-day day-ahead price endpoint (API v2). A fixed, trusted
# origin owned by the provider, not an operator-editable field: a free-form URL would be an SSRF
# target (loopback, RFC1918, link-local/metadata, internal DNS). The provider holds the only copy.
# ``/v2/price_next_day`` returns tomorrow's full day-ahead curve; the legacy ``/price`` without a
# ``start`` would return today's running intervals, which is not what a day-ahead plan needs.
ENERGY_CHARTS_URL = "https://api.energy-charts.info/v2/price_next_day"
# The default exchange bidding zone (Germany/Luxembourg), an Energy-Charts ``bzn`` value.
DEFAULT_BIDDING_ZONE = "DE-LU"
# The currency Energy-Charts reports day-ahead spot prices in. Provider metadata, not operator
# input: the operator cannot claim a different currency than the source actually delivers.
ENERGY_CHARTS_CURRENCY = "EUR"


class FeeRegion(BaseModel):
    """The local grid-fee / tariff area, modelled as a free identifier plus an optional label.

    This is intentionally not a closed list of German grid regions: resolving fees against a
    network-charge database is a later work package. ``code`` is just an operator-chosen key.
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field("", max_length=64)
    label: str = Field("", max_length=128)

    @field_validator("code", "label")
    @classmethod
    def _printable(cls, value: str) -> str:
        value = value.strip()
        if any(not char.isprintable() for char in value):
            raise ValueError("must be printable text")
        return value


class SpotMarketConfig(BaseModel):
    """Operator-editable spot-market configuration, validated at the admin boundary.

    It expresses *which* provider and *which* zone, never a network target: the provider owns its
    fixed trusted endpoint, so there is no operator-editable URL to turn into an SSRF surface. The
    bidding zone and the fee region are separate fields and never collapsed into one.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    provider: SpotMarketProvider = SpotMarketProvider.ENERGY_CHARTS
    bidding_zone: str = Field(DEFAULT_BIDDING_ZONE, min_length=1, max_length=32)
    fee_region: FeeRegion = Field(default_factory=FeeRegion)

    @field_validator("bidding_zone")
    @classmethod
    def _trim_token(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char.isspace() for char in value):
            raise ValueError("must not be empty or contain whitespace")
        return value


@dataclass(frozen=True, slots=True)
class BiddingZoneCatalog:
    """The set of bidding zones a provider accepts, read from provider metadata, not hardcoded.

    ``zones`` is the closed list of accepted ``bzn`` values and ``default`` the provider's default
    (``DE-LU`` for Energy-Charts). ``fetched_at`` is when the metadata was retrieved, so a cache can
    decide freshness and present a last-successful list as stale rather than fabricating one.
    """

    zones: tuple[str, ...]
    default: str
    fetched_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.fetched_at, "fetched_at")
        if not self.zones:
            raise ValueError("a bidding-zone catalog must list at least one zone")
        if self.default not in self.zones:
            raise ValueError("the default bidding zone must be one of the listed zones")


@dataclass(frozen=True, slots=True)
class SpotPriceInterval:
    """One day-ahead price interval, normalised. ``price_eur_per_kwh`` is always EUR/kWh regardless
    of what the provider reported, and ``start``/``end`` are timezone-aware.

    This type is the provider-neutral contract a later optimizer consumes, so it enforces its own
    invariants centrally: a violating interval is a :class:`ValueError` at construction, never a
    formally valid object carrying a naive timestamp, an inverted range or a non-finite price.
    """

    start: datetime
    end: datetime
    price_eur_per_kwh: Decimal

    def __post_init__(self) -> None:
        _require_aware(self.start, "start")
        _require_aware(self.end, "end")
        if self.end <= self.start:
            raise ValueError("end must be strictly after start")
        if not isinstance(self.price_eur_per_kwh, Decimal):
            raise ValueError("price_eur_per_kwh must be a Decimal")
        if not self.price_eur_per_kwh.is_finite():
            raise ValueError("price_eur_per_kwh must be a finite number")


@dataclass(frozen=True, slots=True)
class SpotPriceSnapshot:
    """The result of one provider fetch: the intervals plus the context they were read for.

    Like :class:`SpotPriceInterval`, the snapshot guards its own invariants: its intervals must be
    chronologically ordered and must not overlap, so the optimizer never sees two prices claiming
    the same instant.
    """

    provider: SpotMarketProvider
    bidding_zone: str
    currency: str
    fetched_at: datetime
    intervals: tuple[SpotPriceInterval, ...]
    timezone: str | None = None
    source_generated_at: datetime | None = None
    available_from: datetime | None = None
    available_until: datetime | None = None
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.fetched_at, "fetched_at")
        if self.timezone is not None:
            if not isinstance(self.timezone, str):
                raise ValueError("timezone must be an IANA zone")
            if not is_valid_iana_zone(self.timezone):
                raise ValueError("timezone must be an IANA zone")
        for field in ("source_generated_at", "available_from", "available_until", "valid_until"):
            value = getattr(self, field)
            if value is not None:
                _require_aware(value, field)
        if not isinstance(self.intervals, tuple):
            raise ValueError("intervals must be a tuple")
        # Each interval already guarantees start < end, so requiring the next start not to precede
        # the previous end enforces both ordering and non-overlap in one pass.
        for previous, current in zip(self.intervals, self.intervals[1:], strict=False):
            if current.start < previous.end:
                raise ValueError("intervals must be ordered and non-overlapping")
