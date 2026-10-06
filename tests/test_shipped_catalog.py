#!/usr/bin/env python3
#
# tests/test_shipped_catalog.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Full PDF catalog, scalar write coverage and variable-length writes."""
from pathlib import Path

import pytest

from app.allowlist import Allowlist
from app.catalog.registry import RegistryCatalog
from app.errors import WriteRejected
from app.protocol.frames import encode_frame
from app.protocol.types import Command
from app.transport.types import make_frame

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_catalog_covers_protocol_and_scalar_writes():
    catalog = RegistryCatalog.from_file(ROOT / "app/catalog/objects.json")
    allowed = Allowlist.load(ROOT / "app/catalog/default_write_allowlist.json", catalog)
    assert len(catalog.entries()) == 894
    assert len(allowed) == 893
    for entry in catalog.entries():
        if not entry.writable:
            assert entry.name == 'net_slave_data'
            continue
        sample = {'t_string': 'new name', 't_bool': True}.get(entry.data_type.value, 0)
        allowed.check(entry.name, sample, action=entry.is_action)
    for name, value in [('display_struct_brightness', 255), ('android_description', 'Plant Ä'), ('pas_period', 0)]:
        allowed.check(name, value, action=False)
    allowed.check('com_service', 18, action=True)
    with pytest.raises(WriteRejected):
        allowed.check('com_service', 21, action=True)
    with pytest.raises(WriteRejected):
        allowed.check('android_description', 'a\x00b', action=False)
    with pytest.raises(WriteRejected):
        allowed.check('display_struct_brightness', 256, action=False)


def test_destructive_com_service_actions_are_not_writable_by_default():
    """Security policy: a labeled catalog action is not automatically a safe shipped default.

    The two erase actions must stay denied unless an operator adds them to an own allowlist file.
    """
    catalog = RegistryCatalog.from_file(ROOT / "app/catalog/objects.json")
    allowed = Allowlist.load(ROOT / "app/catalog/default_write_allowlist.json", catalog)
    allowed.check('com_service', 18, action=True)  # a non-destructive action stays writable
    for code, label in [(6, 'erase_parameters_flash'), (11, 'erase_datalog')]:
        try:
            allowed.check('com_service', code, action=True)
        except WriteRejected:
            continue
        pytest.fail(
            f"security policy violated: destructive com_service code {code} ({label}) is writable in the "
            "shipped default allowlist; it must only become writable through an explicit operator allowlist"
        )
    with pytest.raises(WriteRejected):
        allowed.check('com_service', 21, action=True)  # outside the labeled catalog range


@pytest.mark.parametrize('network_id,short_limit', [(None,251), (42,247)])
def test_string_write_switches_to_long_frame(network_id, short_limit):
    for size in (short_limit, short_limit + 1, 1024):
        frame = make_frame(network_id, Command.WRITE, 0xEBC62737, b'a' * size)
        expected = Command.WRITE if size <= short_limit else Command.LONG_WRITE
        assert int(frame.command) == int(expected) | (0x40 if network_id else 0)
        assert encode_frame(frame)


@pytest.mark.parametrize('device_id', ['main', 'slave1'])
async def test_long_string_write_accepts_nul_padded_readback(device_id):
    from tests.api_helpers import WRITE_TOKEN, make_settings, running_app

    settings = make_settings(
        object_registry_path=ROOT / "app/catalog/objects.json",
        write_allowlist_path=ROOT / "app/catalog/default_write_allowlist.json",
        enable_write_support=True,
    )
    value = 'Plant Ä ' * 40
    async with running_app(settings) as h:
        def behavior(frame):
            if frame.object_id == 0xEBC62737 and frame.command in (Command.LONG_WRITE, Command.LONG_WRITE_M):
                h.net.payloads[frame.object_id] = frame.payload + b'\x00\x00'
            return 'respond'
        h.net.behavior = behavior
        response = await h.client.put(
            f'/api/v1/devices/{device_id}/metrics/android_description',
            json={'value': value}, headers={'Authorization': f'Bearer {WRITE_TOKEN}'},
        )
        assert response.status_code == 200, response.text
        assert response.json()['readback_value'] == value


@pytest.mark.parametrize('name,object_id,value,expected', [
    ('power_mng_soc_strategy', 0xF168B748, 2, '02'),
    ('power_mng_soc_target_set', 0xD1DFC969, 0.5, '3f000000'),
    ('power_mng_battery_power_extern', 0xBD008E29, -6000.0, 'c5bb8000'),
    ('power_mng_soc_min', 0xCE266F0F, 0.5, '3f000000'),
    ('power_mng_soc_max', 0x97997C93, 0.5, '3f000000'),
    ('power_mng_soc_charge_power', 0x1D2994EA, 100.0, '42c80000'),
    ('power_mng_soc_charge', 0xBD3A23C3, 0.5, '3f000000'),
    ('p_rec_lim_1', 0x54829753, 6000.0, '45bb8000'),
    ('power_mng_use_grid_power_enable', 0x36A9E9A6, True, '01'),
    ('buf_v_control_power_reduction', 0xFE1AA500, 0.5, '3f000000'),
])
def test_writesupport_reference_parameters(name, object_id, value, expected):
    """Wire samples independently checked against the reference's pinned rctclient 0.0.3."""
    from app.protocol.values import decode_value, encode_value

    catalog = RegistryCatalog.from_file(ROOT / "app/catalog/objects.json")
    entry = catalog.object_entry(name)
    assert entry.object_id == object_id
    allowed = Allowlist.load(ROOT / "app/catalog/default_write_allowlist.json", catalog)
    allowed.check(name, value, action=False)
    wire = encode_value(entry.data_type, value, byte_width=entry.byte_width)
    assert wire.hex() == expected
    assert decode_value(entry.data_type, wire, byte_width=entry.byte_width) == value


def test_all_shipped_enums_use_reference_byte_width():
    catalog = RegistryCatalog.from_file(ROOT / "app/catalog/objects.json")
    allowed = Allowlist.load(ROOT / "app/catalog/default_write_allowlist.json", catalog)
    enums = [e for e in catalog.entries() if e.data_type.value == 't_enum']
    assert len(enums) == 15
    for entry in enums:
        assert entry.byte_width == 1
        with pytest.raises(WriteRejected):
            allowed.check(entry.name, 256, action=entry.is_action)
