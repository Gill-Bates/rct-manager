#!/usr/bin/env python3
#
# app/scheduling/serializer.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Access serializer: one worker task and a bounded FIFO queue per transport endpoint."""

import asyncio
import logging
import math
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.errors import BudgetExhausted, DeviceApiError, QueueFullError, QueueTimeout
from app.scheduling.budget import BudgetHandle, WorkBudget
from app.transport.endpoint import TransportEndpoint
from app.transport.types import (
    SendOutcome,
    TransactionOrigin,
    TransactionRequest,
    TransactionResult,
)

__all__ = ["AccessSerializer", "TransactionOrigin", "TransactionRequest"]

log = logging.getLogger(__name__)

# Restore and dispatch writes jump the FIFO and ignore its capacity; they wait only for the running
# transaction, so this bound just keeps a wedged worker from blocking a caller forever.
_PRIORITY_MAX_WAIT_SECONDS = 60.0
_PRIORITY_ORIGINS = frozenset({TransactionOrigin.SYSTEM_WRITE, TransactionOrigin.SHUTDOWN})

type Handler = Callable[[TransactionRequest], Awaitable[TransactionResult]]


def _consume(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()  # mark retrieved so a departed caller leaves no warning


@dataclass(slots=True)
class _Item:
    request: TransactionRequest
    started: asyncio.Future[None] = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    result: asyncio.Future[TransactionResult] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )

    @property
    def priority(self) -> bool:
        return self.request.origin in _PRIORITY_ORIGINS


class _BoundedDeque:
    """FIFO queue of ``_Item`` bounded by capacity, with O(1) removal of a not-yet-started item.

    ``asyncio.Queue`` has no supported way to drop a specific not-yet-started entry, so a timed
    out or cancelled wait left a dead entry occupying a capacity slot until the worker reached and
    skipped it. This queue lets ``submit()`` remove its own item immediately on abandonment,
    freeing the slot for a new, live request right away instead of leaving it bound until the
    worker's FIFO turn.
    """

    __slots__ = ("_capacity", "_condition", "_items", "_unfinished")

    def __init__(self, maxsize: int) -> None:
        self._capacity = maxsize
        self._items: deque[_Item] = deque()
        self._condition = asyncio.Condition()
        self._unfinished = 0

    def qsize(self) -> int:
        return len(self._items)

    def empty(self) -> bool:
        return not self._items

    async def put(self, item: _Item) -> None:
        async with self._condition:
            if item.priority:
                # Overtakes queued reads only: behind every queued write (caller writes included),
                # so writes to a device keep their order and a restore is never overwritten by an
                # older caller write that was queued first.
                position = max(
                    (i + 1 for i, queued in enumerate(self._items) if queued.priority or queued.request.kind == "write"),
                    default=0,
                )
                self._items.insert(position, item)
            else:
                if len(self._items) >= self._capacity:
                    raise asyncio.QueueFull
                self._items.append(item)
            self._unfinished += 1
            self._condition.notify_all()

    async def discard(self, item: _Item) -> bool:
        """Remove ``item`` if it is still queued; False if the worker already took it."""
        async with self._condition:
            try:
                self._items.remove(item)
            except ValueError:
                return False
            self._unfinished -= 1  # abandoned before it ran: no task_done() will follow for it
            self._condition.notify_all()
            return True

    async def get(self) -> _Item:
        async with self._condition:
            while not self._items:
                await self._condition.wait()
            return self._items.popleft()

    def get_nowait(self) -> _Item:
        return self._items.popleft()

    async def reset(self) -> None:
        """Forget unfinished work whose items were taken out by stop() without a task_done()."""
        async with self._condition:
            self._unfinished = len(self._items)
            self._condition.notify_all()

    async def task_done(self) -> None:
        async with self._condition:
            self._unfinished -= 1
            self._condition.notify_all()

    async def join(self) -> None:
        async with self._condition:
            while self._unfinished > 0:
                await self._condition.wait()


