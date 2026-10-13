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
from app.gateway.vendor import SlaveDiscovery
from app.protocol.frames import Frame
from app.protocol.types import Command
from app.scheduling.budget import WorkBudget
from app.scheduling.heartbeat import Heartbeat, LivenessSource
from app.scheduling.retry import RetryConfig, execute_read
from app.scheduling.serializer import AccessSerializer
from app.transport.counters import DeviceCounters
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


def test_restore_barrier_drops_queued_caller_writes_and_refuses_new_ones_until_lowered() -> None:
    """Race: caller write A queued, restore R starts. A must never run after R restored the device."""

    async def scenario() -> list[str]:
        _budget, serializer, release = await _skipped_and_blocked(queue_max_length=8)
        serializer._max_wait = 5.0
        key = DeviceKey(KEY)
        running = asyncio.create_task(serializer.submit(_request(serializer)))
        async with asyncio.timeout(1):
            while serializer._current is None:
                await asyncio.sleep(0)

        def caller(kind: str) -> TransactionRequest:
            return TransactionRequest(key, Frame(Command.WRITE, 1), TransactionOrigin.CALLER, kind)

        queued_write = asyncio.create_task(serializer.submit(caller("write")))
        await asyncio.sleep(0.01)
        outcome: list[str] = []
        await serializer.raise_barrier(key)
        with pytest.raises(DeviceApiError) as dropped:
            await queued_write  # rejected at the barrier, not executed after the restore
        outcome.append(dropped.value.code)
        with pytest.raises(DeviceApiError) as refused:
            await serializer.submit(caller("write"))
        outcome.append(refused.value.code)
        read = asyncio.create_task(serializer.submit(caller("read")))  # reads pass the barrier
        await asyncio.sleep(0.01)
        outcome.append("read-queued" if serializer.queue_length() == 1 else "read-lost")
        release.set()
        await asyncio.gather(running, read)
        serializer.lower_barrier(key)
        await serializer.submit(caller("write"))  # accepted again
        outcome.append("write-after-barrier-ok")
        await serializer.stop()
        return outcome

    assert asyncio.run(scenario()) == [
        "restore_in_progress", "restore_in_progress", "read-queued", "write-after-barrier-ok",
    ]


def test_gateway_restore_barrier_is_lowered_when_the_restore_fails() -> None:
    async def scenario() -> list[int]:
        gateway, binding, _budget, _net, _catalog = await _gateway(5)
        sizes = []
        with pytest.raises(RuntimeError):
            async with gateway.restore_barrier("inv1"):
                sizes.append(len(binding.serializer._barriers))
                raise RuntimeError("restore failed")
        sizes.append(len(binding.serializer._barriers))
        await binding.serializer.stop()
        return sizes

    assert asyncio.run(scenario()) == [1, 0]


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


def test_replace_devices_clears_device_keyed_state_but_keeps_the_locks() -> None:
    """A live device-list reload may keep a logical device_id while re-addressing the hardware.
    replace_devices() must drop the caches keyed by that id — slave discovery, refresh history and
    the module-serial inventory settle cache — so the new device cannot inherit the old one's
    topology or failure counters and re-discovers its own towers. The per-object
    write locks and per-device slave locks are kept: an in-flight write or discovery may still hold
    one, and dropping it would remove the serialization that protects the device during the swap.
    """

    async def scenario() -> tuple[bool, bool, bool, bool, bool, bool, str]:
        from datetime import UTC, datetime

        from app.gateway.rct import _SLOT_EMPTY_SETTLED

        gateway, binding, _budget, _net, _catalog = await _gateway(5)
        gateway._slave_cache["inv1"] = (1e18, SlaveDiscovery((), True, None))
        gateway._refresh_attempts[("inv1", "battery_power")] = 123.0
        gateway._refresh_failures[("inv1", "battery_power")] = 3
        gateway._inventory_state["inv1"] = {"battery_module_sn_6": _SLOT_EMPTY_SETTLED}
        gateway._inventory_values["inv1"] = {"battery_module_sn_0": ("SN-0", datetime(2026, 1, 1, tzinfo=UTC))}
        gateway._write_locks[("inv1", 0x1234)] = asyncio.Lock()
        async with gateway._slave_locks.setdefault("inv1", asyncio.Lock()):
            pass
        rebuilt = DeviceBinding(binding.entry, binding.endpoint, binding.serializer)
        gateway.replace_devices([rebuilt])
        resolved = gateway._by_address[(binding.entry.key.endpoint, binding.entry.network_id)]
        await binding.serializer.stop()
        return (
            bool(gateway._slave_cache),
            bool(gateway._refresh_attempts),
            bool(gateway._refresh_failures),
            bool(gateway._inventory_state) or bool(gateway._inventory_values),
            ("inv1", 0x1234) in gateway._write_locks,
            "inv1" in gateway._slave_locks,
            resolved,
        )

    slave_cache, attempts, failures, inventory, write_lock, slave_lock, resolved = asyncio.run(scenario())
    assert (slave_cache, attempts, failures) == (False, False, False)  # device-keyed state is dropped
    assert inventory is False  # the module-serial inventory settle cache is dropped too
    assert write_lock and slave_lock  # the locks survive the swap
    assert resolved == "inv1"  # the rebuilt binding is addressable again


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


