#!/usr/bin/env python3
#
# tests/test_float_subnormal.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""IEEE 754 subnormal payloads retain their value and wire representation."""

import math
import struct
from pathlib import Path

import pytest

from app.protocol.types import DataType
from app.protocol.values import decode_value, encode_value


def _decode(raw: int) -> float:
    return decode_value(DataType.FLOAT, raw.to_bytes(4, "big"))  # type: ignore[return-value]


def test_observed_device_value_is_preserved() -> None:
    assert _decode(0x00000004) == 2.0**-147
    assert math.copysign(1.0, _decode(0x00000004)) == 1.0


def test_negative_subnormal_is_preserved() -> None:
    value = _decode(0x80000004)
    assert value == -(2.0**-147)
    assert math.copysign(1.0, value) == -1.0


@pytest.mark.parametrize("raw", [0x007FFFFF, 0x807FFFFF, 0x00000001])
def test_largest_and_smallest_subnormals_round_trip(raw: int) -> None:
    assert encode_value(DataType.FLOAT, _decode(raw)) == raw.to_bytes(4, "big")


@pytest.mark.parametrize("raw", [0x00800000, 0x80800000, 0x3F800000, 0x43EB40E5, 0x7F7FFFFF])
def test_normal_values_are_unchanged(raw: int) -> None:
    assert _decode(raw) == struct.unpack(">f", raw.to_bytes(4, "big"))[0]


def test_zero_nan_and_infinity_are_unchanged() -> None:
    assert _decode(0x00000000) == 0.0
    assert math.isnan(_decode(0x7FC00000))
    assert _decode(0x7F800000) == math.inf
    assert _decode(0xFF800000) == -math.inf


async def test_subnormal_write_readback_preserves_value(tmp_path: Path) -> None:
    from tests.api_helpers import (
        TARGET_NAME,
        WRITE_TOKEN,
        make_settings,
        running_app,
        write_fixtures,
    )

    value = _decode(0x00000004)
    settings = make_settings(
        enable_write_support=True,
        **write_fixtures(tmp_path, step=None),
    )
    async with running_app(settings) as h:
        response = await h.client.put(
            f"/api/v1/devices/main/metrics/{TARGET_NAME}",
            json={"value": value},
            headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["readback_value"] == value
