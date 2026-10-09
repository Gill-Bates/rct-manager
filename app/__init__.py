#!/usr/bin/env python3
#
# app/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Vendor-neutral REST gateway for RCT Power inverters."""

__all__ = ["__build__", "__version__", "resolve_build"]

import os
import re
import tomllib
from pathlib import Path

# No installed package metadata to rely on: the image runs the app from source (only the
# dependencies are pip-installed, see docker/Dockerfile), so the version is read directly
# from pyproject.toml, which sits one level up from this package both in the repo and in
# the image (WORKDIR /app, COPY pyproject.toml ./).
_PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _read_version() -> str:
    try:
        data = tomllib.loads(_PYPROJECT_PATH.read_text())
        return data["project"]["version"]
    except (OSError, tomllib.TOMLDecodeError, KeyError):
        return "0.0.0"


__version__ = _read_version()


_BUILD_RE = re.compile(r"[0-9a-fA-F]{7,40}")


def resolve_build(raw: str | None) -> str:
    """Short commit hash from a raw GIT_SHA value, or 'dev' when absent or not hex."""
    value = (raw or "").strip()
    return value[:7].lower() if _BUILD_RE.fullmatch(value) else "dev"


# GIT_SHA is set by docker/Dockerfile (build arg); resolved once, never via git at runtime.
__build__ = resolve_build(os.environ.get("GIT_SHA"))
