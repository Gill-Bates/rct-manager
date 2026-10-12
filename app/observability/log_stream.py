#!/usr/bin/env python3
#
# app/observability/log_stream.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""The ``tsdb.log`` producer: capture the app's own TSDB-relevant log records for the admin SSE feed.

This is the first (Phase 1) producer wired into the domain-agnostic :class:`app.observability.
event_hub.EventHub`. A :class:`TsdbLogHandler` is attached to the root logger *in addition* to the
stdout handler installed by :func:`app.logging_setup.setup_logging`; it never replaces that handler
and never logs itself, so there is no feedback loop. The handler taps the same
:class:`logging.LogRecord` objects already going to stdout, keeps only the TSDB-relevant ones
(filtered by logger-name prefix, see :data:`TSDB_LOGGER_PREFIXES`), formats a small plain-dict
payload, and publishes it to the hub as the typed event ``tsdb.log``.

All domain knowledge (which loggers count, how a record becomes an event) lives here, in the
producer — never in the hub or the SSE endpoint, which stay transport-only.

Drop policy for ``tsdb.log``: :data:`TSDB_LOG_POLICY` uses ``DROP_OLDEST`` on a full subscriber queue
so the console behaves like a terminal tail (a slow browser loses the oldest lines, never the newest
activity, and the producer is never blocked). The hub also keeps a bounded ring buffer of the most
recent events for replay to a (re)connecting subscriber.

No ``docker logs``, no Docker socket, no new Linux capabilities and no writable filesystem are
involved: everything happens inside the process that already produces the logs.
"""

import logging
from datetime import UTC, datetime

from app.observability.event_hub import BacklogPolicy, DropPolicy, EventHub

# The wire event type for the log tail.
TSDB_LOG_EVENT = "tsdb.log"

# A log record is TSDB-relevant iff its logger name equals one of these prefixes or is a child of it
# (``name == p`` or ``name.startswith(p + ".")``). The set is deliberately small and documented:
#   - ``app.export``        covers the whole metrics-export pipeline: ``app.export.pusher``,
#                           ``app.export.questdb_admin`` and the rest of ``app/export/`` (each uses
#                           ``logging.getLogger(__name__)``, so names are ``app.export.*``).
#   - ``app.api.app_factory`` carries the metrics-export lifecycle logging in
#                           ``app/api/app_factory.py`` (``_export_loop``, "Metrics export stopped",
#                           the ``_await_password_change`` "starting ... export" line).
# ``app.admin.api`` is intentionally excluded: ``_tsdb_view`` only assembles the status dict and logs
# nothing export-specific; that status stays on the page as a separate tile, never as log output.
TSDB_LOGGER_PREFIXES: tuple[str, ...] = ("app.export", "app.api.app_factory")

# Backlog ring buffer size (recent events replayed on connect) and the full-queue drop behaviour.
_RING_SIZE = 500
TSDB_LOG_POLICY = BacklogPolicy(backlog_size=_RING_SIZE, drop=DropPolicy.DROP_OLDEST)


def is_tsdb_logger(name: str) -> bool:
    """Whether a logger ``name`` is TSDB-relevant by prefix match against :data:`TSDB_LOGGER_PREFIXES`."""
    return any(name == prefix or name.startswith(prefix + ".") for prefix in TSDB_LOGGER_PREFIXES)


def build_log_event(record: logging.LogRecord) -> dict[str, str]:
    """The ``tsdb.log`` payload; only these fields, never the raw ``LogRecord``."""
    return {
        "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage(),
    }


def register_tsdb_log_type(hub: EventHub) -> None:
    """Declare the ``tsdb.log`` type (ring buffer + drop policy) on the hub."""
    hub.register_type(TSDB_LOG_EVENT, TSDB_LOG_POLICY)


class TsdbLogHandler(logging.Handler):
    """A root-logger handler that publishes TSDB-relevant records to the hub as ``tsdb.log`` events.

    It only reads records and never logs, so attaching it alongside the stdout handler cannot create
    a feedback loop. Any failure is routed through ``handleError`` so a logging call never raises.
    """

    def __init__(self, hub: EventHub) -> None:
        super().__init__(level=logging.NOTSET)  # let the root level gate what reaches us
        self._hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if is_tsdb_logger(record.name):
                self._hub.publish(TSDB_LOG_EVENT, build_log_event(record))
        except Exception:  # noqa: BLE001 - a handler must never raise into the logging call
            self.handleError(record)
