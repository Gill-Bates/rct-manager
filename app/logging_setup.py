#!/usr/bin/env python3
#
# app/logging_setup.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Structured logging to stdout with an optional correlation id; text output is coloured on a terminal."""

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import ClassVar, TextIO

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if (cid := correlation_id.get()) is not None:
            entry["correlation_id"] = cid
        entry.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable line with the correlation id when present.

    With ``colors`` the level name is coloured and the timestamp dimmed; the
    padding is applied before the escape codes so the columns stay aligned.
    """

    RESET = "\x1b[0m"
    TIMESTAMP_COLOR = "\x1b[90m"  # dark gray
    LEVEL_COLORS: ClassVar[dict[int, str]] = {
        logging.DEBUG: "\x1b[36m",       # cyan
        logging.INFO: "\x1b[32m",        # green
        logging.WARNING: "\x1b[33m",     # yellow
        logging.ERROR: "\x1b[31m",       # red
        logging.CRITICAL: "\x1b[1;31m",  # bold red
    }

    def __init__(self, *args: object, colors: bool = False, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.colors = colors

    def format(self, record: logging.LogRecord) -> str:
        cid = correlation_id.get()
        prefix = f"[{cid}] " if cid else ""
        timestamp = datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%d %H:%M:%S")
        level = f"{record.levelname:<7}"
        if self.colors:
            timestamp = f"{self.TIMESTAMP_COLOR}{timestamp}{self.RESET}"
            if color := self.LEVEL_COLORS.get(record.levelno):
                level = f"{color}{level}{self.RESET}"
        base = f"{timestamp} - {record.name:<24} - {level} - "
        line = base + prefix + super().format(record)
        extra = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and k != "color_message"}
        if extra:
            line += " " + " ".join(f"{k}={json.dumps(v, default=str, ensure_ascii=False)}" for k, v in extra.items())
        return line


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Replace root handlers with one stdout handler."""
    handler = logging.StreamHandler(sys.stdout)
    # Colour is decided from the stream (TTY, NO_COLOR, FORCE_COLOR); JSON output is never coloured.
    handler.setFormatter(
        JsonFormatter() if fmt == "json" else TextFormatter("%(message)s", colors=use_colors(sys.stdout))
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def use_colors(stream: TextIO) -> bool:
    """Whether ANSI colour is appropriate for ``stream``.

    An explicit opt-out wins over everything else, an explicit opt-in over the
    TTY check, so a container whose stdout is a pipe can still ask for colour.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    if _truthy(os.environ.get("FORCE_COLOR")) or _truthy(os.environ.get("CLICOLOR_FORCE")):
        return True
    if not getattr(stream, "isatty", lambda: False)():
        return False
    return os.environ.get("TERM", "").strip().lower() not in {"", "dumb"}
