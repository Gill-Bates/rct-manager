#!/usr/bin/env python3
#
# tests/test_periodic_properties.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Regression: a partial periodic registration must not count as available."""

import asyncio
from datetime import timedelta

import pytest

from app.catalog.registry import RegistryCatalog
from app.config import DeviceKey, EndpointKey, FreshPeriodicMode
from app.errors import DeviceUnreachable, FreshNotAvailable, QueueFullError
from app.observability.names import prometheus_name
from app.protocol.frames import Frame, encode_frame
from app.protocol.types import Command, DataType, FrameKind
from app.protocol.values import encode_value
from app.scheduling.periodic import (
    PAS_PERIOD_OBJECT_ID,
    RETRY_BASE_SECONDS,
    PeriodicManager,
)
from app.scheduling.serializer import AccessSerializer
from app.transport.endpoint import EndpointConfig, TransportEndpoint
from app.transport.types import SendOutcome, TransactionOrigin, TransactionRequest, TransactionResult, make_frame
from tests.api_helpers import make_settings, running_app
from tests.conftest import AutoClock
from tests.fakes import FakeNetwork

FAILING_OBJECT_ID = 0x2222


@pytest.fixture(autouse=True)
def _no_background_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # The app's own refresh loop reads stale values on a real 10 s timer; tests here drive
    # refresh_stale_periodic by hand and count the reads, so the loop must not race them.
    monkeypatch.setattr("app.api.app_factory._REFRESH_CYCLE_SECONDS", 1e9)


def test_partial_registration_leaves_available_false_until_retried() -> None:
    async def scenario() -> tuple[bool, bool, int]:
        clock = AutoClock()
        key = EndpointKey("10.0.0.5", 8899)

        def behavior(frame):
            # Every request answers except the one for the object id chosen to fail.
            if frame.object_id == FAILING_OBJECT_ID:
                return "ignore"
            return "respond"

        net = FakeNetwork(clock, behavior=behavior)
        cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
        endpoint = TransportEndpoint("endpoint-1", key, cfg, clock, connector=net.connect)

        async def handler(request):
            return await endpoint.execute(request)

        serializer = AccessSerializer(endpoint, handler, queue_max_length=10, queue_max_wait_seconds=10)
        serializer.start()
        device = DeviceKey(key)
        manager = PeriodicManager(endpoint, serializer, device, [0x1111, FAILING_OBJECT_ID, 0x3333], 30, clock)

        first = await manager.setup()
        registrations_after_partial = manager.registrations
        failure = manager.last_failure

        # Fix the device response and retry: ensure() must redo the full setup, not skip it.
        net.behavior = lambda frame: "respond"
        second = await manager.ensure()
        return first, second, registrations_after_partial, failure

    first, second, registrations_after_partial, failure = asyncio.run(scenario())
    assert first is False
    assert failure is not None  # the retry warning must name a reason
    # The timeout on the failing object drops the connection (Requirement 9.18), so the next
    # object in this same setup round reconnects on a new epoch. A setup round belongs to one
    # epoch: the mid-round reconnect discards every registration made so far
    # instead of keeping ones made on the now-dead connection.
    assert registrations_after_partial == 0
    assert second is True


def test_reconnect_during_setup_discards_registrations_from_the_old_connection() -> None:
    """A setup round must not mix registrations made on different connections."""

    async def scenario() -> tuple[bool, int, bool]:
        clock = AutoClock()
        key = EndpointKey("10.0.0.5", 8899)
        net = FakeNetwork(clock)
        cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
        endpoint = TransportEndpoint("endpoint-1", key, cfg, clock, connector=net.connect)

        async def handler(request):
            return await endpoint.execute(request)

        serializer = AccessSerializer(endpoint, handler, queue_max_length=10, queue_max_wait_seconds=10)
        serializer.start()
        device = DeviceKey(key)
        manager = PeriodicManager(endpoint, serializer, device, [0x1111, 0x3333], 30, clock)

        real_submit = serializer.submit

        async def submit_and_bump_epoch_once(request):
            result = await real_submit(request)
            if request.frame.object_id == 0x1111:
                endpoint._epoch += 1  # simulate a reconnect between two registrations of this round
            return result

        serializer.submit = submit_and_bump_epoch_once
        ok = await manager.setup()
        return ok, manager.registrations, manager.is_registered(0x1111)

    ok, registrations, old_object_registered = asyncio.run(scenario())
    assert ok is False
    assert registrations == 0
    assert old_object_registered is False


