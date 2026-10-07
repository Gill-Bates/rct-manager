#!/usr/bin/env python3
#
# tests/e2e/fake_inverter.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Simulated RCT inverter for browser E2E runs: answers reads with fixed floats on a local port.

Usage: python -m tests.e2e.fake_inverter PORT   (binds 127.0.0.1 only)
"""

import asyncio
import struct
import sys
from pathlib import Path

from app.catalog.registry import RegistryCatalog
from app.protocol.frames import encode_frame
from app.protocol.stream import StreamParser
from app.protocol.types import Command, DataType
from app.protocol.values import encode_value
from tests.fakes import response_to

VALUES = {"solar_a_power": 1234.5, "solar_b_power": 800.0, "grid_power": -250.0, "battery_soc": 0.55,
          "battery_temperature": 28.5,
          # The second, placeholder tower carries its own distinct soc/temperature so an e2e check
          # can prove the two rendered battery cards show different readings, not duplicates of the
          # primary tower (battery_placeholder_0 has no stack_cycles/soc_target/next_calib
          # counterpart in the catalog, see app/catalog/objects.json; those stay shared by design).
          "battery_placeholder_0_soc": 0.81, "battery_placeholder_0_temperature": 19.0}
# Device-card status badges (Requirement: dashboard shows inverter_state/battery_status2 as text);
# feed_in=13 and the community-observed "balancing active" bit combination exercise the labels.
INT_VALUES = {"inverter_state": 13, "battery_status2": 2304, "battery_cycles": 142}
# battery_module_sn_0..2 of the primary tower and battery_placeholder_0_module_sn_0..1 of a second,
# smaller tower, so the dashboard's dynamic battery-slice stack has something concrete to count per
# tower (module_count 3 and 2) and _battery_tower_present (app/admin/api.py) reports two towers.
STRING_VALUES = {f"battery_module_sn_{i}": f"SIM-{i:03d}" for i in range(3)}
STRING_VALUES.update({f"battery_placeholder_0_module_sn_{i}": f"SIM-P0-{i:03d}" for i in range(2)})


def _default_payload(data_type: DataType) -> bytes:
    """A real device answers an unmodeled register with its own type's zero value, not another
    type's bytes reinterpreted; an unpopulated t_string register decodes to '', never garbage."""
    return b"\x00" if data_type is DataType.STRING else struct.pack(">f", 1.0)


def _by_object_id() -> tuple[dict[int, bytes], dict[int, DataType]]:
    catalog = RegistryCatalog.from_file(Path(__file__).resolve().parents[2] / "app" / "catalog" / "objects.json")
    types = {entry.object_id: entry.data_type for entry in catalog.entries()}
    payloads = {catalog.object_entry(name).object_id: struct.pack(">f", value) for name, value in VALUES.items()}
    for name, value in INT_VALUES.items():
        entry = catalog.object_entry(name)
        payloads[entry.object_id] = encode_value(entry.data_type, value, byte_width=entry.byte_width)
    for name, value in STRING_VALUES.items():
        entry = catalog.object_entry(name)
        payloads[entry.object_id] = encode_value(entry.data_type, value, byte_width=entry.byte_width)
    return payloads, types


PAS_PERIOD = 0x9C8FE559


async def _serve(port: int) -> None:
    values, types = _by_object_id()
    stored: dict[int, bytes] = {}  # values the gateway wrote (pas.period readback)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        parser = StreamParser()
        periodic: dict[int, object] = {}

        def payload_for(object_id: int) -> bytes:
            if object_id in stored:
                return stored[object_id]
            if object_id in values:
                return values[object_id]
            return _default_payload(types.get(object_id, DataType.FLOAT))

        async def push() -> None:
            while True:
                await asyncio.sleep(1)
                for object_id, frame in list(periodic.items()):
                    writer.write(encode_frame(response_to(frame, payload_for(object_id))))
                await writer.drain()

        task = asyncio.create_task(push())
        try:
            while data := await reader.read(4096):
                for frame in parser.feed(data):
                    if frame.command in (Command.WRITE, Command.LONG_WRITE) and frame.object_id == PAS_PERIOD:
                        stored[frame.object_id] = frame.payload  # the real device never answers WRITE
                    elif frame.command in (Command.READ, Command.READ_PERIODICALLY):
                        if frame.command is Command.READ_PERIODICALLY:
                            periodic[frame.object_id] = frame
                        writer.write(encode_frame(response_to(frame, payload_for(frame.object_id))))
                        await writer.drain()
        finally:
            task.cancel()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_serve(int(sys.argv[1])))
