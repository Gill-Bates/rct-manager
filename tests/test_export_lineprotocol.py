#!/usr/bin/env python3
#
# tests/test_export_lineprotocol.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Line protocol formatting and escaping."""

import math

from app.export.lineprotocol import batches, build_lines, escape_key, escape_measurement
from app.observability.exporter import MetricsExporter, _labels


def test_escaping() -> None:
    assert escape_measurement("my table,x") == "my\\ table\\,x"
    assert escape_key("a b,c=d") == "a\\ b\\,c\\=d"
    assert "\n" not in escape_key("a\nb")


def test_groups_fields_by_tag_set_and_writes_floats() -> None:
    samples = [
        ("rct_grid_power", {"device": "main"}, 5.0),
        ("rct_battery_soc", {"device": "main"}, 0.5),
        ("rct_api_requests_total", {}, 7.0),
        ("rct_state", {"device": "main", "state": "on x"}, 1.0),
        ("bad", {"device": "main"}, math.nan),
        ("inf", {"device": "main"}, math.inf),
    ]
    lines = build_lines(samples, "rct", 42)
    assert lines == [
        "rct rct_api_requests_total=7.0 42",
        "rct,device=main rct_battery_soc=0.5,rct_grid_power=5.0 42",
        "rct,device=main,state=on\\ x rct_state=1.0 42",
    ]


def test_empty_tag_values_are_dropped_and_batches_split() -> None:
    assert build_lines([("m", {"device": ""}, 1.0)], "t", 1) == ["t m=1.0 1"]
    chunks = batches([f"l{i}" for i in range(5)], size=2)
    assert chunks == ["l0\nl1\n", "l2\nl3\n", "l4\n"]


def test_labels_keep_pairs_for_the_exporter() -> None:
    assert _labels(device="a").pairs == {"device": "a"}
    assert _labels() == ""
    assert MetricsExporter.collect is not None
