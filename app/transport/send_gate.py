#!/usr/bin/env python3
#
# app/transport/send_gate.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Single choke point for outgoing request frames; owns the pause and the Commit_Point."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta

from app.clock import Clock
from app.transport.types import SendOutcome


class SendGate:
    """The only caller of ``writer.write`` and ``writer.drain`` in the project."""

    def __init__(self, min_interval: timedelta, clock: Clock, drain_timeout_seconds: float = 5.0) -> None:
        self._interval = min_interval.total_seconds()
        self._drain_timeout = drain_timeout_seconds
        self._clock = clock
        self._last_send: float | None = None
        self._lock = asyncio.Lock()  # keeps the spacing intact even for concurrent callers

    async def send(
        self,
        writer: asyncio.StreamWriter,
        data: bytes,
        *,
        precheck: Callable[[], Exception | None] | None = None,
        on_commit: Callable[[datetime, float], None] | None = None,
    ) -> SendOutcome:
        """Wait out the minimum pause, then hand the frame over.

        Anything failing before the Commit_Point leaves committed=False; anything after it,
        including a failing or timed out drain(), leaves committed=True. ``on_commit`` runs
        synchronously at the Commit_Point so the caller keeps the state even if the task is
        cancelled afterwards.
        """
        async with self._lock:
            # Phase 1: before the Commit_Point.
            if self._last_send is not None:
                wait = self._last_send + self._interval - self._clock.monotonic()
                if wait > 0:
                    await self._clock.sleep(wait)
            if precheck is not None and (error := precheck()) is not None:
                return SendOutcome(committed=False, sent_at=None, error=error)
            if writer.is_closing():
                return SendOutcome(committed=False, sent_at=None, error=ConnectionResetError("connection closing"))
            # Phase 2: the Commit_Point. No await between the bookkeeping and write().
            # Requirement 9.2 puts it at the hand-over of the first byte, and the rationale to
            # criteria 2-5 places it conservatively there: a failure raised by write() leaves open
            # how many bytes are already on their way, so it counts as post-commit (Requirement
            # 9.17 then allows exactly one write frame on the wire).
            sent_at = self._clock.now()
            self._last_send = self._clock.monotonic()
            if on_commit is not None:
                on_commit(sent_at, self._last_send)
            try:
                writer.write(data)
                # Phase 3: after the Commit_Point; failures here never clear `committed`. The
                # timeout keeps a peer that stops reading from holding the endpoint lock forever.
                await asyncio.wait_for(writer.drain(), self._drain_timeout)
            except (TimeoutError, OSError, RuntimeError) as exc:
                return SendOutcome(committed=True, sent_at=sent_at, error=exc)
            return SendOutcome(committed=True, sent_at=sent_at)
