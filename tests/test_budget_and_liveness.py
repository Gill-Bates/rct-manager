#!/usr/bin/env python3
#
# tests/test_budget_and_liveness.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Budget accounting (Requirement 6.10 to 6.12) and per-device liveness (Requirement 16)."""

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.allowlist import Allowlist
from app.cache import MemoryCache
from app.catalog.registry import RegistryCatalog
from app.config import (
    SYSTEM_READBACK_TIMEOUT_SECONDS,
    DeviceEntry,
    DeviceKey,
    EndpointKey,
)
from app.errors import BudgetExhausted, DeviceApiError, QueueFullError, QueueTimeout
from app.gateway.rct import DeviceBinding, RctGateway
from app.protocol.frames import Frame
from app.protocol.types import Command
from app.scheduling.budget import WorkBudget
from app.scheduling.heartbeat import Heartbeat, LivenessSource
from app.scheduling.retry import RetryConfig
from app.scheduling.serializer import AccessSerializer
from app.transport.endpoint import EndpointConfig, TransportEndpoint
from app.transport.types import (
    SendOutcome,
    TransactionOrigin,
    TransactionRequest,
    TransactionResult,
)
from tests.conftest import AutoClock, ManualClock
from tests.fakes import FakeNetwork

_REGISTRY = Path(__file__).resolve().parent / "fixtures" / "objects.json"
KEY = EndpointKey("10.0.0.5", 8899)


def _request(serializer: AccessSerializer, **kwargs) -> TransactionRequest:
    request = TransactionRequest(DeviceKey(KEY), Frame(Command.READ, 1), TransactionOrigin.CALLER, "read", **kwargs)
    request.charge = serializer.reserve_budget(1)
    return request


def test_refund_removes_exactly_the_reserved_stamps() -> None:
    clock = ManualClock()
    budget = WorkBudget(3, 10, clock)
    first = budget.try_consume(1)
    clock.advance(1)
    second = budget.try_consume(1)
    assert first is not None and second is not None
    first.release()
    assert list(budget._stamps) == [1001.0]  # the older stamp went, not the newest
    second.commit()
    second.release()
    assert budget.remaining() == 2  # a started transaction stays charged
    assert budget.try_consume(3) is None  # all or nothing


async def _skipped_and_blocked(queue_max_length: int = 32) -> tuple[WorkBudget, AccessSerializer, asyncio.Event]:
    release = asyncio.Event()

    async def handler(request):
        await release.wait()
        return TransactionResult(SendOutcome(True, None), frame=Frame(Command.RESPONSE, 1))

    budget = WorkBudget(5, 100, AutoClock())
    serializer = AccessSerializer(
        SimpleNamespace(),
        handler,
        queue_max_length=queue_max_length,
        queue_max_wait_seconds=0.05,
        budget=budget,
        cache_hit=lambda r: r.recheck_cache,
    )
    serializer.start()
    return budget, serializer, release


def test_budget_comes_back_when_the_cache_recheck_skips() -> None:
    async def scenario() -> tuple[int, bool]:
        budget, serializer, _ = await _skipped_and_blocked()
        result = await serializer.submit(_request(serializer, recheck_cache=True))
        await serializer.stop()
        return budget.remaining(), result.skipped_by_cache

    assert asyncio.run(scenario()) == (5, True)


def test_budget_comes_back_on_queue_timeout_and_cancel_but_not_for_the_running_one() -> None:
    async def scenario() -> list[int]:
        budget, serializer, release = await _skipped_and_blocked()
        running = asyncio.create_task(serializer.submit(_request(serializer)))
        async with asyncio.timeout(1):
            while serializer._current is None:
                await asyncio.sleep(0)
        seen = []
        with pytest.raises(QueueTimeout):
            await serializer.submit(_request(serializer))
        seen.append(budget.remaining())
        waiting = asyncio.create_task(serializer.submit(_request(serializer)))
        async with asyncio.timeout(1):
            while serializer.queue_length() == 0:
                await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        seen.append(budget.remaining())
        release.set()
        await running
        await serializer.stop()
        seen.append(budget.remaining())
        return seen

    assert asyncio.run(scenario()) == [4, 4, 4]


