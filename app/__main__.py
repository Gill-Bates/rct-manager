#!/usr/bin/env python3
#
# app/__main__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Command line entry point: ``serve`` or ``validate``."""

import argparse
import base64
import logging
import os
import secrets
import stat
import sys
from pathlib import Path

from app.allowlist import Allowlist
from app.banner import environment, print_banner, resolve_version
from app.catalog.registry import RegistryCatalog
from app.config import Settings, load_settings, parse_env_file
from app.errors import ConfigError
from app.logging_setup import setup_logging

log = logging.getLogger("app")
_DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / "settings.env"


def _secure_existing_permissions(path: Path) -> None:
    """Repair overly permissive bits on a file already holding the secret.

    Opens with O_NOFOLLOW first and changes permissions on the resulting file descriptor, the
    same FD-based check creation uses, so a symlink swapped in between the stat() that found the
    loose bits and this call cannot redirect the chmod to a different file.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        log.warning("%s holds the secret but is readable by other users and could not be reopened to fix it", path)
        return
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            log.warning("%s holds the secret but is not a regular file; refusing to change its permissions", path)
            return
        os.fchmod(fd, 0o600)
        log.warning("%s held the secret but was readable by other users; permissions corrected to 0600", path)
    finally:
        os.close(fd)


def _ensure_hmac_secret(env_file: Path | None) -> None:
    if os.environ.get("HMAC_SECRET") or os.environ.get("ADMIN_SECRET"):
        return
    path = env_file or _DEFAULT_ENV_FILE
    stored = parse_env_file(path) if path.exists() else {}
    if stored.get("HMAC_SECRET") or stored.get("ADMIN_SECRET"):  # the deprecated name still unlocks old databases
        if path.stat().st_mode & 0o077:
            _secure_existing_permissions(path)
        return
    secret = base64.b64encode(secrets.token_bytes(32)).decode()  # same shape as `openssl rand -base64 32`
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as target:
            if not stat.S_ISREG(os.fstat(target.fileno()).st_mode):
                raise ConfigError("invalid_configuration", detail="settings.env must be a regular file")
            os.fchmod(target.fileno(), 0o600)
            if path.stat().st_size:
                target.write("\n")
            target.write(f"HMAC_SECRET={secret}\n")
            target.flush()
            os.fsync(target.fileno())
    except OSError as exc:
        raise ConfigError("invalid_configuration", detail="HMAC_SECRET is missing and settings.env is not writable") from exc


def _report_invalid(exc: ConfigError) -> None:
    print(f"Configuration invalid:\n{exc.context.get('detail', exc.code)}", file=sys.stderr)


def _load(env_file: Path | None) -> Settings | None:
    setup_logging()  # defaults first, so warnings raised while loading are not lost
    try:
        settings = load_settings(env_file)
    except ConfigError as exc:
        _report_invalid(exc)
        return None
    setup_logging(settings.log_level, settings.log_format.value)
    return settings


def _validate(env_file: Path | None) -> int:
    settings = _load(env_file)
    if settings is None:
        return 1
    try:
        catalog = RegistryCatalog.from_file(settings.object_registry_path)
        if settings.heartbeat_metric_name not in catalog.names():
            raise ConfigError("invalid_object_registry", detail="HEARTBEAT_METRIC_NAME is not in the object registry")
        if settings.enable_write_support or settings.write_allowlist_path.exists():
            allowlist = Allowlist.load(settings.write_allowlist_path, catalog)
            if settings.enable_write_support and not len(allowlist):
                log.warning("Write support is enabled but the write allowlist approves no metric")
    except ConfigError as exc:
        _report_invalid(exc)
        return 1
    log.info("Configuration, object registry and write allowlist are valid")
    return 0


def _serve(env_file: Path | None) -> int:
    print_banner()
    try:
        _ensure_hmac_secret(env_file)
    except ConfigError as exc:
        _report_invalid(exc)
        return 1
    settings = _load(env_file)
    if settings is None:
        return 1
    log.info("rct-api %s starting (%s)", resolve_version(), environment())
    from app.api.app_factory import create_app
    from app.api.server import run_server

    try:
        app = create_app(settings)
    except ConfigError as exc:
        _report_invalid(exc)
        return 1
    sock = None
    bound_fd = os.environ.get("RCT_API_BOUND_FD")  # test-only: a harness that already reserved the port
    if bound_fd is not None:
        import socket

        sock = socket.fromfd(int(bound_fd), socket.AF_INET, socket.SOCK_STREAM)
    return run_server(app, app.state.runtime.settings, sock=sock)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="python -m app", description="RCT Power REST gateway")
    parser.add_argument("mode", nargs="?", default="serve", choices=("serve", "validate"))
    parser.add_argument(
        "--env-file", type=Path, default=_DEFAULT_ENV_FILE, help="dotenv file (default: project settings.env)"
    )
    args = parser.parse_args(arguments)
    return (_serve if args.mode == "serve" else _validate)(args.env_file)


if __name__ == "__main__":
    raise SystemExit(main())
