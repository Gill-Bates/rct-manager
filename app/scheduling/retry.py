#!/usr/bin/env python3
#
# app/scheduling/retry.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Retry rules; reads and writes are strictly separate (Requirement 8 versus 9)."""

import asyncio
import contextlib
from dataclasses import dataclass

from app.clock import Clock
from app.errors import DeviceMaintenance, DeviceTimeout
from app.transport.endpoint import TransportEndpoint
from app.transport.types import SendOutcome, TransactionRequest, TransactionResult


@dataclass(frozen=True, slots=True)
class RetryConfig:
    read_retries: int = 4
    backoff_initial_ms: int = 200
    backoff_max_ms: int = 5000
    write_retries: int = 2
    response_timeout_seconds: float = 5.0
    read_total_timeout_seconds: float = 20.0


def backoff_seconds(attempt: int, cfg: RetryConfig) -> float:
    """Wait before attempt ``attempt`` (>= 2)."""
    return min(cfg.backoff_initial_ms * 2 ** (attempt - 2), cfg.backoff_max_ms) / 1000


async def cancel_and_wait(*tasks: "asyncio.Future[object]") -> None:
    """Cancel and drain helper tasks so none outlives its cancelled owner (e.g. the endpoint lock)."""
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _execute_within(
    endpoint: TransportEndpoint, request: TransactionRequest, response_timeout: float, remaining: float, clock: Clock
) -> TransactionResult:
    """Bound the whole attempt by ``remaining``, not only its response-wait phase.

    ``endpoint.execute()`` also waits on the endpoint-wide transaction lock, and connects and
    sends before it ever starts the response wait; ``response_timeout`` alone leaves those
    phases unbounded, so a request can return well past ``read_total_timeout_seconds``.
    """
    task = asyncio.ensure_future(endpoint.execute(request, response_timeout=response_timeout))
    timer = asyncio.ensure_future(clock.sleep(remaining))
    try:
        await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        await cancel_and_wait(task, timer)
        raise
    timer.cancel()
    if task.done():
        return task.result()
    # Still queued on the lock, connecting or sending: cancel it. TransportEndpoint.execute()
    # already drops the connection on a CancelledError that lands after the Commit_Point, so the
    # attempt leaves no half-sent frame behind.
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    error = DeviceTimeout("attempt_timeout")
    return TransactionResult(SendOutcome(False, None, error), error=error)


async def execute_read(
    endpoint: TransportEndpoint, request: TransactionRequest, cfg: RetryConfig, clock: Clock
) -> TransactionResult:
    """First attempt plus READ_RETRIES; the total timeout stops further attempts."""
    total = request.read_total_timeout_seconds or cfg.read_total_timeout_seconds
    deadline = clock.monotonic() + total
    result: TransactionResult | None = None
    for attempt in range(1, cfg.read_retries + 2):
        if attempt > 1:
            wait = backoff_seconds(attempt, cfg)
            if clock.monotonic() + wait >= deadline:
                break
            await clock.sleep(wait)
        remaining = deadline - clock.monotonic()
        if remaining <= 0 or request.abandoned:
            break
        result = await _execute_within(
            endpoint, request, min(cfg.response_timeout_seconds, remaining), remaining, clock
        )
        if result.ok or isinstance(result.error, DeviceMaintenance):
            return result
    assert result is not None, "the first attempt always runs"
    return result


async def execute_write(
    endpoint: TransportEndpoint, request: TransactionRequest, cfg: RetryConfig, clock: Clock
) -> TransactionResult:
    """Retry only on SendOutcome.committed == False, never after the Commit_Point.

    Action variables and non-idempotent objects are not retried even before the Commit_Point.
    The outcome of a committed write is established by a read-back, not here.
    """
    retries = 0
    while True:
        result = await endpoint.execute(request)
        if result.ok or result.committed or isinstance(result.error, DeviceMaintenance):
            return result
        if request.is_action or not request.idempotent or request.abandoned or retries >= cfg.write_retries:
            return result
        retries += 1
        await clock.sleep(backoff_seconds(retries + 1, cfg))


async def execute_with_retry(
    endpoint: TransportEndpoint, request: TransactionRequest, cfg: RetryConfig, clock: Clock
) -> TransactionResult:
    runner = execute_write if request.kind == "write" else execute_read
    return await runner(endpoint, request, cfg, clock)