def test_failed_tick_clears_the_liveness_source() -> None:
    """A failed heartbeat must not keep reporting the last successful source as the current one."""

    async def scenario() -> tuple[LivenessSource | None, int, LivenessSource | None, int]:
        clock = AutoClock()
        counters = DeviceCounters(last_success_monotonic=clock.monotonic())
        alive = [True]

        async def read() -> bool:
            return alive[0]

        beat = Heartbeat(counters, read, clock, interval_seconds=30, failure_threshold=3)
        await beat.tick()  # a recent transaction wins: source is TRANSACTION
        first = (beat.liveness_source, beat.consecutive_failures)
        alive[0] = False
        counters.last_success_monotonic = None  # no recent transaction or periodic either
        clock.advance(31)
        await beat.tick()  # every path fails: the stale source must be cleared
        return (*first, beat.liveness_source, beat.consecutive_failures)

    source, failures, after_source, after_failures = asyncio.run(scenario())
    assert (source, failures) == (LivenessSource.TRANSACTION, 0)
    assert (after_source, after_failures) == (None, 1)


async def test_cancelled_read_cancels_the_endpoint_call() -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Endpoint:
        async def execute(self, request, response_timeout):
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    class Clock:
        def monotonic(self) -> float:
            return 0.0

        async def sleep(self, seconds: float) -> None:
            await asyncio.sleep(3600)

    request = SimpleNamespace(read_total_timeout_seconds=None, abandoned=False)
    runner = asyncio.ensure_future(execute_read(Endpoint(), request, RetryConfig(), Clock()))
    await started.wait()
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert cancelled.is_set()


# --- FIX 1: static module serials are read by a bounded inventory pass, not the periodic set ---

_SHIPPED_REGISTRY = Path(__file__).resolve().parents[1] / "app" / "catalog" / "objects.json"


async def _inventory_gateway(populated: dict[str, bytes], unanswered: set[str]):
    """Gateway on the SHIPPED catalog (the fixture carries no module_sn slots). ``populated`` maps
    a module_sn metric name to the serial bytes the fake returns; ``unanswered`` names slots the
    device never answers, so their read times out (an unpopulated slot on real hardware).

    AutoClock (like the other gateway tests here) advances time on every internal sleep, so the
    cache TTL is set very high to keep the just-read serials FRESH across the whole pass. Serials
    are static inventory: a slot settles once (answered or confirmed empty) and is never re-read on
    a timer, so no re-gate advance is needed between passes."""
    clock = AutoClock()
    catalog = RegistryCatalog.from_file(_SHIPPED_REGISTRY)
    by_name = {name: catalog.object_entry(name).object_id for name in (set(populated) | unanswered)}
    unanswered_ids = {by_name[name] for name in unanswered}

    def behavior(frame):
        return "ignore" if frame.object_id in unanswered_ids else "respond"

    net = FakeNetwork(clock, behavior=behavior)
    for name, raw in populated.items():
        net.payloads[by_name[name]] = raw
    cache = MemoryCache(1e9, 1e9)  # serials must stay FRESH despite AutoClock advancing on sleeps
    gateway = RctGateway(
        catalog, cache, clock, Allowlist({}, catalog), retry=RetryConfig(response_timeout_seconds=0.01)
    )
    device = DeviceEntry(device_id="inv1", host="10.0.0.5")
    cfg = EndpointConfig(response_timeout_seconds=0.01, min_interval=timedelta(0))
    endpoint = TransportEndpoint("endpoint-1", device.key.endpoint, cfg, clock, connector=net.connect)
    serializer = AccessSerializer(
        endpoint, gateway.handler(endpoint), budget=WorkBudget(10_000, 1000, clock), cache_hit=gateway.cache_hit
    )
    serializer.start()
    gateway.add_device(DeviceBinding(device, endpoint, serializer))
    return gateway, serializer, net, clock


