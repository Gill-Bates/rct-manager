#!/usr/bin/env python3
#
# app/api/server.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Uvicorn runner that drives the application shutdown before the HTTP listener closes (Requirement 27.7)."""

import asyncio
import ipaddress
import logging
import os
import signal
import socket
from pathlib import Path
from types import FrameType

import uvicorn
from fastapi import FastAPI

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
            settings = app.state.runtime.settings
            path = write_first_start_password(settings, password)
            print(first_start_banner(settings, path), flush=True)

    async def serve(self, sockets=None) -> None:
        self._loop = asyncio.get_running_loop()
        if self._early_signal:
            log.info("Signal received before startup, starting shutdown")
            self._drain_requested = True
            self._start_drain()
        await super().serve(sockets)


FIRST_START_PASSWORD_FILE = "initial-admin-password"


def write_first_start_password(settings: Settings, password: str) -> Path:
    """Hand the bootstrap credential over through a 0600 file next to the admin DB.

    Stdout is persisted and centrally collected in systemd/Docker/Kubernetes, so it must not carry it.

    The secret filename is never ``resolve()``d: a pre-planted ``initial-admin-password`` symlink
    must not let an attacker redirect the unlink/create onto its target. The file is created with
    ``O_NOFOLLOW`` through a directory fd so the final path component cannot be a symlink either.
    """
    directory = settings.admin_db_path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / FIRST_START_PASSWORD_FILE
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if os.fstat(dir_fd).st_mode & 0o077:
            os.fchmod(dir_fd, 0o700)  # tighten a parent directory created with a looser umask
        # Relative to the trusted directory fd and never following a symlink: unlink the plain name,
        # then create it exclusively. The mode is owner-only from creation, never tightened afterwards.
        try:
            os.unlink(FIRST_START_PASSWORD_FILE, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        descriptor = os.open(
            FIRST_START_PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd
        )
    finally:
        os.close(dir_fd)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(password + "\n")
    return path


def first_start_banner(settings: Settings, password_file: Path) -> str:
    """Boxed first-login notice naming where the one-time password was saved.

    The password itself is never printed: stdout is persisted and centrally collected in
    systemd/Docker/Kubernetes, where it would remain as a permanent secret history. The password
    file (0600, next to the admin DB) is the only place the operator reads it from.
    """
    host = url_host(settings.bind_address)
    rule = "=" * 61
    return "\n".join((
        "", rule, " FIRST START - admin login", " User:          admin",
        f" Password file: {password_file} (0600)",
        "                (one-time; change it at first login)",
        f" Open:          http://{host}:{settings.bind_port}/", rule, "",
    ))


def _in_container() -> bool:
    # RCT_API_CONTAINER is set by the Dockerfile; the marker files cover other runtimes.
    return os.environ.get("RCT_API_CONTAINER") == "1" or any(
        os.path.exists(marker) for marker in ("/.dockerenv", "/run/.containerenv")
    )


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
    asyncio.run(server.serve(sockets=[sock] if sock is not None else None))
    return 0
