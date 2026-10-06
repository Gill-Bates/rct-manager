#!/usr/bin/env python3
#
# tests/test_endpoint_lock.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Lock state: magic drops the connection, the probe after the cooldown uses a fresh one (Requirement 8.13)."""

import asyncio
from datetime import timedelta

from app.errors import DeviceMaintenance
from app.protocol.types import BOOTLOADER_MAGIC
from app.transport.endpoint import EndpointConfig, EndpointState, TransportEndpoint
from tests.conftest import ManualClock
from tests.fakes import FakeNetwork
from tests.test_send_gate_properties import KEY, _request

COOLDOWN = 60.0


async def _settle(endpoint: TransportEndpoint) -> None:
    async with asyncio.timeout(2):
        while endpoint.state is not EndpointState.LOCKED:
            await asyncio.sleep(0)


def test_lock_cooldown_probe_release() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        net = FakeNetwork(clock)
        readers: list[asyncio.StreamReader] = []

        async def connect(host: str, port: int):
            reader, writer = await net.connect(host, port)
            readers.append(reader)
            return reader, writer

        cfg = EndpointConfig(min_interval=timedelta(0), bootloader_cooldown_seconds=COOLDOWN)
        endpoint = TransportEndpoint("endpoint-1", KEY, cfg, clock, connector=connect)
        await endpoint.start()
        assert (await endpoint.execute(_request())).ok

        readers[-1].feed_data(BOOTLOADER_MAGIC)
        await _settle(endpoint)
        assert endpoint.maintenance() and endpoint.state is EndpointState.LOCKED
        assert net.open_now == 0  # the connection was torn down
        assert not endpoint.probe_due()
        assert isinstance((await endpoint.execute(_request())).error, DeviceMaintenance)
        assert net.connects == 1

        clock.advance(COOLDOWN)
        assert endpoint.probe_due()
        assert (await endpoint.execute(_request())).ok  # one probe on a new connection
        assert net.connects == 2
        assert not endpoint.maintenance()
        await endpoint.close()

    asyncio.run(scenario())
