#!/usr/bin/env python3
#
# app/observability/event_hub.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""A small, domain-agnostic in-process pub/sub hub behind the admin SSE endpoint.

Producers publish TYPED events (``event_type`` + a JSON-serialisable ``data`` dict); the SSE endpoint
(``GET /admin/api/events``) subscribes and serialises each event to the wire. The hub owns no domain
logic: it does not know what a ``tsdb.log`` event means, only how to buffer it, number it and fan it
out. Adding a new event type later is just "publish a new type" plus, optionally, registering a
buffering policy for it.

Design (the four guardrails):

* **Transport vs. domain.** The hub is pure transport/fan-out. The code that turns log records into
  ``tsdb.log`` events lives in the producer (``app/observability/log_stream.py``), not here.
* **Monotonic global id.** Every published event gets the next value of one global counter, exposed
  on the wire as ``id:``. ``Last-Event-ID`` is accepted by the endpoint but Phase 1 does no durable
  replay; a reconnecting client simply receives whatever is still in the per-type backlog.
* **History vs. queues.** Per-type backlog (a bounded ring buffer) is separate from the per-subscriber
  bounded queues. A full subscriber queue never blocks the producer.
* **Pluggable per-type policy.** Each event type may register a :class:`BacklogPolicy` controlling its
  ring-buffer size and the drop behaviour on a full subscriber queue (drop oldest, drop newest, or
  latest-wins for a future state type). Unknown types fall back to a default policy.
"""

import asyncio
import contextlib
import itertools
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum


class DropPolicy(Enum):
    """What a type's events do when a subscriber's bounded queue is already full.

    DROP_OLDEST / DROP_NEWEST keep a rolling stream (used by ``tsdb.log``). LATEST_WINS collapses the
    queue to the newest event, for a future state-type where only the current value matters; it is
    defined here so adding such a type later needs no hub change, but no Phase 1 type uses it.
    """

    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    LATEST_WINS = "latest_wins"


@dataclass(frozen=True)
class BacklogPolicy:
    """Per-type buffering: how many events to retain for replay and how to shed load on a full queue."""

    backlog_size: int = 0  # events kept for replay to a new/reconnecting subscriber (0 = no backlog)
    drop: DropPolicy = DropPolicy.DROP_OLDEST  # behaviour when a subscriber queue is full


@dataclass(frozen=True)
class Event:
    """One published event. ``id`` is assigned by the hub from its global monotonic counter."""

    id: int
    type: str
    data: dict


@dataclass(eq=False)  # identity hashing: subscribers live in a set and each one is distinct
class _Subscriber:
    queue: asyncio.Queue[Event]
    # Per-type drop decision is cheap to look up here so _publish stays non-blocking.
    policies: dict[str, DropPolicy] = field(default_factory=dict)


# Per-subscriber live buffer size; bounded so a slow browser never grows memory without limit.
_DEFAULT_QUEUE_SIZE = 1000
_DEFAULT_POLICY = BacklogPolicy(backlog_size=0, drop=DropPolicy.DROP_OLDEST)


class EventHub:
    """In-process pub/sub: typed events in, per-subscriber bounded queues out, bounded per-type backlog."""

    def __init__(self, *, queue_size: int = _DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._ids = itertools.count(1)  # global monotonic id sequence (starts at 1)
        self._policies: dict[str, BacklogPolicy] = {}
        self._backlogs: dict[str, deque[Event]] = {}
        self._subscribers: set[_Subscriber] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def register_type(self, event_type: str, policy: BacklogPolicy) -> None:
        """Declare a type's buffering policy. Call once per type before (or at) producer start-up."""
        self._policies[event_type] = policy
        if policy.backlog_size > 0:
            self._backlogs[event_type] = deque(maxlen=policy.backlog_size)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the running loop so a producer on any thread can deliver onto subscriber queues safely."""
        self._loop = loop

    def _policy(self, event_type: str) -> BacklogPolicy:
        return self._policies.get(event_type, _DEFAULT_POLICY)

    def publish(self, event_type: str, data: dict) -> None:
        """Publish a typed event. Non-blocking and never raises; safe to call from any thread.

        The global id is assigned here (so ids stay monotonic regardless of the caller thread),
        the event is appended to the type's backlog, and delivery to subscriber queues is scheduled
        on the loop thread.
        """
        if self._closed:
            return
        event = Event(id=next(self._ids), type=event_type, data=data)
        backlog = self._backlogs.get(event_type)
        if backlog is not None:
            backlog.append(event)
        loop = self._loop
        if loop is None or not self._subscribers:
            return
        loop.call_soon_threadsafe(self._deliver, event)

    def _deliver(self, event: Event) -> None:
        drop = self._policy(event.type).drop
        for sub in self._subscribers:
            self._offer(sub, event, drop)

    @staticmethod
    def _offer(sub: _Subscriber, event: Event, drop: DropPolicy) -> None:
        queue = sub.queue
        if not queue.full():
            queue.put_nowait(event)
            return
        if drop is DropPolicy.DROP_NEWEST:
            return  # keep what is queued, discard this event
        # DROP_OLDEST and LATEST_WINS both make room by discarding the head; LATEST_WINS additionally
        # drains the rest so only the newest event remains.
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        if drop is DropPolicy.LATEST_WINS:
            while not queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(event)

    def backlog(self, *event_types: str) -> list[Event]:
        """Replay snapshot: the retained events for the given types (all types if none given), id-ordered."""
        wanted = self._backlogs if not event_types else {t: self._backlogs.get(t) for t in event_types}
        events: list[Event] = []
        for buffer in wanted.values():
            if buffer:
                events.extend(buffer)
        events.sort(key=lambda e: e.id)
        return events

    @contextlib.asynccontextmanager
    async def subscribe(self, *event_types: str) -> AsyncIterator[tuple[list[Event], asyncio.Queue[Event]]]:
        """Yield ``(backlog_snapshot, live_queue)``; the subscriber is always removed on exit.

        ``event_types`` scopes which types' backlog is replayed; the live queue still receives every
        published type (the SSE endpoint streams all types over one connection).
        """
        policies = {t: self._policy(t).drop for t in self._policies}
        sub = _Subscriber(queue=asyncio.Queue(maxsize=self._queue_size), policies=policies)
        snapshot = self.backlog(*event_types)
        self._subscribers.add(sub)
        try:
            yield snapshot, sub.queue
        finally:
            self._subscribers.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def close(self) -> None:
        """Stop accepting events and drop every subscriber/backlog reference (app teardown)."""
        self._closed = True
        self._subscribers.clear()
        self._backlogs.clear()
        self._loop = None
