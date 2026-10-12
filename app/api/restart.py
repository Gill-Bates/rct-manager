#!/usr/bin/env python3
#
# app/api/restart.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Debounced, loop-guarded background restart for settings that cannot be applied in place.

Only the HTTP listener (bind address and port) is bound once per process. A saved change to one
of them is applied by a controlled re-exec of the same command line: the normal graceful shutdown
runs first (in-flight requests drain, dispatch hands the inverters back), then the process image
is replaced. The PID, and therefore a container's PID 1 and a systemd unit's main PID, stays.
"""

import asyncio
import contextlib
import inspect
import logging
import os
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from ipaddress import ip_address
from typing import NoReturn

log = logging.getLogger(__name__)

RESTARTED_AT_ENV = "RCT_API_RESTARTED_AT"
DEBOUNCE_SECONDS = 1.5
MIN_INTERVAL_SECONDS = 30.0


def in_container() -> bool:
    """True inside a container; RCT_API_CONTAINER is set by the Dockerfile, the marker files cover other runtimes."""
    return os.environ.get("RCT_API_CONTAINER") == "1" or any(
        os.path.exists(marker) for marker in ("/.dockerenv", "/run/.containerenv")
    )


def container_ip() -> str | None:
    """Primary IPv4 of this container, or None when it cannot be determined.

    A UDP socket is given a non-routable test-net destination (RFC 5737): ``connect`` on a
    datagram socket only sets the kernel's source selection and sends nothing, so ``getsockname``
    yields the IP of the interface that would carry outbound traffic without any DNS or packets.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("192.0.2.1", 9))
        ip = sock.getsockname()[0]
        return ip if ip and not ip_address(ip).is_loopback else None
    except OSError:
        return None
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()


def last_restart_at() -> float:
    """Wall-clock time of the previous re-exec (0 when this process was started normally)."""
    try:
        return float(os.environ.get(RESTARTED_AT_ENV, "0"))
    except ValueError:
        return 0.0


class BackgroundRestart:
    """Several quick requests collapse into one restart, and restarts keep a minimum distance."""

    def __init__(
        self,
        restarter: Callable[[], Awaitable[None] | None],
        *,
        needed: Callable[[], bool] = lambda: True,
        debounce: float = DEBOUNCE_SECONDS,
        min_interval: float = MIN_INTERVAL_SECONDS,
        last_restart: float | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._restarter = restarter
        self._needed = needed
        self._debounce = debounce
        self._min_interval = min_interval
        self._last_restart = last_restart_at() if last_restart is None else last_restart
        self._wall_clock = wall_clock
        self._handle: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self.fired = False

    def request(self, reason: str, loop: asyncio.AbstractEventLoop | None) -> bool:
        """Thread-safe. Arms (or re-arms) the timer; False when no loop is available."""
        if loop is None or loop.is_closed():
            return False
        loop.call_soon_threadsafe(self._arm, reason)
        return True

    def _arm(self, reason: str) -> None:
        if self.fired:
            return
        if self._handle is not None:
            self._handle.cancel()
        # A restart right after a restart would be a loop: wait out the minimum distance instead.
        guard = self._last_restart + self._min_interval - self._wall_clock()
        delay = max(self._debounce, guard)
        log.info("Background restart scheduled in %.1f s: %s", delay, reason)
        loop = asyncio.get_running_loop()
        self._handle = loop.call_later(delay, self._start)

    def _start(self) -> None:
        self._handle = None
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        if self.fired:
            return
        if not self._needed():
            log.info("Background restart skipped: the running listener already matches the settings")
            return
        self.fired = True
        try:
            result = self._restarter()
            if inspect.isawaitable(result):
                await result
        except Exception:
            self.fired = False  # a failed attempt must not block the next request
            log.exception("Background restart failed; the service keeps running with its current listener")

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if self._task is not None and not self._task.done():
            self._task.cancel()


def _current_listener_holds(address: str, current_address: str | None, *, port_unchanged: bool) -> bool:
    """True only when EADDRINUSE on ``address`` must come from the service's own listener.

    ``port_unchanged=True`` is necessary but not sufficient: on an unchanged port a busy new
    address is the current listener only when that listener actually covers the address, i.e. it
    is bound to the very same address, or it is a wildcard bind that holds every address on the
    port. Two different specific addresses on the same port are a genuine conflict with another
    process and must not be waved through.
    """
    if not port_unchanged or current_address is None:
        return False
    try:
        new_ip = ip_address(address)
        current_ip = ip_address(current_address)
    except ValueError:
        return False
    return new_ip == current_ip or current_ip.is_unspecified  # 0.0.0.0 / :: covers the whole port


def probe_listener(
    address: str, port: int, *, port_unchanged: bool, current_address: str | None = None
) -> str | None:
    """Return a user-facing reason when the new listener could not be bound, else None.

    Checked before anything is saved: re-exec onto an unbindable address would take the service
    down. On an unchanged port the service's own listener still holds it, so an EADDRINUSE is
    expected only where that listener actually covers the new address (``current_address``: the
    same address, or a wildcard bind). An EADDRINUSE from any other process — a different specific
    address on the same port whose binder we cannot identify — stays an error, so a re-exec that
    could not bind is never scheduled.
    """
    try:
        family = socket.AF_INET6 if ip_address(address).version == 6 else socket.AF_INET
    except ValueError:
        return "The listen address is not a valid IP address."
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((address, port))
    except OSError as exc:
        if exc.errno == 98 and _current_listener_holds(  # EADDRINUSE
            address, current_address, port_unchanged=port_unchanged
        ):
            return None
        return f"The service cannot listen on {address}:{port} ({exc.strerror or exc})."
    finally:
        with contextlib.suppress(OSError):
            probe.close()
    return None


def reexec() -> NoReturn:
    """Replace the process image with the same command line; settings are re-read from the store."""
    os.environ[RESTARTED_AT_ENV] = str(time.time())
    # A test harness hands over a pre-bound socket; the new process must bind its own.
    os.environ.pop("RCT_API_BOUND_FD", None)
    logging.shutdown()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])  # noqa: S606 - own interpreter, own argv
