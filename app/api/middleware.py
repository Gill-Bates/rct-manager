#!/usr/bin/env python3
#
# app/api/middleware.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Correlation id, ``Cache-Control: no-store`` and request logging (Requirement 14.8, 16.14, 25.8 to 25.11)."""

import logging
import re
import time
import uuid

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.problems import ErrorCode, build_problem
from app.logging_setup import correlation_id
from app.observability.stats import ServiceCounters

log = logging.getLogger("app.access")
_VALID_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_API_PREFIXES = ("/api/", "/metrics", "/health")
_BROWSER_PROBES = frozenset({"/.well-known/appspecific/com.chrome.devtools.json"})  # Chrome DevTools asks on its own


def access_log_level(method: str, path: str, status: int) -> int:
    """Errors and warnings always show; admin actions (login, logout, password, settings, tokens)
    are INFO; plain browser traffic of the admin GUI (pages, assets, session polls) is DEBUG."""
    if status >= 500:
        return logging.ERROR
    if status == 404 and path in _BROWSER_PROBES:
        return logging.DEBUG
    if status >= 400:
        return logging.WARNING
    if path.startswith("/admin/api/") and method in _MUTATING:
        return logging.INFO
    if path == "/metrics":
        return logging.DEBUG
    if path.startswith(_API_PREFIXES):
        return logging.INFO
    return logging.DEBUG


def resolve_correlation_id(supplied: str | None) -> str:
    """Take the client value only when it is short and uses safe characters."""
    if supplied is not None and _VALID_ID.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Outermost layer: it also turns unexpected exceptions into a 500 Problem_Details."""

    def __init__(self, app: ASGIApp, header: str = "X-Request-Id", counters: ServiceCounters | None = None) -> None:
        self.app = app
        self._counters = counters
        self._header = header.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = next((v.decode("latin-1") for k, v in scope["headers"] if k == self._header), None)
        cid = resolve_correlation_id(supplied)
        scope["correlation_id"] = cid
        if self._counters is not None:
            self._counters.requests += 1
        token = correlation_id.set(cid)
        started = time.perf_counter()
        status = 500
        sent = False

        async def wrapped(message: Message) -> None:
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status = message["status"]
                sent = True
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-store"))
                if not any(k.lower() == self._header for k, _ in headers):
                    headers.append((self._header, cid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            try:
                await self.app(scope, receive, wrapped)
            except Exception:
                log.exception("Unhandled error while processing the request")
                if sent:
                    raise
                response = build_problem(Request(scope), ErrorCode.INTERNAL_ERROR)
                await response(scope, receive, wrapped)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            log.log(
                access_log_level(scope["method"], scope["path"], status),
                "%s %s -> %d in %.1f ms (token %s)",
                scope["method"],
                scope["path"],
                status,
                duration_ms,
                scope.get("token_id") or "-",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": duration_ms,
                    "token_id": scope.get("token_id"),
                },
            )
            correlation_id.reset(token)