class AccessSerializer:
    """Only the worker sends; it takes one transaction, finishes it, then takes the next."""

    def __init__(
        self,
        endpoint: TransportEndpoint,
        handler: Handler,
        *,
        queue_max_length: int = 32,
        queue_max_wait_seconds: float = 10.0,
        seconds_per_transaction: float = 5.3,
        budget: WorkBudget | None = None,
        cache_hit: Callable[[TransactionRequest], bool] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.budget = budget
        self._handler = handler
        self._queue = _BoundedDeque(queue_max_length)
        self._max_wait = queue_max_wait_seconds
        self._per_tx = seconds_per_transaction
        self._cache_hit = cache_hit
        self._worker: asyncio.Task[None] | None = None
        self._current: _Item | None = None
        self._accepting = True

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    def queue_length(self) -> int:
        return self._queue.qsize()

    def accepting(self) -> bool:
        return self._accepting

    def stop_accepting(self) -> None:
        self._accepting = False

    def reserve_budget(self, count: int = 1) -> BudgetHandle | None:
        """Reserve before queueing; the handle rides on the request and is released unless it starts."""
        if self.budget is None:
            return None
        handle = self.budget.try_consume(count)
        if handle is None:
            raise BudgetExhausted(retry_after=self.budget.retry_after())
        return handle

    @staticmethod
    def _release(item: _Item) -> None:
        if item.request.charge is not None:
            item.request.charge.release()

    @classmethod
    def _abort(cls, item: _Item) -> None:
        cls._release(item)
        for future in (item.started, item.result):
            if not future.done():
                future.set_exception(DeviceApiError("shutdown"))
                future.add_done_callback(_consume)

    async def drain(self) -> None:
        """Wait until the queue is empty and no transaction is running."""
        await self._queue.join()

    async def stop(self) -> int:
        """Cancel the worker (shutdown only); returns the number of aborted transactions."""
        self._accepting = False  # a submit() racing the teardown must not queue behind a dead worker
        worker, self._worker = self._worker, None
        running = self._current  # the worker clears it while being cancelled
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        items = [running] if running is not None else []
        while not self._queue.empty():
            items.append(self._queue.get_nowait())
        for item in items:
            self._abort(item)  # a running transaction was committed, so its release is a no-op
        await self._queue.reset()  # otherwise a later drain()/join() waits for items that never finish
        self._current = None
        return len(items)

    async def submit(self, request: TransactionRequest) -> TransactionResult:
        if not self._accepting:
            raise DeviceApiError("not_ready")
        item = _Item(request)
        item.result.add_done_callback(_consume)
        try:
            await self._queue.put(item)
        except asyncio.QueueFull:
            self._release(item)
            raise QueueFullError(retry_after=math.ceil(self._queue.qsize() * self._per_tx)) from None
        try:
            wait = _PRIORITY_MAX_WAIT_SECONDS if item.priority else self._max_wait
            await asyncio.wait_for(asyncio.shield(item.started), wait)
        except TimeoutError:
            if not item.started.done():
                request.abandoned = True
                self._release(item)
                # Remove the item right away: a timed-out caller must not keep binding a queue
                # slot until the worker's FIFO turn reaches and skips it.
                await self._queue.discard(item)
                raise QueueTimeout() from None
        except asyncio.CancelledError:
            request.abandoned = True  # not started: dropped without sending; started: runs to its end
            if not item.started.done():
                self._release(item)
                await self._queue.discard(item)
            raise
        return await asyncio.shield(item.result)

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item.request.abandoned:
                    item.result.set_exception(QueueTimeout())
                    continue
                self._current = item
                item.started.set_result(None)
                if item.request.recheck_cache and self._cache_hit is not None and self._cache_hit(item.request):
                    result = TransactionResult(SendOutcome(False, None), skipped_by_cache=True)
                else:
                    if item.request.charge is not None:
                        item.request.charge.commit()  # the transaction really starts: the unit stays spent
                    result = await self._handler(item.request)
                if not item.result.done():
                    item.result.set_result(result)
            except Exception as exc:  # outer boundary: one bad transaction must not kill the worker
                log.exception("Transaction failed unexpectedly")
                if not item.result.done():
                    item.result.set_exception(exc if isinstance(exc, DeviceApiError) else DeviceApiError())
            finally:
                self._release(item)  # skipped, abandoned or cancelled before the handler ran
                self._current = None
                await self._queue.task_done()