def test_budget_comes_back_when_the_queue_is_full_or_stopped() -> None:
    async def scenario() -> tuple[int, int]:
        budget, serializer, _ = await _skipped_and_blocked(queue_max_length=1)
        tasks = [asyncio.create_task(serializer.submit(_request(serializer))) for _ in range(3)]
        async with asyncio.timeout(1):
            while serializer._current is None:
                await asyncio.sleep(0)
        await serializer.stop()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        return budget.remaining(), sum(isinstance(o, QueueFullError) for o in outcomes)

    remaining, rejected = asyncio.run(scenario())
    assert (remaining, rejected >= 1) == (4, True)  # only the running transaction stays charged


def test_timed_out_entries_free_their_queue_slot_immediately() -> None:
    """P3: a dead, timed-out entry must not keep binding queue capacity until the worker reaches it."""

    async def scenario() -> list[bool]:
        _budget, serializer, release = await _skipped_and_blocked(queue_max_length=1)
        running = asyncio.create_task(serializer.submit(_request(serializer)))
        async with asyncio.timeout(1):
            while serializer._current is None:
                await asyncio.sleep(0)
        timed_out = []
        for _ in range(3):
            with pytest.raises(QueueTimeout):
                await serializer.submit(_request(serializer))
            # If the dead item were still occupying the queue, this immediate re-submit would see
            # QueueFullError instead of waiting out the same max_wait again.
            timed_out.append(serializer.queue_length() == 0)
        release.set()
        await running
        await serializer.stop()
        return timed_out

    assert asyncio.run(scenario()) == [True, True, True]


def test_system_writes_jump_the_queue_and_survive_a_full_queue_and_stop_unblocks_drain() -> None:
    """A restore write must not queue behind caller reads (or time out in them); stop() must not
    leave drain() waiting for items it removed.
    """

    async def scenario() -> list[str]:
        _budget, serializer, release = await _skipped_and_blocked(queue_max_length=1)
        order: list[str] = []
        running = asyncio.create_task(serializer.submit(_request(serializer)))
        async with asyncio.timeout(1):
            while serializer._current is None:
                await asyncio.sleep(0)
        queued_read = asyncio.create_task(serializer.submit(_request(serializer)))
        await asyncio.sleep(0)  # fills the single slot; its 0.05 s wait is about to run out
        write = TransactionRequest(
            DeviceKey(KEY), Frame(Command.WRITE, 1), TransactionOrigin.SYSTEM_WRITE, "write"
        )
        write_task = asyncio.create_task(serializer.submit(write))
        await asyncio.sleep(0.1)  # longer than queue_max_wait_seconds
        assert not write_task.done() and serializer._queue._items[0].request is write
        order.append("write-waits-ahead")
        with pytest.raises(QueueTimeout):
            await queued_read
        release.set()
        await running
        await write_task
        await serializer.stop()
        async with asyncio.timeout(1):
            await serializer.drain()
        order.append("drain-returns")
        return order

    assert asyncio.run(scenario()) == ["write-waits-ahead", "drain-returns"]


