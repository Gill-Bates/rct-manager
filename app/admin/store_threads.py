#!/usr/bin/env python3
#
# app/admin/store_threads.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Cancellation-safe store-thread tracking for background services.

A background service that reaches the admin SQLite store from a worker thread must not let that
thread outlive the database: when the awaiting task is cancelled at shutdown, a bare
``asyncio.to_thread()`` keeps running, unregistered, and the store can be closed under it. The
helpers here run the store call in a shielded future that stays registered until the thread ends,
so shutdown can wait for every outstanding store thread (:func:`drain_store_threads`) before
closing the database.

This module is vendor- and feature-neutral: it is only about DB-store access safety, so every
background service that writes the store shares one registry. Provider/network I/O done via plain
``asyncio.to_thread()`` stays OUT of this mechanism on purpose — it touches no store and must not
hold up shutdown.
"""

import asyncio
import logging

log = logging.getLogger(__name__)

_DRAIN_TIMEOUT_SECONDS = 10.0
_inflight: set[asyncio.Future] = set()


def _reap(future: asyncio.Future) -> None:
    _inflight.discard(future)
    if not future.cancelled():
        future.exception()  # mark retrieved: a shielded thread may finish after its awaiter was cancelled


async def store_thread(func, *args):
    """Run a store call in a thread that survives cancellation of the awaiting task.

    The shielded future stays registered until its thread ends, so shutdown can wait for it
    (see :func:`drain_store_threads`) instead of closing the database underneath it.
    """
    future = asyncio.ensure_future(asyncio.to_thread(func, *args))
    _inflight.add(future)
    future.add_done_callback(_reap)
    return await asyncio.shield(future)


async def drain_store_threads(timeout: float = _DRAIN_TIMEOUT_SECONDS) -> None:
    """Wait for outstanding store threads; bounded so a hung disk cannot block shutdown."""
    pending = {future for future in _inflight if not future.done()}
    if not pending:
        return
    _, still_running = await asyncio.wait(pending, timeout=timeout)
    if still_running:
        log.warning(
            "%d store operation(s) still running after %.0fs at shutdown",
            len(still_running),
            timeout,
        )
