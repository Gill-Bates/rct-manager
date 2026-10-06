#!/usr/bin/env python3
#
# app/catalog/base.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Vendor-neutral metric catalog port (no object ids above this boundary)."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class NeutralValueType(StrEnum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    ENUM = "enum"
    OBJECT = "object"


_NON_NUMERIC = frozenset({NeutralValueType.STRING, NeutralValueType.OBJECT})


def is_numeric(value_type: NeutralValueType) -> bool:
    """True for value types that carry a plottable/exportable number (not string or struct)."""
    return value_type not in _NON_NUMERIC


@dataclass(frozen=True, slots=True)
class MetricDescriptor:
    name: str
    unit: str
    value_type: NeutralValueType
    writable: bool
    preselected: bool
    is_action: bool = False
    description: str = ""


class MetricCatalog(Protocol):
    """Names, units and neutral value types of the metrics a device offers."""

    def describe(self, name: str) -> MetricDescriptor: ...

    def names(self) -> Sequence[str]: ...

    def preselected(self) -> Sequence[str]: ...

    def exists(self, name: str) -> bool: ...
