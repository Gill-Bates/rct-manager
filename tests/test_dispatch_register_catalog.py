#!/usr/bin/env python3
#
# tests/test_dispatch_register_catalog.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Pins the eight dispatch registers against the shipped catalog and write-policy seed.

Work package 0 of the dispatch safety layer: the object IDs, data types and byte widths of the
registers the dispatch path writes and reads were checked once against the protocol PDF. These
tests nail that result down so a later silent catalog edit turns into a red test instead of a
hardware risk. No object ID is changed here; a deviation is a stop-and-report condition.

The negative assertions pin the four documented evidence gaps: the catalog carries no enum labels
for the SoC strategy, no sign statement for the external battery power setpoint, a float-range
minimum for the receive limit that still permits negative values, and the SoC unit `ratio`. Each
gap is answered on real hardware, per device, not in the catalog.
"""
from pathlib import Path

import pytest

from app.allowlist import Allowlist
from app.catalog.registry import RegistryCatalog

ROOT = Path(__file__).resolve().parents[1]

OBJECTS_PATH = ROOT / "app/catalog/objects.json"
ALLOWLIST_PATH = ROOT / "app/catalog/default_write_allowlist.json"

# name, object_id, data_type -- the pinned register set of the dispatch path.
DISPATCH_REGISTERS = [
    ('power_mng_soc_strategy', 0xF168B748, 't_enum'),
    ('power_mng_battery_power_extern', 0xBD008E29, 't_float'),
    ('power_mng_use_grid_power_enable', 0x36A9E9A6, 't_bool'),
    ('p_rec_lim_1', 0x54829753, 't_float'),
    ('power_mng_soc_target_set', 0xD1DFC969, 't_float'),
    ('battery_soc', 0x959930BF, 't_float'),
    ('grid_power', 0x91617C58, 't_float'),
    ('battery_power', 0x400F015B, 't_float'),
]

FLOAT_MAX = 3.4028234663852886e38


@pytest.fixture(scope='module')
def catalog():
    return RegistryCatalog.from_file(OBJECTS_PATH)


@pytest.fixture(scope='module')
def allowlist(catalog):
    return Allowlist.load(ALLOWLIST_PATH, catalog)


@pytest.mark.parametrize('name,object_id,data_type', DISPATCH_REGISTERS)
def test_dispatch_register_object_id_and_data_type_are_pinned(catalog, name, object_id, data_type):
    entry = catalog.object_entry(name)
    assert entry.object_id == object_id, (
        f"object ID of {name} changed from {object_id:#010X} to {entry.object_id:#010X}; "
        "stop and report, do not adjust the pinned value"
    )
    assert entry.data_type.value == data_type


def test_soc_strategy_enum_is_one_byte_wide(catalog):
    """The strategy write is a single byte; a wider frame would hit a different register layout."""
    assert catalog.object_entry('power_mng_soc_strategy').byte_width == 1


@pytest.mark.parametrize('name,object_id,data_type', DISPATCH_REGISTERS)
def test_dispatch_register_is_writable_in_the_shipped_allowlist(catalog, allowlist, name, object_id, data_type):
    del object_id, data_type
    assert allowlist.entry(name) is not None
    assert catalog.object_entry(name).writable


@pytest.mark.parametrize('name', [
    'power_mng_battery_power_extern',
    'p_rec_lim_1',
    'power_mng_soc_target_set',
    'battery_soc',
    'grid_power',
    'battery_power',
])
def test_float_dispatch_registers_keep_the_full_single_precision_range(allowlist, name):
    entry = allowlist.entry(name)
    assert entry.data_type.value == 't_float'
    assert entry.minimum == pytest.approx(-FLOAT_MAX)
    assert entry.maximum == pytest.approx(FLOAT_MAX)


def test_soc_strategy_allowlist_range_covers_one_unsigned_byte(allowlist):
    entry = allowlist.entry('power_mng_soc_strategy')
    assert entry.data_type.value == 't_enum'
    assert (entry.minimum, entry.maximum) == (0, 255)


def test_use_grid_power_enable_is_a_bare_boolean_in_the_allowlist(allowlist):
    entry = allowlist.entry('power_mng_use_grid_power_enable')
    assert entry.data_type.value == 't_bool'
    assert entry.minimum is None and entry.maximum is None


def test_soc_strategy_carries_no_enum_labels(catalog):
    """Evidence gap 1: the catalog does not say which code activates external control.

    The assumption `2 = External` is unproven and is answered by verification point V-06 before
    WRITE_PATH may become `verified`. A future catalog that does carry labels makes this test red
    on purpose, so the label source is reviewed instead of silently trusted.
    """
    assert catalog.object_entry('power_mng_soc_strategy').enum_labels == {}


def test_external_battery_power_carries_no_sign_statement(catalog):
    """Evidence gap 2: the external setpoint has no documented sign convention.

    `battery_power` (read) states "positive on discharge according to the protocol example", but
    that statement does not carry over to the write register. The sign is answered per device by
    verification points V-07/V-08 (capability BATTERY_POWER_SIGN).
    """
    entry = catalog.object_entry('power_mng_battery_power_extern')
    text = f"{entry.description} {entry.help_text or ''}".lower()
    for word in ('positive', 'negative', 'discharge', 'charge', 'sign'):
        assert word not in text, f"catalog now claims a sign convention for {entry.name}: {text!r}"
    assert entry.unit == 'W'


def test_receive_limit_allowlist_minimum_is_negative(allowlist):
    """Evidence gap 3: the allowlist range does not constrain the receive limit to >= 0.

    `p_rec_lim_1 = 0 W` as an export block is an engineering hypothesis (V-19). The range alone
    neither confirms nor bounds it, so the clamping has to happen in the dispatch path.
    """
    assert allowlist.entry('p_rec_lim_1').minimum < 0


def test_battery_soc_is_declared_as_a_ratio(catalog):
    """Evidence gap 4: SoC is a 0..1 ratio, not a percentage.

    The dispatch path must not mix the two; `power_mng_soc_target_set` carries no unit at all
    (answered by V-17).
    """
    assert catalog.object_entry('battery_soc').unit == 'ratio'
    assert catalog.object_entry('power_mng_soc_target_set').unit == ''