def test_setup_that_throws_mid_registration_leaves_no_endpoint_registration() -> None:
    """A submit() that throws after a route was pre-registered must roll the registration back,
    not leave the demux routing a value frame for an object the manager considers unregistered."""

    async def scenario() -> tuple[bool, int, bool, int]:
        clock = AutoClock()
        key = EndpointKey("10.0.0.5", 8899)
        net = FakeNetwork(clock)
        cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
        endpoint = TransportEndpoint("endpoint-1", key, cfg, clock, connector=net.connect)

        async def handler(request):
            return await endpoint.execute(request)

        serializer = AccessSerializer(endpoint, handler, queue_max_length=10, queue_max_wait_seconds=10)
        serializer.start()
        manager = PeriodicManager(endpoint, serializer, DeviceKey(key), [0x1111, 0x3333], 30, clock)

        real_submit = serializer.submit

        async def submit_or_throw(request):
            # Let pas.period and the first registration through; throw on the second registration,
            # by which point object 0x1111 is already registered on the endpoint.
            if request.frame.command is Command.READ_PERIODICALLY and request.frame.object_id == 0x3333:
                raise RuntimeError("transport blew up mid-registration")
            return await real_submit(request)

        serializer.submit = submit_or_throw
        ok = await manager.setup()
        return ok, endpoint._demux.periodic_count(), manager.available, manager.registrations

    ok, leftover, available, registrations = asyncio.run(scenario())
    assert ok is False and available is False
    assert leftover == 0, "a throwing setup left periodic routes registered on the endpoint"
    assert registrations == 0


def _manager(clock, net, ids, interval=30):
    key = EndpointKey("10.0.0.5", 8899)
    cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
    endpoint = TransportEndpoint("endpoint-1", key, cfg, clock, connector=net.connect)

    async def handler(request):
        return await endpoint.execute(request)

    serializer = AccessSerializer(endpoint, handler, queue_max_length=10, queue_max_wait_seconds=10)
    serializer.start()
    return PeriodicManager(endpoint, serializer, DeviceKey(key), ids, interval, clock)


def _pas_writes(net) -> int:
    return sum(1 for _, f in net.frames if f.command is Command.WRITE and f.object_id == PAS_PERIOD_OBJECT_ID)


async def test_dropped_connection_invalidates_periodic_registrations() -> None:
    clock = AutoClock()
    net = FakeNetwork(clock)
    manager = _manager(clock, net, [0x1111])
    endpoint = manager._endpoint
    assert await manager.setup()
    assert manager.is_registered(0x1111)
    assert endpoint._demux.periodic_count() == 1

    await endpoint._drop_connection(DeviceUnreachable())
    assert endpoint._demux.periodic_count() == 0
    assert not manager.is_registered(0x1111)
    assert manager.registered_object_ids == ()
    net.fail_connects = 1
    assert not await manager.ensure()
    net.fail_connects = 0
    request = TransactionRequest(
        manager._key, make_frame(None, Command.READ, 0x1111), TransactionOrigin.CALLER, "read", clock.now()
    )
    assert (await endpoint.execute(request)).ok
    response = Frame(Command.RESPONSE, 0x1111, b"\x00")
    assert endpoint._demux.classify(response, clock.monotonic()) is FrameKind.UNEXPECTED
    assert not manager.is_registered(0x1111)
    assert await manager.setup()
    assert manager.is_registered(0x1111)


