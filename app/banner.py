#!/usr/bin/env python3
#
# app/banner.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Startup banner and version resolution.

A centred wordmark, the version and the copyright line below it, cyan on a
terminal and plain everywhere else. It is written straight to stdout rather than
through the logger, so the art carries no timestamp and no level prefix.
"""

import functools
import os
import platform
import sys
from importlib import metadata

from app import __version__
from app.logging_setup import use_colors

DISTRIBUTION = "rct-rest-api"

_WORDMARK = r"""
          _                     _
 _ __ ___| |_        __ _ _ __ (_)      ___  ___ _ ____   _____ _ __
| '__/ __| __|      / _` | '_ \| |     / __|/ _ \ '__\ \ / / _ \ '__|
| | | (__| |_   _  | (_| | |_) | |  _  \__ \  __/ |   \ V /  __/ |
|_|  \___|\__| (_)  \__,_| .__/|_| (_) |___/\___|_|    \_/ \___|_|
                         |_|
""".strip("\n")

_CYAN = "\033[96m"
_RESET = "\033[0m"


@functools.cache
def resolve_version() -> str:
    """Installed distribution version, falling back to the packaged constant."""
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return __version__


@functools.cache
def build_info() -> str:
    """Short commit of the image build, or 'dev' outside one."""
    return os.environ.get("GIT_SHA", "").strip()[:7] or "dev"


@functools.cache
def build_date() -> str:
    """Day part of the image build timestamp, or '' outside an image."""
    return os.environ.get("BUILD_DATE", "").strip().split("T", 1)[0].replace("unknown", "")


def banner() -> str:
    """The banner as one block: wordmark plus centred text lines."""
    built = build_date()
    title = f"RCT Power REST gateway v{resolve_version()} ({build_info()})"
    text_lines = [
        title + (f"  -  built {built}" if built else ""),
        "(C) 2026 by Gill-Bates (https://github.com/Gill-Bates/docker-repo)",
    ]
    art_lines = [line.rstrip() for line in _WORDMARK.splitlines()]  # trailing blanks would skew the width
    art_width = max((len(line) for line in art_lines), default=0)
    width = max(art_width, *(len(line) for line in text_lines))

    pad = " " * ((width - art_width) // 2)
    centred_art = "\n".join(pad + line for line in art_lines)
    centred_text = "\n".join(line.center(width).rstrip() for line in text_lines)
    return f"\n{centred_art}\n\n{centred_text}\n"


def environment() -> str:
    """Runtime line kept as a log record: it is data, not decoration."""
    return f"Python {platform.python_version()} on {platform.system()} {platform.machine()}"


def print_banner() -> None:
    """Write the banner to stdout, coloured only on a compatible terminal.

    Never raises: a cosmetic banner must not be able to take the gateway down.
    """
    if not sys.stdout:
        return
    try:
        text = banner()
        sys.stdout.write(f"{_CYAN}{text}{_RESET}\n" if use_colors(sys.stdout) else f"{text}\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001, S110  # closed stdout or encoding glitches must not break startup
        pass


__all__ = ["banner", "build_date", "build_info", "environment", "print_banner", "resolve_version"]