_INVENTORY_NAMES = tuple(f"battery_module_sn_{i}" for i in range(7))


def test_inventory_read_feeds_devices_module_count_without_touching_the_periodic_set() -> None:
    """read_module_inventory() lands the module serials in the cache so admin's devices() can still
    derive a populated tower's module count from cached_reading(), even though the slots are no
    longer periodically registered. A five-module tower reports six populated slots (five modules
    plus the base/top part)."""

    async def scenario():
        populated = {f"battery_module_sn_{i}": b"SN-%d" % i for i in range(6)}  # slots 0..5 populated
        gateway, serializer, _net, _clock = await _inventory_gateway(populated, unanswered={"battery_module_sn_6"})
        sent = await gateway.read_module_inventory("inv1", _INVENTORY_NAMES)
        readings = [gateway.cached_reading("inv1", n) for n in _INVENTORY_NAMES]
        await serializer.stop()
        return sent, readings

    sent, readings = asyncio.run(scenario())
    assert sent == 7  # all seven slots attempted in the one bounded pass
    # Slots 0..5 cached with their serials (the data devices() counts); slot 6 is unanswered -> None.
    assert [r[0] if r else None for r in readings] == ["SN-0", "SN-1", "SN-2", "SN-3", "SN-4", "SN-5", None]


def test_settled_inventory_slots_are_not_re_read_on_a_later_pass() -> None:
    """Serials are static: once every slot is settled (answered or confirmed empty), a further
    read_module_inventory call sends NO device frames and returns 0, so an inventory loop ticking
    often is cheap - no re-gate, no re-probe. Here every slot answers, so a single pass settles the
    whole tower; the next pass must do no device I/O at all."""

    async def scenario():
        populated = {f"battery_module_sn_{i}": b"SN-%d" % i for i in range(7)}  # all slots answer
        gateway, serializer, net, _clock = await _inventory_gateway(populated, unanswered=set())
        first = await gateway.read_module_inventory("inv1", _INVENTORY_NAMES)
        frames_after_first = len(net.frames)
        second = await gateway.read_module_inventory("inv1", _INVENTORY_NAMES)  # everything settled
        frames_after_second = len(net.frames)
        await serializer.stop()
        return first, second, frames_after_first, frames_after_second

    first, second, after_first, after_second = asyncio.run(scenario())
    assert first == 7 and second == 0  # settled slots are not re-read on the next pass
    assert after_second == after_first  # and no additional device frames are sent


def test_inventory_marks_an_unanswered_slot_as_read_failed_for_devices() -> None:
    """A slot the device never answers must become read_failed() after repeated passes, so
    devices() can tell an unpopulated slot (EMPTY) from one not read yet (UNKNOWN) and does not
    hold the tower on 'pending' forever."""

    async def scenario():
        populated = {f"battery_module_sn_{i}": b"SN-%d" % i for i in range(6)}
        gateway, serializer, _net, _clock = await _inventory_gateway(populated, unanswered={"battery_module_sn_6"})

        # No re-gate advance: slot 6 is re-probed each pass only until REFRESH_FAILURES_CONFIRMED
        # confirms it empty, then it settles and is never read again.
        for _ in range(3):  # REFRESH_FAILURES_CONFIRMED passes to confirm the unanswered slot
            await gateway.read_module_inventory("inv1", _INVENTORY_NAMES)
        failed = gateway.read_failed("inv1", "battery_module_sn_6")
        populated_ok = gateway.read_failed("inv1", "battery_module_sn_0")
        await serializer.stop()
        return failed, populated_ok

    failed, populated_ok = asyncio.run(scenario())
    assert failed is True  # the empty slot is confirmed unanswered
    assert populated_ok is False  # a populated slot is never marked failed