async def test_fresh_reject_uses_live_partial_registrations(monkeypatch) -> None:
    settings = make_settings(enable_periodic_reads=False, fresh_periodic_mode=FreshPeriodicMode.REJECT)
    entries = [
        entry for entry in RegistryCatalog.from_file(settings.object_registry_path).entries()
        if entry.preselected and entry.data_type is DataType.FLOAT
    ][:2]
    registered, failed = entries
    async with running_app(settings) as harness:
        gateway = harness.runtime.gateway
        binding = gateway._device("main")
        manager = PeriodicManager(
            binding.endpoint, binding.serializer,
            DeviceKey(EndpointKey(binding.entry.host, binding.entry.port)),
            [entry.object_id for entry in entries], 30, gateway._clock,
        )
        binding.periodic = manager
        submit = binding.serializer.submit

        async def partial_submit(request):
            if request.frame.command is Command.READ_PERIODICALLY and request.frame.object_id == failed.object_id:
                return TransactionResult(SendOutcome(False, None), error=QueueFullError())
            return await submit(request)

        monkeypatch.setattr(binding.serializer, "submit", partial_submit)
        assert not await manager.setup()
        assert manager.registrations == 1
        assert manager.is_registered(registered.object_id)
        assert not manager.is_registered(failed.object_id)
        with pytest.raises(FreshNotAvailable):
            await gateway.read_metric("main", registered.name, fresh=True)
        assert (await gateway.read_metric("main", failed.name, fresh=True)).source == "device"
        await binding.endpoint._drop_connection(DeviceUnreachable())
        assert not gateway._is_periodic(binding, registered)
        assert (await gateway.read_metric("main", registered.name, fresh=True)).source == "device"
        assert not gateway._is_periodic(binding, registered)


async def test_failed_reconnect_setup_preserves_required_period_reset() -> None:
    clock = AutoClock()
    net = FakeNetwork(clock)
    manager = _manager(clock, net, [0x1111])
    assert await manager.setup()
    await manager._endpoint._drop_connection(DeviceUnreachable())
    net.fail_connects = 2
    assert not await manager.ensure()
    assert manager.period_enabled
    assert not await manager.teardown()
    assert manager.period_enabled
    net.fail_connects = 0
    assert await manager.teardown()
    assert not manager.period_enabled
    assert net.payloads[PAS_PERIOD_OBJECT_ID] == encode_value(DataType.UINT32, 0)
    assert _pas_writes(net) == 2


def test_silent_write_is_confirmed_by_readback_and_registers() -> None:
    async def scenario():
        clock = AutoClock()
        net = FakeNetwork(clock)  # WRITE is never answered, like the real device
        manager = _manager(clock, net, [0x1111, 0x3333])
        return await manager.setup(), manager, net

    ok, manager, net = asyncio.run(scenario())
    assert ok and manager.available and manager.period_enabled and manager.registrations == 2
    assert net.payloads[PAS_PERIOD_OBJECT_ID] == encode_value(DataType.UINT32, 30)
    assert _pas_writes(net) == 1


def test_unconfirmed_pas_period_is_not_available_and_backs_off() -> None:
    async def scenario():
        clock = AutoClock()
        net = FakeNetwork(clock)
        net.freeze_writes = True  # the interval never changes on the device
        net.payloads[PAS_PERIOD_OBJECT_ID] = encode_value(DataType.UINT32, 0)
        manager = _manager(clock, net, [0x1111])
        for _ in range(30):  # 30 loop ticks of 10 s
            await manager.ensure()
            clock.advance(10)
        return manager, net

    manager, net = asyncio.run(scenario())
    assert not manager.available
    assert 1 < _pas_writes(net) < 10  # exponential backoff instead of one write per tick


def test_concurrent_ensure_runs_one_setup() -> None:
    async def scenario():
        clock = AutoClock()
        net = FakeNetwork(clock)
        manager = _manager(clock, net, [0x1111])
        await asyncio.gather(manager.ensure(), manager.ensure(), manager.ensure())
        return net

    assert _pas_writes(asyncio.run(scenario())) == 1


def test_reconnect_after_outage_ends_the_long_setup_backoff() -> None:
    """A lost connection that comes back must not wait out the grown retry delay (up to 300 s)."""

    async def scenario():
        clock = AutoClock()
        net = FakeNetwork(clock)
        manager = _manager(clock, net, [0x1111])
        net.fail_connects = 10**6  # device unreachable: setup fails and the backoff grows
        while manager._consecutive_failures < 6:  # loop ticks of 10 s, like the periodic loop
            await manager.ensure()
            clock.advance(10)
        assert not manager.available and manager._retry_delay > RETRY_BASE_SECONDS * 4
        failed_at = clock.monotonic()
        net.fail_connects = 0  # device is back; any read re-establishes the connection
        read = TransactionRequest(
            manager._key, make_frame(None, Command.READ, 0x1111), TransactionOrigin.CALLER, "read", clock.now()
        )
        assert (await manager._endpoint.execute(read)).ok
        clock.advance(RETRY_BASE_SECONDS + 1)
        return await manager.ensure(), clock.monotonic() - failed_at

    ok, elapsed = asyncio.run(scenario())
    assert ok
    assert elapsed < 60


