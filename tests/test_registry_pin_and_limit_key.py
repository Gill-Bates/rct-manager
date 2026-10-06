#!/usr/bin/env python3
#
# tests/test_registry_pin_and_limit_key.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Slave-data pin on its object id (Requirement 4.5) and the business rate-limit caller key."""

import json

import pytest

from app.catalog.registry import ObjectRegistry
from app.errors import ConfigError
from app.security.ratelimit import caller_key
from tests.api_helpers import REGISTRY_FIXTURE


def _write(tmp_path, mutate) -> object:
    data = json.loads(REGISTRY_FIXTURE.read_text(encoding="utf-8"))
    mutate(data["entries"])
    path = tmp_path / "objects_read.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _slave(entries: list[dict]) -> dict:
    return next(e for e in entries if e.get("struct") == "slave_data")


def test_fixture_registry_is_accepted() -> None:
    ObjectRegistry.load(REGISTRY_FIXTURE)


def test_slave_data_on_another_object_id_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, lambda entries: _slave(entries).update(object_id="0x11111111"))
    with pytest.raises(ConfigError) as info:
        ObjectRegistry.load(path)
    assert "0xC0A7074F" in str(info.value.context["detail"])


def test_pinned_object_id_must_be_slave_data(tmp_path) -> None:
    def mutate(entries: list[dict]) -> None:
        slave = _slave(entries)
        slave.pop("struct")
        slave.update(data_type="t_float", value_type="number")

    with pytest.raises(ConfigError) as info:
        ObjectRegistry.load(_write(tmp_path, mutate))
    assert "slave_data" in str(info.value.context["detail"])


def _battery_power(entries: list[dict]) -> dict:
    return next(e for e in entries if e["name"] == "battery_power")


def test_enum_labels_on_a_non_enum_metric_is_rejected(tmp_path) -> None:
    """A number metric must not acquire StateSet semantics just by carrying enum_labels."""

    def mutate(entries: list[dict]) -> None:
        _battery_power(entries)["enum_labels"] = {"0": "idle"}

    with pytest.raises(ConfigError) as info:
        ObjectRegistry.load(_write(tmp_path, mutate))
    assert "enum_labels are only allowed for enum metrics" in str(info.value.context["detail"])


def test_enum_label_code_outside_the_byte_width_is_rejected(tmp_path) -> None:
    def mutate(entries: list[dict]) -> None:
        inverter_state = next(e for e in entries if e["name"] == "inverter_state")
        inverter_state["enum_labels"]["256"] = "overflow"  # t_uint8: codes must fit 0..255

    with pytest.raises(ConfigError) as info:
        ObjectRegistry.load(_write(tmp_path, mutate))
    assert "do not fit a 8-bit value" in str(info.value.context["detail"])


def test_scale_on_a_non_number_metric_is_rejected(tmp_path) -> None:
    def mutate(entries: list[dict]) -> None:
        inverter_state = next(e for e in entries if e["name"] == "inverter_state")
        inverter_state["scale"] = 100

    with pytest.raises(ConfigError) as info:
        ObjectRegistry.load(_write(tmp_path, mutate))
    assert "scale is only allowed for number metrics" in str(info.value.context["detail"])


def test_zero_scale_is_rejected(tmp_path) -> None:
    def mutate(entries: list[dict]) -> None:
        _battery_power(entries)["scale"] = 0

    with pytest.raises(ConfigError) as info:
        ObjectRegistry.load(_write(tmp_path, mutate))
    assert "scale must not be zero" in str(info.value.context["detail"])


def test_caller_key_combines_token_and_address() -> None:
    assert caller_key("t1", "192.0.2.1") != caller_key("t2", "192.0.2.1")
    assert caller_key("t1", "192.0.2.1") != caller_key("t1", "192.0.2.2")
    assert caller_key("t1", "2001:db8::1") == caller_key("t1", "2001:db8::2")  # same /64
