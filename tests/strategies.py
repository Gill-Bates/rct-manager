#!/usr/bin/env python3
#
# tests/strategies.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Hypothesis strategies shared by the protocol property tests."""

from hypothesis import strategies as st

from app.protocol.frames import Frame
from app.protocol.types import LONG_COMMANDS, PLANT_BIT, Command


@st.composite
def frames(draw: st.DrawFn) -> Frame:
    command = draw(st.sampled_from(list(Command)))
    plant = bool(int(command) & PLANT_BIT)
    limit = 300 if command in LONG_COMMANDS else 100
    return Frame(
        command=command,
        object_id=draw(st.integers(0, 2**32 - 1)),
        payload=draw(st.binary(max_size=limit)),
        plant_address=draw(st.integers(0, 2**32 - 1)) if plant else None,
    )