async def test_values_reach_metrics_after_setup_against_a_silent_write_device() -> None:
    settings = make_settings()
    catalog = RegistryCatalog.from_file(settings.object_registry_path)
    entry = next(e for e in catalog.entries() if e.preselected and e.data_type is DataType.FLOAT)
    async with running_app(settings, settle=False) as h:
        bindings = [h.runtime.gateway._device(d) for d in h.runtime.devices]
        async with asyncio.timeout(5):
            while not all(b.periodic is not None and b.periodic.available for b in bindings):
                await asyncio.sleep(0.01)
        h.net.push(Frame(Command.RESPONSE, entry.object_id, encode_value(DataType.FLOAT, 1.5)))
        await asyncio.sleep(0.05)
        body = (await h.client.get("/metrics")).text
    assert _lines(body, prometheus_name(entry), " 1.5"), body


async def _registered(h) -> None:
    bindings = [h.runtime.gateway._device(d) for d in h.runtime.devices]
    async with asyncio.timeout(5):
        while not all(b.periodic is not None and b.periodic.available for b in bindings):
            await asyncio.sleep(0.01)


def _float_entry(settings):
    catalog = RegistryCatalog.from_file(settings.object_registry_path)
    return next(e for e in catalog.entries() if e.preselected and e.data_type is DataType.FLOAT)


def _reads(net, start: int, object_id: int) -> int:
    return sum(1 for _, f in net.frames[start:] if f.command is Command.READ and f.object_id == object_id)


def _lines(body: str, metric_name: str, value: str) -> list[str]:
    return [
        ln
        for ln in body.splitlines()
        if ln.startswith(metric_name) and 'device="main"' in ln and ln.endswith(value)
    ]


async def test_silent_value_is_refreshed_by_read_and_shows_the_new_device_value() -> None:
    # pas.period 1 s: refresh after 2 s without update, freshness window 3 s (Requirement 17.27, 17.29).
    settings = make_settings(periodic_interval_seconds=1, cache_ttl_seconds=0.05, cache_grace_seconds=0.5)
    entry = _float_entry(settings)
    async with running_app(settings, settle=False) as h:
        h.net.payloads[entry.object_id] = encode_value(DataType.FLOAT, 2.5)
        await _registered(h)
        gateway = h.runtime.gateway
        metric_name = prometheus_name(entry)
        assert _lines((await h.client.get("/metrics")).text, metric_name, " 2.5")
        h.net.payloads[entry.object_id] = encode_value(DataType.FLOAT, 3.5)  # device changes, pushes nothing
        await asyncio.sleep(2.2)
        start = len(h.net.frames)
        before = h.runtime.gateway._device("main").serializer.budget.remaining()
        assert await gateway.refresh_stale_periodic("main") >= 1
        assert _reads(h.net, start, entry.object_id) == 1
        assert h.runtime.gateway._device("main").serializer.budget.remaining() == before  # budget-exempt
        body = (await h.client.get("/metrics")).text
        assert _lines(body, metric_name, " 3.5") and not _lines(body, metric_name, " 2.5")
        assert "rct_device_metric_age_seconds" not in "".join(
            ln for ln in body.splitlines() if 'metric="' + entry.name in ln or entry.name in ln
        ).replace("rct_device_" + entry.name, "")


