#!/usr/bin/env python3
#
# tests/test_values_properties.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Properties 14 (cache consistency), 17 (scalar share) and 18."""

import asyncio
import math
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.allowlist import Allowlist
from app.cache import MemoryCache
from app.catalog.registry import RegistryCatalog
from app.config import DeviceEntry
from app.errors import DecodeLengthMismatch, DeviceUnreachable
from app.gateway.base import StaleReason
from app.gateway.rct import DeviceBinding, RctGateway
from app.protocol.types import DataType
from app.protocol.values import decode_value, encode_value
from app.scheduling.retry import RetryConfig
from app.scheduling.serializer import AccessSerializer
from app.transport.endpoint import EndpointConfig, TransportEndpoint
from tests.conftest import AutoClock
from tests.fakes import FakeNetwork

PROP = settings(max_examples=200, deadline=None)

_INT_RANGES = {
    DataType.UINT8: (0, 2**8 - 1),
    DataType.INT8: (-(2**7), 2**7 - 1),
    DataType.UINT16: (0, 2**16 - 1),
    DataType.INT16: (-(2**15), 2**15 - 1),
    DataType.UINT32: (0, 2**32 - 1),
    DataType.INT32: (-(2**31), 2**31 - 1),
    DataType.ENUM: (0, 2**32 - 1),
}


# Feature: rct-manager, Property 17: value round-trip across all data types
@PROP
@given(st.data())
def test_integer_round_trip(data: st.DataObject) -> None:
    data_type = data.draw(st.sampled_from(list(_INT_RANGES)))
    low, high = _INT_RANGES[data_type]
    value = data.draw(st.integers(low, high))
    assert decode_value(data_type, encode_value(data_type, value)) == value


@PROP
@given(st.booleans())
def test_bool_round_trip(value: bool) -> None:
    assert decode_value(DataType.BOOL, encode_value(DataType.BOOL, value)) is value


@PROP
@given(st.floats(width=32))
def test_float_round_trip(value: float) -> None:
    result = decode_value(DataType.FLOAT, encode_value(DataType.FLOAT, value))
    assert (math.isnan(value) and math.isnan(result)) or result == value


@PROP
@given(st.text(alphabet=st.characters(exclude_categories=("Cs",), exclude_characters="\x00�"), max_size=40))
def test_string_round_trip(value: str) -> None:
    assert decode_value(DataType.STRING, encode_value(DataType.STRING, value)) == value


def test_bool_maps_any_nonzero_to_true() -> None:
    assert decode_value(DataType.BOOL, b"\x00") is False
    assert decode_value(DataType.BOOL, b"\x07") is True


def test_byte_width_overrides_default() -> None:
    assert decode_value(DataType.UINT32, b"\x01\x00", byte_width=2) == 256
    assert decode_value(DataType.INT16, b"\xff\xfe") == -2


def test_length_mismatch_reports_expected_and_received() -> None:
    with pytest.raises(DecodeLengthMismatch) as info:
        decode_value(DataType.UINT32, b"\x01\x02")
    assert (info.value.expected, info.value.received) == (4, 2)


# Feature: rct-manager, Property 18: string decoding terminates for every byte sequence
@PROP
@given(st.binary(max_size=200), st.sampled_from(["utf-8", "latin-1"]))
def test_string_decoding_terminates_and_is_deterministic(payload: bytes, encoding: str) -> None:
    first = decode_value(DataType.STRING, payload, encoding=encoding)
    assert first == decode_value(DataType.STRING, payload, encoding=encoding)
    assert "\x00" not in first


def test_string_ends_at_first_nul_and_replaces_invalid_bytes() -> None:
    assert decode_value(DataType.STRING, b"ab\x00cd") == "ab"
    assert decode_value(DataType.STRING, b"a\xffb") == "a�b"


def test_fixed_width_string_round_trips_through_nul_padding() -> None:
    """Finding P3-3: a decoder that cuts at the first NUL and an encoder that required the text
    itself to fill byte_width exactly made a byte_width-constrained string unwritable even for
    values the decoder had just produced from it. The encoder now NUL-pads instead."""
    encoded = encode_value(DataType.STRING, "RCT", byte_width=16)
    assert encoded == b"RCT" + b"\x00" * 13
    assert decode_value(DataType.STRING, encoded, byte_width=16) == "RCT"


def test_fixed_width_string_rejects_text_that_does_not_fit() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        encode_value(DataType.STRING, "0123456789ABCDEF", byte_width=16)  # exactly 16 bytes, no room for a NUL
    with pytest.raises(ValueError, match="does not fit"):
        encode_value(DataType.STRING, "too long for the field", byte_width=16)


_REGISTRY = Path(__file__).resolve().parent / "fixtures" / "objects.json"


async def _read_with_cache(ttl: int, grace: int, age: int, device_ok: bool, wall_jump: int) -> tuple:
    clock = AutoClock()
    net = FakeNetwork(clock, fail_connects=0 if device_ok else 1000)
    catalog = RegistryCatalog.from_file(_REGISTRY)
    cache = MemoryCache(ttl, grace)
    gateway = RctGateway(
        catalog, cache, clock, Allowlist({}, catalog), retry=RetryConfig(response_timeout_seconds=0.01)
    )
    device = DeviceEntry(device_id="inv1", host="10.0.0.5")
    cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
    endpoint = TransportEndpoint("endpoint-1", device.key.endpoint, cfg, clock, connector=net.connect)
    serializer = AccessSerializer(endpoint, gateway.handler(endpoint), cache_hit=gateway.cache_hit)
    serializer.start()
    gateway.add_device(DeviceBinding(device, endpoint, serializer))
    key = ("inv1", "battery_power")
    cache.put(key, 1.5, measured_at=clock.now(), received_monotonic=clock.monotonic(), origin="transaction")
    started = clock.monotonic()
    clock.advance(age)
    clock.jump_wall(wall_jump)  # a system time step must not change age or validity
    try:
        outcome = await gateway.read_metric("inv1", "battery_power", fresh=False)
    except DeviceUnreachable as exc:
        outcome = exc
    waited = clock.monotonic() - started
    frames = len(net.frames)
    await serializer.stop()
    await endpoint.close()
    return outcome, waited, frames


# Feature: rct-manager, Property 14: cache fields are consistent with each other
@settings(max_examples=100, deadline=None)
@given(
    ttl=st.integers(0, 30),
    grace=st.integers(0, 30),
    age=st.integers(0, 80),
    device_ok=st.booleans(),
    wall_jump=st.integers(-3600, 3600),
)
def test_cache_fields_are_consistent(ttl: int, grace: int, age: int, device_ok: bool, wall_jump: int) -> None:
    outcome, waited, frames = asyncio.run(_read_with_cache(ttl, grace, age, device_ok, wall_jump))
    if age <= ttl:
        assert (outcome.source, outcome.stale, outcome.stale_reason, frames) == ("cache", False, None, 0)
        assert outcome.age_seconds == pytest.approx(age)
    elif device_ok:
        assert (outcome.source, outcome.stale, outcome.stale_reason) == ("device", False, None)
        assert outcome.age_seconds == 0
    elif waited <= ttl + grace:  # the failed attempts consumed time, so the age is taken at the end
        assert (outcome.source, outcome.stale_reason) == ("cache", StaleReason.DEVICE_UNREACHABLE)
        assert outcome.stale is (waited > ttl)
        assert outcome.age_seconds == pytest.approx(waited)
    else:
        assert isinstance(outcome, DeviceUnreachable)
