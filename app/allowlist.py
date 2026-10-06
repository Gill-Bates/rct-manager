#!/usr/bin/env python3
#
# app/allowlist.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Write policy and the value check in front of every write (Requirement 19)."""

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.catalog.registry import SECRET_METRIC_NAMES, RegistryCatalog
from app.errors import ConfigError, UnknownMetric, WriteRejected
from app.protocol.types import DataType
from app.protocol.values import ScalarValue

_STEP_TOLERANCE = 1e-9
_INT_RANGES = {
    DataType.UINT8: (0, 2**8 - 1),
    DataType.INT8: (-(2**7), 2**7 - 1),
    DataType.UINT16: (0, 2**16 - 1),
    DataType.INT16: (-(2**15), 2**15 - 1),
    DataType.UINT32: (0, 2**32 - 1),
    DataType.INT32: (-(2**31), 2**31 - 1),
    DataType.ENUM: (0, 2**32 - 1),
}


class AllowlistEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    data_type: DataType
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = Field(None, gt=0)
    allowed_values: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.data_type is DataType.STRUCT:
            raise ValueError(f"{self.data_type.value} cannot be approved for writing")
        if (
            self.data_type not in (DataType.BOOL, DataType.STRING)
            and not self.allowed_values
            and (self.minimum is None or self.maximum is None)
        ):
            raise ValueError("a value range (minimum and maximum) is required")
        if self.data_type in (DataType.BOOL, DataType.STRING) and (
            self.minimum is not None or self.maximum is not None or self.step is not None or self.allowed_values
        ):
            raise ValueError(f"{self.data_type.value} takes no minimum, maximum, step or allowed_values")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        if (width := _INT_RANGES.get(self.data_type)) is not None:
            low, high = width
            bounds = [b for b in (self.minimum, self.maximum) if b is not None]
            if any(not low <= b <= high for b in bounds) or any(not low <= v <= high for v in self.allowed_values):
                raise ValueError(f"minimum, maximum and allowed_values must fit the range of {self.data_type.value}")
        return self


class Allowlist:
    """Approved write targets; deny by default."""

    def __init__(self, entries: Mapping[str, AllowlistEntry], catalog: RegistryCatalog) -> None:
        self._entries = dict(entries)
        self._catalog = catalog

    def __len__(self) -> int:
        return len(self._entries)

    def entry(self, name: str) -> AllowlistEntry | None:
        return self._entries.get(name)

    @classmethod
    def load(cls, path: Path, catalog: RegistryCatalog) -> Self:
        """Abort the start on any inconsistency with the object registry (Requirement 19.17 to 19.20)."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(
                "invalid_write_allowlist", detail=f"cannot read {path.name}: {type(exc).__name__}"
            ) from None
        problems: list[str] = []
        entries: dict[str, AllowlistEntry] = {}
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list) or raw.get("version") != 1:
            raise ConfigError(
                "invalid_write_allowlist", detail=f"{path.name}: expected an object with version 1 and entries"
            )
        for index, item in enumerate(raw["entries"]):
            try:
                entry = AllowlistEntry.model_validate(item)
            except ValidationError as exc:
                for err in exc.errors(include_url=False, include_input=False, include_context=False):
                    where = ".".join(str(p) for p in err["loc"])
                    problems.append(
                        f"entries[{index}] {where}: {err['msg'].removeprefix('Value error, ')}".replace(": :", ":")
                    )
                continue
            if entry.name in SECRET_METRIC_NAMES:
                problems.append(f"entries[{index}] ({entry.name}): secret metric must not be writable")
                continue
            problems.extend(cls._check_against_registry(index, entry, catalog))
            if entry.name in entries:
                problems.append(f"entries[{index}] ({entry.name}): duplicate entry")
            entries[entry.name] = entry
        if problems:
            raise ConfigError("invalid_write_allowlist", detail="\n".join(f"{path.name}: {p}" for p in problems))
        return cls(entries, catalog)

    @staticmethod
    def _check_against_registry(index: int, entry: AllowlistEntry, catalog: RegistryCatalog) -> list[str]:
        where = f"entries[{index}] ({entry.name})"
        try:
            reg = catalog.object_entry(entry.name)
        except UnknownMetric:
            return [f"{where}: metric is not in the object registry"]
        problems: list[str] = []
        if reg.data_type is not entry.data_type:
            problems.append(f"{where}: data_type differs from the object registry")
        if not reg.writable:
            problems.append(f"{where}: metric is not writable in the object registry")
        if reg.is_action and (not entry.allowed_values or entry.minimum is not None or entry.maximum is not None):
            problems.append(f"{where}: an action variable needs an explicit list of allowed_values and no range")
        return problems

    # ---- value check -----------------------------------------------------------------------
    def check(self, name: str, value: ScalarValue, *, action: bool) -> AllowlistEntry:
        """Raise WriteRejected unless ``value`` may be written to ``name``; never touches the device."""
        reg = self._catalog.object_entry(name)
        if reg.is_action and not action:
            raise WriteRejected("metric_is_action")
        entry = self._entries.get(name)
        if entry is None or action != reg.is_action:
            raise WriteRejected("write_not_allowed")
        self._check_value(entry, value)
        return entry

    @staticmethod
    def _check_value(entry: AllowlistEntry, value: ScalarValue) -> None:
        kind = entry.data_type
        if kind is DataType.STRING:
            if not isinstance(value, str):
                raise WriteRejected("value_type_mismatch")
            if "\x00" in value:
                raise WriteRejected("value_out_of_range")
            return
        if kind is DataType.BOOL:
            if not isinstance(value, bool):
                raise WriteRejected("value_type_mismatch")
            return
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise WriteRejected("value_type_mismatch")
        if kind is DataType.FLOAT:
            if not math.isfinite(value):
                raise WriteRejected("value_not_finite")
        elif type(value) is not int:  # integer types and enums: the encoder takes exact ints only
            raise WriteRejected("value_type_mismatch")
        if entry.allowed_values:
            if value not in entry.allowed_values:
                raise WriteRejected("value_out_of_range")
            return
        assert entry.minimum is not None and entry.maximum is not None
        if not entry.minimum <= value <= entry.maximum:
            raise WriteRejected("value_out_of_range")
        if entry.step is not None:
            steps = value / entry.step
            if abs(steps - round(steps)) > _STEP_TOLERANCE * max(1.0, abs(steps)):
                raise WriteRejected("value_step_mismatch")
