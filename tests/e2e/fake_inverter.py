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
          # The second battery tower reports its own SoC and temperature. The values differ from the
          # first tower's on purpose: a card that falls back to the shared battery_* names would then
          # show two identical towers, which the browser test asserts against.
          "battery_placeholder_0_soc": 0.42, "battery_placeholder_0_temperature": 24.5}
# Device-card status badges (Requirement: dashboard shows inverter_state/battery_status2 as text);
# feed_in=13 and the community-observed "balancing active" bit combination exercise the labels.
INT_VALUES = {"inverter_state": 13, "battery_status2": 2304, "battery_cycles": 142,
              # Second tower's own status: "Synchronizing", deliberately different from tower 1's
              # "Balancing active" so a card that reuses the first tower's status is visible.
              "battery_placeholder_0_status2": 2}
# Two simulated towers with different module counts, so the dashboard has to render each tower from
# its own data: tower 1 has 5 modules (a Power Battery 9.6), tower 2 has 4 (a 7.6). The remaining
# module_sn slots of each tower stay unmodeled and answer with a t_string zero value, i.e. the empty
# string an unpopulated slot really returns - so the derived counts are 5 and 4, not 7. Seven slots
# exist in the catalog; at most 6 modules exist in the documented hardware.
STRING_VALUES = {
    **{f"battery_module_sn_{i}": f"SIM-{i:03d}" for i in range(5)},
    **{f"battery_placeholder_0_module_sn_{i}": f"SIM-B{i:03d}" for i in range(4)},
}


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
