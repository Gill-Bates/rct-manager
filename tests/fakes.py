#!/usr/bin/env python3
#
# tests/fakes.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""In-memory device and connection doubles for the transport tests."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from app.protocol.frames import Frame, encode_frame
from app.protocol.stream import StreamParser
from app.protocol.types import WRITE_COMMANDS, Command

RESPONSE_FOR = {
    Command.READ: Command.RESPONSE,
    Command.WRITE: Command.RESPONSE,
    Command.LONG_WRITE: Command.LONG_RESPONSE,
    Command.READ_PERIODICALLY: Command.RESPONSE,
    Command.READ_M: Command.RESPONSE_M,
    Command.WRITE_M: Command.RESPONSE_M,
    Command.LONG_WRITE_M: Command.LONG_RESPONSE_M,
    Command.READ_PERIODICALLY_M: Command.RESPONSE_M,
}


def response_to(frame: Frame, payload: bytes = b"\x00\x00\x00\x01") -> Frame:
    command = RESPONSE_FOR[frame.command]
    header = 4 if frame.plant_address is None else 8
    if header + len(payload) > 255:
        command = Command.LONG_RESPONSE if frame.plant_address is None else Command.LONG_RESPONSE_M
    return Frame(command, frame.object_id, payload, frame.plant_address)


@dataclass
class FakeNetwork:
    """Counts connections and frames; ``behavior`` decides what the device does per frame."""

    clock: object
    behavior: Callable[[Frame], object] = lambda frame: "respond"
    fail_connects: int = 0
    write_error_at: int | None = None  # 1-based index of the write() call that raises
    drain_error_at: int | None = None
    open_now: int = 0
    max_open: int = 0
    connects: int = 0
    frames: list[tuple[float, Frame]] = field(default_factory=list)
    writes: int = 0
    payloads: dict[int, bytes] = field(default_factory=dict)  # per object id; default is a 4-byte value
    writers: list = field(default_factory=list)
    freeze_writes: bool = False  # the device ignores a write: a readback shows the old value
    answer_writes: bool = False  # the real device never answers WRITE (design.md); tests opt in

    async def connect(self, host: str, port: int):
        self.connects += 1
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise ConnectionRefusedError
        reader = asyncio.StreamReader()
        writer = FakeWriter(self, reader)
        self.writers.append(writer)
        self.open_now += 1
        self.max_open = max(self.max_open, self.open_now)
        return reader, writer

    def push(self, frame: Frame) -> None:
        """Unsolicited frame from the device, e.g. a periodic value after READ PERIODICALLY."""
        for writer in self.writers:
            if not writer.closed:
                writer.deliver(encode_frame(frame))


class FakeWriter:
    def __init__(self, net: FakeNetwork, reader: asyncio.StreamReader) -> None:
        self._net = net
        self._reader = reader
        self._parser = StreamParser()
        self.closed = False

    def deliver(self, data: bytes) -> None:
        self._reader.feed_data(data)

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str):
        return None

    def write(self, data: bytes) -> None:
        net = self._net
        net.writes += 1
        if net.write_error_at == net.writes:
            # Checked before any parsing/payload mutation/_react scheduling: a failed write must
            # not let the fake device "receive" the frame or react to it.
            raise OSError("write failed")
        for frame in self._parser.feed(data):
            net.frames.append((net.clock.monotonic(), frame))
            action = net.behavior(frame)
            if frame.command in WRITE_COMMANDS:
                if not net.answer_writes and action != "drop":
                    action = "ignore"
                if action != "drop" and not net.freeze_writes:
                    net.payloads[frame.object_id] = frame.payload  # the device stores the value anyway
            asyncio.get_running_loop().create_task(self._react(frame, action))

    async def _react(self, frame: Frame, action: object) -> None:
        if isinstance(action, tuple) and action[0] == "delay":
            await self._net.clock.sleep(action[1])
            action = "respond"
        if self.closed:
            return
        if action == "respond":
            payload = self._net.payloads.get(frame.object_id, b"\x00\x00\x00\x01")
            self._reader.feed_data(encode_frame(response_to(frame, payload)))
        elif action == "drop":
            self._reader.feed_eof()

    async def drain(self) -> None:
        if self._net.drain_error_at == self._net.writes:
            raise ConnectionResetError("drain failed")

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._net.open_now -= 1
            self._reader.feed_eof()

    async def wait_closed(self) -> None:
        return None