def test_priority_write_overtakes_reads_but_never_a_queued_caller_write() -> None:
    """A restore must not be overwritten by an older caller write queued before it."""

    async def scenario() -> list[tuple[str, str]]:
        _budget, serializer, release = await _skipped_and_blocked(queue_max_length=8)
        serializer._max_wait = 5.0
        running = asyncio.create_task(serializer.submit(_request(serializer)))
        async with asyncio.timeout(1):
            while serializer._current is None:
                await asyncio.sleep(0)

        def request(origin: TransactionOrigin, kind: str) -> TransactionRequest:
            return TransactionRequest(DeviceKey(KEY), Frame(Command.WRITE, 1), origin, kind)

        tasks = [
            asyncio.create_task(serializer.submit(request(origin, kind)))
            for origin, kind in (
                (TransactionOrigin.CALLER, "write"),
                (TransactionOrigin.CALLER, "read"),
                (TransactionOrigin.SYSTEM_WRITE, "write"),
            )
        ]
        await asyncio.sleep(0.01)
        order = [(item.request.origin.value, item.request.kind) for item in serializer._queue._items]
        release.set()
        await asyncio.gather(running, *tasks)
        await serializer.stop()
        return order

    assert asyncio.run(scenario()) == [("caller", "write"), ("system_write", "write"), ("caller", "read")]


def test_submit_after_stop_is_refused_instead_of_queued_behind_a_dead_worker() -> None:
    async def scenario() -> str:
        _budget, serializer, _release = await _skipped_and_blocked()
        await serializer.stop()
        with pytest.raises(DeviceApiError) as excinfo:
            await serializer.submit(_request(serializer))
        return str(excinfo.value.code)

    assert asyncio.run(scenario()) == "not_ready"


def test_readback_is_bounded_only_while_the_shutdown_restore_runs() -> None:
    async def scenario() -> list[float | None]:
        gateway, binding, _budget, _net, catalog = await _gateway(5)
        entry = next(iter(catalog.entries()))
        seen: list[float | None] = []
        real_submit = binding.serializer.submit

        async def spy(request):
            if request.kind == "read":
                seen.append(request.read_total_timeout_seconds)
            return await real_submit(request)

        binding.serializer.submit = spy
        await gateway._write(binding, entry, ("inv1", entry.name), b"\x00\x00\x00\x01", system=True)
        gateway.begin_shutdown_restore()
        await gateway._write(binding, entry, ("inv1", entry.name), b"\x00\x00\x00\x01", system=True)
        await binding.serializer.stop()
        return seen

    assert asyncio.run(scenario()) == [None, SYSTEM_READBACK_TIMEOUT_SECONDS]


async def _gateway(limit: int, fail_connects: int = 0):
    clock = AutoClock()
    net = FakeNetwork(clock, fail_connects=fail_connects)
    catalog = RegistryCatalog.from_file(_REGISTRY)
    cache = MemoryCache(30, 30)
    gateway = RctGateway(
        catalog, cache, clock, Allowlist({}, catalog), retry=RetryConfig(response_timeout_seconds=0.01)
    )
    device = DeviceEntry(device_id="inv1", host="10.0.0.5")
    cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
    endpoint = TransportEndpoint("endpoint-1", device.key.endpoint, cfg, clock, connector=net.connect)
    budget = WorkBudget(limit, 1000, clock)
    serializer = AccessSerializer(endpoint, gateway.handler(endpoint), budget=budget, cache_hit=gateway.cache_hit)
    serializer.start()
    binding = DeviceBinding(device, endpoint, serializer)
    gateway.add_device(binding)
    return gateway, binding, budget, net, catalog


def test_invalidated_cache_after_skip_costs_exactly_one_unit() -> None:
    async def scenario() -> tuple[int, int]:
        gateway, binding, budget, net, _ = await _gateway(5)
        binding.serializer._cache_hit = lambda request: True  # the entry vanishes after the recheck
        await gateway.read_metric("inv1", "battery_power", fresh=False)
        await binding.serializer.stop()
        return budget.remaining(), len(net.frames)

    assert asyncio.run(scenario()) == (4, 1)


def test_caller_write_reserves_write_and_readback() -> None:
    async def scenario() -> tuple[int, int]:
        gateway, binding, budget, net, catalog = await _gateway(2)
        entry = next(iter(catalog.entries()))
        await gateway._write(binding, entry, ("inv1", entry.name), b"\x00\x00\x00\x01")
        await binding.serializer.stop()
        return budget.remaining(), len(net.frames)

    assert asyncio.run(scenario()) == (0, 2)


