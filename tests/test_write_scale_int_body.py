#!/usr/bin/env python3
#
# tests/test_write_scale_int_body.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Requirement 9: a scaled register divides on write for an integer body exactly as for a float."""
from pathlib import Path

import pytest

from app.protocol.types import WRITE_COMMANDS
from tests.api_helpers import WRITE_TOKEN, make_settings, running_app

ROOT = Path(__file__).resolve().parents[1]
WRITER = {"Authorization": f"Bearer {WRITE_TOKEN}"}
SOC_TARGET = "battery_soc_target"  # the only shipped register with scale != 1 (percent over a 0..1 ratio)
SOC_TARGET_OBJECT_ID = 0x8B9FF008


def _shipped_settings():
    """The scaled register only exists in the shipped catalog, so the tests write against it."""
    return make_settings(
        object_registry_path=ROOT / "app/catalog/objects.json",
        write_allowlist_path=ROOT / "app/catalog/default_write_allowlist.json",
        enable_write_support=True,
        write_response_timeout_ms=100,
    )


def _write_payload(net, object_id: int) -> str:
    """Hex of the payload of the one WRITE frame that reached the fake device for ``object_id``."""
    payloads = [f.payload.hex() for _, f in net.frames if f.command in WRITE_COMMANDS and f.object_id == object_id]
    assert len(payloads) == 1, payloads
    return payloads[0]


async def _put(h, name: str, value) -> tuple:
    response = await h.client.put(f"/api/v1/devices/main/metrics/{name}", json={"value": value}, headers=WRITER)
    return response, response.json()


@pytest.mark.parametrize("value", [80, 80.0])
async def test_scaled_register_divides_an_int_body_like_a_float_body(value) -> None:
    """An SoC target of 80 percent must reach the wire as the ratio 0.8, whether the JSON body says
    80 or 80.0. Before the fix the division was skipped for an int body, so the device silently got
    80.0 instead of 0.8: a factor of 100 too high, without an error or a warning."""
    async with running_app(_shipped_settings()) as h:
        response, body = await _put(h, SOC_TARGET, value)
        wire = _write_payload(h.net, SOC_TARGET_OBJECT_ID)
    assert response.status_code == 200, response.text
    assert wire == "3f4ccccd"  # IEEE-754 float32 of 0.8, identical for both body types
    assert body["written_value"] == value
    # float32 cannot hold 0.8 exactly, so the scaled readback is 80.0000011920929, not 80.
    assert body["confirmed"] is True and body["readback_value"] == pytest.approx(80.0)


@pytest.mark.parametrize("value,expected", [(0, "00000000"), (-5, "bd4ccccd"), (100, "3f800000")])
async def test_scaled_register_divides_zero_and_negative_int_bodies(value, expected) -> None:
    async with running_app(_shipped_settings()) as h:
        response, body = await _put(h, SOC_TARGET, value)
        wire = _write_payload(h.net, SOC_TARGET_OBJECT_ID)
    assert response.status_code == 200, response.text
    assert wire == expected
    assert body["confirmed"] is True
    assert body["readback_value"] == pytest.approx(float(value), abs=1e-4)


@pytest.mark.parametrize("name,object_id,value,expected", [
    ("power_mng_soc_strategy", 0xF168B748, 2, "02"),  # t_enum, int body
    ("power_mng_soc_charge_power", 0x1D2994EA, 100, "42c80000"),  # t_float with scale 1, int body
])
async def test_unscaled_register_keeps_an_int_body_untouched(name, object_id, value, expected) -> None:
    """Regression guard: for scale == 1.0 the int body must produce the same wire bytes as before."""
    async with running_app(_shipped_settings()) as h:
        response, body = await _put(h, name, value)
        wire = _write_payload(h.net, object_id)
    assert response.status_code == 200, response.text
    assert wire == expected
    assert body["confirmed"] is True


@pytest.mark.parametrize("value,expected", [(True, "01"), (False, "00")])
async def test_bool_body_is_never_scaled(value, expected) -> None:
    """bool is a subclass of int in Python, so it must be excluded from the scale division by hand."""
    async with running_app(_shipped_settings()) as h:
        response, body = await _put(h, "power_mng_use_grid_power_enable", value)
        wire = _write_payload(h.net, 0x36A9E9A6)
    assert response.status_code == 200, response.text
    assert wire == expected
    assert body["confirmed"] is True and body["readback_value"] is value


def test_bool_on_a_scaled_register_stays_a_bool_in_the_encoder() -> None:
    """Directly on the encoder, because no shipped register is both scaled and t_bool: a bool must
    not be divided, which for t_float means it is rejected as a type mismatch, not silently scaled."""
    from app.allowlist import Allowlist
    from app.cache import MemoryCache
    from app.catalog.registry import RegistryCatalog
    from app.clock import SystemClock
    from app.errors import WriteRejected
    from app.gateway.rct import RctGateway

    catalog = RegistryCatalog.from_file(ROOT / "app/catalog/objects.json")
    gateway = RctGateway(catalog, MemoryCache(30, 30), SystemClock(), Allowlist({}, catalog))
    entry = catalog.object_entry(SOC_TARGET)
    assert entry.scale == 100.0
    with pytest.raises(WriteRejected) as info:
        gateway._encode(entry, True)
    assert info.value.code == "value_type_mismatch"