async def test_unrefreshable_value_ages_visibly_then_leaves_metrics() -> None:
    settings = make_settings(periodic_interval_seconds=1, cache_ttl_seconds=0.05, cache_grace_seconds=1.0)
    entry = _float_entry(settings)
    async with running_app(settings, settle=False) as h:
        h.net.payloads[entry.object_id] = encode_value(DataType.FLOAT, 2.5)
        await _registered(h)
        metric_name = prometheus_name(entry)
        # From now on the device answers no read of this object id and pushes nothing.
        h.net.behavior = lambda f: "ignore" if f.object_id == entry.object_id else "respond"
        await asyncio.sleep(2.2)
        assert _lines((await h.client.get("/metrics")).text, metric_name, " 2.5")  # inside the 3 s window: fresh
        await asyncio.sleep(1.0)  # window exceeded, grace running
        grace = (await h.client.get("/metrics")).text
        await asyncio.sleep(1.2)  # window plus grace exceeded
        gone = (await h.client.get("/metrics")).text
        # A refresh without any device answer fails quietly, stops after two failures, changes nothing.
        h.net.behavior = lambda f: "ignore"
        assert await h.runtime.gateway.refresh_stale_periodic("main") == 2
    assert _lines(grace, metric_name, " 2.5")
    assert any(ln.startswith("rct_device_metric_age_seconds") and 'device="main"' in ln for ln in grace.splitlines())
    assert not _lines(gone, metric_name, " 2.5")


async def test_pushed_values_cause_no_refresh_reads_and_the_cycle_is_limited() -> None:
    settings = make_settings(periodic_interval_seconds=1, device_budget_transactions=3)
    entry = _float_entry(settings)
    async with running_app(settings, settle=False) as h:
        await _registered(h)
        gateway = h.runtime.gateway
        # The startup heartbeat, name and serial reads are not part of the refresh cycle; under load
        # the serial read (queued right after the name) would land inside the counted window.
        async with asyncio.timeout(5):
            while (
                gateway._device("main").last_heartbeat_at is None
                or gateway.reported_name("main") is None
                or gateway.reported_serial("main") is None
            ):
                await asyncio.sleep(0.01)
        start = len(h.net.frames)
        for _ in range(5):  # the device keeps pushing this one value
            h.net.push(Frame(Command.RESPONSE, entry.object_id, encode_value(DataType.FLOAT, 1.0)))
            await asyncio.sleep(0.5)
        assert await gateway.refresh_stale_periodic("main", limit=3) == 3
        reads = [f for _, f in h.net.frames[start:] if f.command is Command.READ]
        assert len(reads) == 3 and all(f.object_id != entry.object_id for f in reads)
        assert await gateway.refresh_stale_periodic("main", limit=3) >= 1  # the next oldest ones
        assert _reads(h.net, start, entry.object_id) == 0
        assert h.runtime.gateway._device("main").serializer.budget.remaining() == 3  # nothing charged
        await asyncio.sleep(0)
        total = len([1 for _, f in h.net.frames[start:] if f.command is Command.READ])
        # 8+1, not 7+1: the device-card wiring now adds two dashboard-only metrics
        # (heat_sink_temperature, battery_temperature) to the periodic set instead of one, so one
        # more non-pushed id needs its own read within the same "each id once per interval" bound.
        assert await gateway.refresh_stale_periodic("main", limit=8) + total <= 8 + 1


async def test_unanswered_register_is_confirmed_after_repeated_failed_refreshes_and_cleared_on_success() -> None:
    settings = make_settings(periodic_interval_seconds=1, cache_ttl_seconds=0.05, cache_grace_seconds=0.5)
    entry = _float_entry(settings)
    async with running_app(settings, settle=False) as h:
        await _registered(h)
        gateway = h.runtime.gateway
        real_read = gateway._read_into_cache
        answer = False

        async def read(binding, e, key, *args, **kwargs):
            return await real_read(binding, e, key, *args, **kwargs) if answer or e.name != entry.name else None

        gateway._read_into_cache = read
        h.net.behavior = lambda f: "ignore" if f.object_id == entry.object_id else "respond"
        for _ in range(3):
            assert not gateway.read_failed("main", entry.name)
            await asyncio.sleep(2.2)
            await gateway.refresh_stale_periodic("main", limit=64)
        assert gateway.read_failed("main", entry.name)
        answer = True
        h.net.behavior = lambda f: "respond"
        await asyncio.sleep(1.1)
        await gateway.refresh_stale_periodic("main", limit=64)
        assert not gateway.read_failed("main", entry.name)


