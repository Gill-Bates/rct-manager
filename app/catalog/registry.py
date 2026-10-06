#!/usr/bin/env python3
#
# app/catalog/registry.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Packaged object registry and the catalog built from it (Requirement 4)."""

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.catalog.base import MetricDescriptor, NeutralValueType
from app.errors import ConfigError, UnknownMetric
from app.protocol.slave_data import SLAVE_DATA_SIZE
from app.protocol.types import DataType, StructKind
from app.protocol.values import DEFAULT_WIDTHS

# Credentials must never be readable or writable over the API; the registry is operator-mountable,
# so the guard rejects them at startup instead of trusting the shipped file.
SECRET_METRIC_NAMES: frozenset[str] = frozenset({"wifi_password"})
SECRET_OBJECT_IDS: frozenset[int] = frozenset({0x14C0E627})  # wifi.password under any name

_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_INT_WIDTHS = frozenset({1, 2, 4})
_NEUTRAL_TYPES: dict[DataType, frozenset[NeutralValueType]] = {
    DataType.BOOL: frozenset({NeutralValueType.BOOLEAN}),
    DataType.FLOAT: frozenset({NeutralValueType.NUMBER}),
    DataType.STRING: frozenset({NeutralValueType.STRING}),
    DataType.STRUCT: frozenset({NeutralValueType.OBJECT}),
}
_INTEGERISH = frozenset({NeutralValueType.INTEGER, NeutralValueType.ENUM})

# The slave list lives on this object id (Requirement 4.5).
SLAVE_DATA_OBJECT_ID = 0xC0A7074F


class RegistryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    object_id: int = Field(ge=0, le=2**32 - 1)  # JSON carries "0x..." strings
    data_type: DataType
    unit: str
    value_type: NeutralValueType
    idempotent_write: bool
    writable: bool = False
    is_action: bool = False
    preselected: bool = False
    struct: StructKind | None = None
    byte_width: int | None = None
    enum_labels: dict[int, str] = Field(default_factory=dict)
    prometheus_name: str | None = None
    description: str = ""
    # Operator-facing explanation shown in the GUI; empty where the parameter's meaning is not
    # documented. ``description`` stays the vendor object path and feeds the Prometheus HELP text.
    help_text: str = ""
    scale: float = 1.0  # multiplied onto a decoded number so the API itself reports the declared unit

    @field_validator("object_id", mode="before")
    @classmethod
    def _hex_id(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return int(value, 16) if value.lower().startswith("0x") else int(value)
            except ValueError:
                raise ValueError("object_id must be an integer or a 0x-prefixed hex string") from None
        return value

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("name must match [a-z][a-z0-9_]{0,63}")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        is_struct = self.data_type is DataType.STRUCT
        if is_struct and self.struct is None:
            raise ValueError("t_struct requires a struct kind")
        if not is_struct and self.struct is not None:
            raise ValueError("struct is only allowed for t_struct")
        if is_struct and (self.byte_width is not None or self.writable or self.is_action):
            raise ValueError("t_struct has a fixed layout and is neither writable nor an action")
        if self.byte_width is not None:
            self._check_width()
        allowed = _NEUTRAL_TYPES.get(self.data_type, _INTEGERISH)
        if self.value_type not in allowed:
            raise ValueError(f"value_type {self.value_type.value} does not fit {self.data_type.value}")
        if self.is_action and not self.writable:
            raise ValueError("an action variable must be writable")
        if self.is_action and self.idempotent_write:
            raise ValueError("an action variable is never idempotent")
        if self.enum_labels and self.value_type is not NeutralValueType.ENUM:
            raise ValueError("enum_labels are only allowed for enum metrics")
        if self.enum_labels:
            self._check_enum_label_codes()
        if self.scale != 1.0 and self.value_type is not NeutralValueType.NUMBER:
            raise ValueError("scale is only allowed for number metrics")
        if self.scale == 0.0:
            raise ValueError("scale must not be zero")
        return self

    def _check_enum_label_codes(self) -> None:
        """Label codes must fit the metric's own encoded width, matching the exporter's StateSet use."""
        width = self.byte_width if self.byte_width is not None else DEFAULT_WIDTHS.get(self.data_type)
        if width is None:
            return
        low, high = (0, 2 ** (8 * width) - 1)
        bad = [code for code in self.enum_labels if not low <= code <= high]
        if bad:
            raise ValueError(f"enum_labels code(s) {sorted(bad)} do not fit a {8 * width}-bit value")

    def _check_width(self) -> None:
        width = self.byte_width
        if self.data_type is DataType.STRING:
            if width is None or width < 1:
                raise ValueError("byte_width must be positive")
        elif self.data_type is DataType.FLOAT:
            if width != 4:
                raise ValueError("t_float is single precision (byte_width 4)")
        elif width not in _INT_WIDTHS:
            raise ValueError("byte_width must be 1, 2 or 4")

    @property
    def expected_payload(self) -> int | None:
        """Payload size for fixed-size types; None for strings without a configured width."""
        if self.data_type is DataType.STRUCT:
            return SLAVE_DATA_SIZE
        return self.byte_width


class ObjectRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    entries: list[RegistryEntry]

    @model_validator(mode="after")
    def _check_unique(self) -> Self:
        for label, values in (
            ("name", [e.name for e in self.entries]),
            ("object_id", [e.object_id for e in self.entries]),
        ):
            seen: set[object] = set()
            for value in values:
                if value in seen:
                    shown = f"0x{value:08X}" if label == "object_id" else value
                    raise ValueError(f"duplicate {label}: {shown}")
                seen.add(value)
        for entry in self.entries:
            if entry.name in SECRET_METRIC_NAMES or entry.object_id in SECRET_OBJECT_IDS:
                raise ValueError(f"secret metric must not be exposed: {entry.name} (0x{entry.object_id:08X})")
            is_slave = entry.struct is StructKind.SLAVE_DATA
            if is_slave and entry.object_id != SLAVE_DATA_OBJECT_ID:
                raise ValueError(f"struct slave_data is only allowed on object 0x{SLAVE_DATA_OBJECT_ID:08X}")
            if entry.object_id == SLAVE_DATA_OBJECT_ID and not is_slave:
                raise ValueError(f"object 0x{SLAVE_DATA_OBJECT_ID:08X} must be t_struct with struct slave_data")
        return self

    @classmethod
    def load(cls, path: Path) -> Self:
        """Read and validate the registry file; any violation aborts the start (Requirement 4.19, 4.20)."""
        if not path.is_file():  # a missing bind-mount source makes Docker create a directory instead
            raise ConfigError(
                "invalid_object_registry",
                detail=f"object registry {path} is missing or not a file; check the packaged catalog (reinstall the application)",
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(
                "invalid_object_registry", detail=f"cannot read {path.name}: {type(exc).__name__}"
            ) from None
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            lines = []
            for err in exc.errors(include_url=False, include_input=False, include_context=False):
                where = ".".join(str(p) for p in err["loc"])
                lines.append(f"{path.name}: {where}: {err['msg'].removeprefix('Value error, ')}")
            raise ConfigError("invalid_object_registry", detail="\n".join(lines)) from None


class RegistryCatalog:
    """MetricCatalog over the registry; ``object_entry`` is the adapter-side extension."""

    def __init__(self, registry: ObjectRegistry) -> None:
        self._by_name = {e.name: e for e in registry.entries}
        self._by_id = {e.object_id: e for e in registry.entries}

    @classmethod
    def from_file(cls, path: Path) -> "RegistryCatalog":
        return cls(ObjectRegistry.load(path))

    def _neutral(self, name: str) -> RegistryEntry:
        entry = self._by_name.get(name)
        if entry is None or entry.data_type is DataType.STRUCT:  # structures are diagnostics-only
            raise UnknownMetric(name=name)
        return entry

    def describe(self, name: str) -> MetricDescriptor:
        entry = self._neutral(name)
        return MetricDescriptor(
            entry.name,
            entry.unit,
            entry.value_type,
            entry.writable,
            entry.preselected,
            entry.is_action,
            entry.description,
        )

    def names(self) -> Sequence[str]:
        return [n for n, e in self._by_name.items() if e.data_type is not DataType.STRUCT]

    def preselected(self) -> Sequence[str]:
        return [n for n in self.names() if self._by_name[n].preselected]

    def exists(self, name: str) -> bool:
        entry = self._by_name.get(name)
        return entry is not None and entry.data_type is not DataType.STRUCT

    # ---- adapter side (RctGateway, diagnostics router, exporter) ----
    def object_entry(self, name: str) -> RegistryEntry:
        entry = self._by_name.get(name)
        if entry is None:
            raise UnknownMetric(name=name)
        return entry

    def entry_by_object_id(self, object_id: int) -> RegistryEntry | None:
        return self._by_id.get(object_id)

    def entries(self) -> Sequence[RegistryEntry]:
        return list(self._by_name.values())
