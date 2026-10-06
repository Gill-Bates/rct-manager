#!/usr/bin/env python3
#
# app/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Vendor-neutral REST gateway for RCT Power inverters."""

__all__ = ["__version__"]

import tomllib
from pathlib import Path

# No installed package metadata to rely on: the app runs from source (no
# [build-system]/pip install, see docker/Dockerfile), so the version is read
# directly from pyproject.toml, which sits one level up from this package both
# in the repo and in the image (WORKDIR /app, COPY pyproject.toml ./).
_PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _read_version() -> str:
    try:
        data = tomllib.loads(_PYPROJECT_PATH.read_text())
        return data["project"]["version"]
    except (OSError, tomllib.TOMLDecodeError, KeyError):
        return "0.0.0"


__version__ = _read_version()