async def test_readiness_and_diagnostics_show_a_lost_periodic_registration_and_crc_drops() -> None:
    async with running_app() as h:
        await _registered(h)
        binding = h.runtime.gateway._device("main")
        body = (await h.client.get("/api/v1/readiness")).json()
        main = next(d for d in body["devices"] if d["device_id"] == "main")
        assert main["periodic_available"] is True and main["periodic_setup_failures"] == 0

        # A CRC-damaged frame is dropped and counted, never delivered.
        wire = bytearray(encode_frame(Frame(Command.RESPONSE, 0x1111, b"\x01\x02\x03\x04")))
        wire[-3] ^= 0x01
        for writer in h.net.writers:
            writer.deliver(bytes(wire))
        await asyncio.sleep(0.05)
        assert binding.endpoint.counters.crc_errors == 1

        await binding.endpoint._drop_connection(DeviceUnreachable())
        status = h.runtime.gateway.device_status("main")
        assert status.periodic_available is False  # the registration died with the connection
        transport = h.runtime.gateway.transports()[0]
        assert transport.crc_errors == 1
        assert transport.connection_epoch >= 1


async def test_reconnect_flips_periodic_available_and_epoch_and_health_stays_a_process_check() -> None:
    async with running_app() as h:
        await _registered(h)
        binding = h.runtime.gateway._device("main")
        endpoint = binding.endpoint
        epoch_before = endpoint.connection_epoch

        await endpoint._drop_connection(DeviceUnreachable())
        status = h.runtime.gateway.device_status("main")
        assert status.periodic_available is False
        assert (await h.client.get("/health")).status_code == 200  # process check only

        assert await binding.periodic.ensure()  # re-registers on a new connection
        assert endpoint.connection_epoch > epoch_before
        assert h.runtime.gateway.device_status("main").periodic_available is True
        info = h.runtime.gateway.transports()[0]
        assert info.connection_epoch == endpoint.connection_epoch
        assert info.periodic_setup_failures["main"] == 0 and info.periodic_last_failure["main"] is None


def test_failed_registrations_are_counted_named_and_cleared_by_a_successful_one() -> None:
    async def scenario() -> list:
        clock = AutoClock()
        net = FakeNetwork(clock, behavior=lambda f: "ignore" if f.object_id == FAILING_OBJECT_ID else "respond")
        manager = _manager(clock, net, [0x1111, FAILING_OBJECT_ID])
        seen = []
        assert not await manager.ensure()
        seen.append((manager.consecutive_failures, manager.last_failure, manager.live))
        clock.advance(RETRY_BASE_SECONDS * 2 + 1)
        assert not await manager.ensure()
        seen.append((manager.consecutive_failures, manager.last_failure, manager.live))
        net.behavior = lambda f: "respond"
        clock.advance(RETRY_BASE_SECONDS * 4 + 1)
        assert await manager.ensure()
        seen.append((manager.consecutive_failures, manager.last_failure, manager.live))
        return seen

    first, second, third = asyncio.run(scenario())
    assert first[0] == 1 and first[1] and not first[2]
    assert second[0] == 2 and second[1] and not second[2]
    assert third[0] == 0 and third[2]


def test_setup_during_shutdown_is_not_a_warning_with_traceback(caplog: pytest.LogCaptureFixture) -> None:
    """A serializer that stopped accepting work (SIGINT) makes the setup abort quietly."""

    async def scenario() -> tuple[bool, str | None]:
        clock = AutoClock()
        key = EndpointKey("10.0.0.5", 8899)
        net = FakeNetwork(clock, behavior=lambda frame: "respond")
        cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
        endpoint = TransportEndpoint("endpoint-1", key, cfg, clock, connector=net.connect)

        async def handler(request):
            return await endpoint.execute(request)

        serializer = AccessSerializer(endpoint, handler, queue_max_length=10, queue_max_wait_seconds=10)
        serializer.start()
        serializer.stop_accepting()
        manager = PeriodicManager(endpoint, serializer, DeviceKey(key), [0x1111], 30, clock)
        return await manager.setup(), manager.last_failure

    caplog.set_level("INFO", logger="app.scheduling.periodic")
    ok, failure = asyncio.run(scenario())
    assert ok is False and failure == "device access is shutting down"
    assert not [r for r in caplog.records if r.levelname == "WARNING" or r.exc_info]