def test_write_is_rejected_before_sending_when_only_one_unit_is_left() -> None:
    async def scenario() -> tuple[int, int]:
        gateway, binding, budget, net, catalog = await _gateway(1)
        entry = next(iter(catalog.entries()))
        with pytest.raises(BudgetExhausted):
            await gateway._write(binding, entry, ("inv1", entry.name), b"\x00\x00\x00\x01")
        await binding.serializer.stop()
        return budget.remaining(), len(net.frames)

    assert asyncio.run(scenario()) == (1, 0)


def test_unsent_readback_unit_is_refunded() -> None:
    async def scenario() -> int:
        gateway, binding, budget, _, catalog = await _gateway(4, fail_connects=1000)
        entry = next(iter(catalog.entries()))
        with pytest.raises(Exception):  # noqa: B017 - any device error: the write never left
            await gateway._write(binding, entry, ("inv1", entry.name), b"\x00\x00\x00\x01")
        await binding.serializer.stop()
        return budget.remaining()

    assert asyncio.run(scenario()) == 3  # the attempted write stays charged, the readback unit is back


def test_heartbeat_transaction_lands_in_the_duration_histogram() -> None:
    """Requirement 20.6: an idle system still fills the histogram - the heartbeat is a transaction."""

    async def scenario() -> tuple[int, float, int]:
        gateway, binding, _, net, catalog = await _gateway(5)
        net.payloads[catalog.object_entry("inverter_state").object_id] = b"\x01"  # t_uint8, not 4 bytes
        assert binding.durations.count == 0  # nothing observed before the first transaction
        read = gateway.heartbeat_read("inv1")
        assert await read() is True  # no HTTP access anywhere in this test
        await binding.serializer.stop()
        return binding.durations.count, binding.durations.total, len(net.frames)

    count, total, frames = asyncio.run(scenario())
    assert count == 1 and frames == 1
    assert total >= 0.0


def test_failed_transaction_is_counted_once() -> None:
    """The error count stays single even though the observation moved into the handler."""

    async def scenario() -> tuple[int, int]:
        gateway, binding, _, _, _ = await _gateway(5, fail_connects=1000)
        with pytest.raises(Exception):  # noqa: B017 - any device error: the connect never succeeds
            await gateway.read_metric("inv1", "battery_power", fresh=True)
        await binding.serializer.stop()
        return binding.errors, binding.durations.count

    errors, observed = asyncio.run(scenario())
    assert (errors, observed) == (1, 1)


def test_one_slave_does_not_hide_a_silent_one() -> None:
    async def scenario() -> tuple[int, int, int, int, bool, LivenessSource | None]:
        clock = AutoClock()
        net = FakeNetwork(clock)
        cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
        endpoint = TransportEndpoint("endpoint-1", KEY, cfg, clock, connector=net.connect)
        a, b = DeviceKey(KEY, 1), DeviceKey(KEY, 2)
        request = TransactionRequest(a, Frame(Command.READ_M, 1, b"", 1), TransactionOrigin.CALLER, "read")
        assert (await endpoint.execute(request)).ok
        probed = []

        async def read() -> bool:
            probed.append(1)
            return True

        beat = Heartbeat(endpoint.counters_for(b), read, clock, interval_seconds=30, failure_threshold=3)
        await beat.tick()
        silent = endpoint.counters_for(b)
        out = (endpoint.counters.transactions, endpoint.counters_for(a).transactions, silent.transactions, len(probed))
        await endpoint.close()
        return (*out, silent.last_success_at is None, beat.liveness_source)

    total, a_count, b_count, probes, b_never, source = asyncio.run(scenario())
    assert (total, a_count, b_count, probes, b_never, source) == (1, 1, 0, 1, True, LivenessSource.HEARTBEAT)
