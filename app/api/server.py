#!/usr/bin/env python3
#
# app/api/server.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Uvicorn runner that drives the application shutdown before the HTTP listener closes (Requirement 27.7)."""

import asyncio
import ipaddress
import logging
import signal
import socket
from types import FrameType

import uvicorn
from fastapi import FastAPI

from app.api.restart import container_ip as _container_ip
from app.api.restart import in_container as _in_container
from app.api.restart import reexec as _reexec
from app.api.runtime import Runtime
from app.config import Settings, url_host

log = logging.getLogger(__name__)


class GracefulServer(uvicorn.Server):
    """First SIGTERM/SIGINT starts the four-phase shutdown; uvicorn exits only after it finished."""

    def __init__(self, config: uvicorn.Config, runtime: Runtime) -> None:
        super().__init__(config)
        self._runtime = runtime
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drain: asyncio.Task[int] | None = None
        self._drained = False
        self._drain_requested = False
        self._early_signal = False
        self.restart_requested = False

    def request_restart(self) -> None:
        """Run the normal shutdown, then let ``run_server`` re-exec; call on the event loop."""
        if self._drain_requested:
            return
        log.info("Restarting in the background to apply the changed listener settings")
        self.restart_requested = True
        self._drain_requested = True
        self._start_drain()

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        # Not calling super() keeps uvicorn from re-raising the signal after a clean exit (exit code 0).
        if self._drained:
            self.should_exit = True
            self.force_exit = self.force_exit or sig == signal.SIGINT
            return
        if self._loop is None:
            # Event loop not running yet: serve() starts the drain as soon as it is.
            self._early_signal = True
            return
        if self._drain_requested:
            log.warning("Shutdown already in progress; the deadline stays unchanged")
            return
        log.info("Received signal %s, starting shutdown", signal.Signals(sig).name)
        self._drain_requested = True
        self._loop.call_soon_threadsafe(self._start_drain)

    def _start_drain(self) -> None:
        # Keep a reference: the loop holds tasks weakly and could drop the drain mid-flight.
        if self._runtime.shutdown is not None:
            self._drain = asyncio.get_running_loop().create_task(self._run_drain())

    async def _run_drain(self) -> int:
        try:
            assert self._runtime.shutdown is not None
            return await self._runtime.shutdown.run()
        finally:
            self._drained = True
            self.should_exit = True  # now uvicorn may close the listener and run the lifespan exit

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        app = self.config.app
        password = getattr(getattr(app, "state", None), "first_start_password", None)
        if password and self.started:  # after uvicorn's own log lines so the notice stays visible
            print(first_start_banner(app.state.runtime.settings, password), flush=True)

    async def serve(self, sockets=None) -> None:
        self._loop = asyncio.get_running_loop()
        if self._early_signal:
            log.info("Signal received before startup, starting shutdown")
            self._drain_requested = True
            self._start_drain()
        await super().serve(sockets)


def _banner_host(settings: Settings) -> str:
    """Host for the banner's ``Open:`` URL; never raises so the banner always builds.

    Outside a container the bind address is kept. Inside one, a bind address bound to the whole
    container (loopback or 0.0.0.0/::) is useless from outside, so the detected container IP is
    preferred; a specific non-loopback bind address the operator chose, or a failed detection,
    falls back to the bind address.
    """
    try:
        bind_is_wildcard_or_loopback = ipaddress.ip_address(str(settings.bind_address)).is_loopback or (
            ipaddress.ip_address(str(settings.bind_address)).is_unspecified
        )
        if _in_container() and bind_is_wildcard_or_loopback:
            detected = _container_ip()
            if detected:
                return url_host(detected)
    except (ValueError, OSError):
        pass
    return url_host(settings.bind_address)


def first_start_banner(settings: Settings, password: str) -> str:
    """Boxed first-login notice that prints the one-time admin password to the console.

    The password is shown on stdout by deliberate operator choice; systemd/Docker/Kubernetes may
    persist it in log history, an accepted tradeoff. It appears only on the initial bootstrap.
    """
    host = _banner_host(settings)
    rule = "=" * 61
    return "\n".join((
        "", rule, " FIRST START", " Admin user:         admin",
        f" One-time password:  {password}",
        " Please change this password after login.",
        f" Open:               http://{host}:{settings.bind_port}/", rule, "",
    ))


def warn_if_loopback_in_container(settings: Settings) -> None:
    """Warn (never abort) when a container binds to loopback, which no port mapping can reach."""
    if _in_container() and ipaddress.ip_address(str(settings.bind_address)).is_loopback:
        log.warning(
            "Running in a container but BIND_ADDRESS=%s is a loopback address: published ports "
            "(docker -p / compose ports) cannot reach the service, while the container health check "
            "may still pass. Set BIND_ADDRESS=0.0.0.0 (with the GUI option \"behind reverse proxy\") and make "
            "BIND_PORT=%s match the container port of the port mapping.",
            settings.bind_address,
            settings.bind_port,
        )


def run_server(app: FastAPI, settings: Settings, sock: socket.socket | None = None) -> int:
    """``sock``, when given, is a pre-bound, listening socket uvicorn should serve on directly.

    Lets a caller (e.g. a test harness) reserve the port itself and hand the already-bound
    socket over, instead of racing a bind-probe-close against uvicorn's own later bind.
    """
    warn_if_loopback_in_container(settings)
    config = uvicorn.Config(
        app,
        host=str(settings.bind_address),
        port=settings.bind_port,
        workers=settings.http_workers,
        log_config=None,  # the application logging setup stays in charge
        access_log=False,
        timeout_graceful_shutdown=5,
        server_header=False,
        # One trust configuration only: TRUSTED_PROXIES/FORWARDED_HEADER decide who may speak for a
        # client. Uvicorn's own forwarded-header handling would be a second, divergent list.
        proxy_headers=False,
    )
    server = GracefulServer(config, app.state.runtime)
    app.state.server_restart = server.request_restart
    asyncio.run(server.serve(sockets=[sock] if sock is not None else None))
    if server.restart_requested:
        try:
            _reexec()
        except OSError:
            # A non-zero exit lets the supervisor's restart policy start the service instead.
            log.exception("Re-exec failed; exiting so the supervisor starts the service again")
            return 1
    return 0
