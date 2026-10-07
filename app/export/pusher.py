#!/usr/bin/env python3
#
# app/export/pusher.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Periodic push of the Prometheus samples as line protocol; failures never reach the API."""

import asyncio
import base64
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from app.clock import Clock
from app.config import DbType, Settings
from app.export.lineprotocol import batches, build_lines
from app.export.questdb_admin import QuestDbProvisioner
from app.observability.exporter import MetricsExporter
from app.observability.stats import ServiceCounters

log = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 10.0
MAX_BACKOFF_SECONDS = 300.0
_BODY_LIMIT = 300
_RESPONSE_LIMIT = 1_048_576  # bounded read of QuestDB /exec JSON replies


class PushError(Exception):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Target:
    base_url: str
    write_path: str
    headers: dict[str, str]
    verify_tls: bool
    exec_path: str | None = None  # QuestDB SQL endpoint

    @classmethod
    def from_settings(cls, settings: Settings) -> "Target":
        endpoint = settings.export_endpoint()
        if endpoint is None:
            raise ValueError("export is disabled")
        if settings.db_type is DbType.INFLUXDB_V2:
            query = urlencode(
                {"org": settings.influxdb_organization, "bucket": settings.influxdb_bucket, "precision": "ns"},
                quote_via=quote,
            )
            token = settings.influxdb_token.get_secret_value() if settings.influxdb_token else ""
            return cls(endpoint.base_url, f"/api/v2/write?{query}", {"Authorization": f"Token {token}"},
                       settings.influxdb_verify_tls)
        headers: dict[str, str] = {}
        if settings.questdb_username and settings.questdb_password:
            raw = f"{settings.questdb_username}:{settings.questdb_password.get_secret_value()}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        return cls(endpoint.base_url, "/write", headers, settings.questdb_verify_tls, exec_path="/exec")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the Authorization header is never forwarded to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


def _request(target: Target, path: str, data: bytes | None) -> bytes:
    request = urllib.request.Request(target.base_url + path, data=data, headers=target.headers)
    if data is not None:
        request.add_header("Content-Type", "text/plain; charset=utf-8")
    context = None
    if target.base_url.startswith("https") and not target.verify_tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read(_RESPONSE_LIMIT)
    except urllib.error.HTTPError as exc:
        detail = exc.read(_BODY_LIMIT).decode("utf-8", "replace").strip()
        if exc.code in (401, 403):
            raise PushError(f"authentication failed (HTTP {exc.code})", retryable=False) from None
        # 4xx other than throttling means the batch itself is bad; resending would not help
        raise PushError(f"HTTP {exc.code}: {detail}", retryable=exc.code in (408, 429) or exc.code >= 500) from None
    except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        reason = getattr(exc, "reason", exc)
        raise PushError(f"connection failed: {type(reason).__name__}: {reason}") from None


def write(target: Target, body: str) -> None:
    _request(target, target.write_path, body.encode("utf-8"))


def questdb_exec(target: Target, sql: str) -> dict:
    raw = _request(target, f"{target.exec_path}?{urlencode({'query': sql})}", None)
    try:
        data = json.loads(raw)
    except ValueError:
        raise PushError("QuestDB returned an invalid SQL response") from None
    if isinstance(data, dict) and data.get("error"):
        raise PushError(f"QuestDB rejected SQL: {data['error']}", retryable=False)
    return data if isinstance(data, dict) else {}


class PushExporter:
    def __init__(self, settings: Settings, exporter: MetricsExporter, stats: ServiceCounters, clock: Clock) -> None:
        self._settings = settings
        self._exporter = exporter
        self._stats = stats
        self._clock = clock
        self._target = Target.from_settings(settings)
        self._failures = 0
        self._provisioner: QuestDbProvisioner | None = None
        if settings.db_type is DbType.QUESTDB:
            target = self._target
            self._provisioner = QuestDbProvisioner(
                lambda sql: questdb_exec(target, sql), settings.questdb_measurement_name,
                settings.questdb_downsampling.value, settings.questdb_raw_retention_days,
                settings.questdb_retention_days,
            )

    @property
    def measurement(self) -> str:
        s = self._settings
        return s.influxdb_measurement_name if s.db_type is DbType.INFLUXDB_V2 else s.questdb_measurement_name

    def _push_lines(self, lines: list[str]) -> None:
        """Blocking I/O only; runs in a worker thread with already built, immutable lines."""
        if self._provisioner is not None:
            self._provisioner.ensure_tables()  # symbol columns exist before the first line arrives
        for body in batches(lines):
            write(self._target, body)

    async def cycle(self) -> float:
        """One export; returns the delay until the next one (grows with consecutive failures)."""
        interval = float(self._settings.metrics_export_interval_seconds)
        try:
            # collect() reads loop-owned state (queues, budget, counters), so it stays on the loop.
            lines = build_lines(self._exporter.collect(), self.measurement, time.time_ns())
            await asyncio.to_thread(self._push_lines, lines)
            if self._provisioner is not None:
                # Re-run every cycle: a newly exported metric adds a DOUBLE column at any time, and
                # run() derives the rollup view name from the live column set, so a stale rollup
                # projection is replaced instead of kept once provisioning first succeeded.
                await asyncio.to_thread(self._provisioner.run)
        except PushError as exc:
            self._record_failure(str(exc))
            if not exc.retryable:
                return MAX_BACKOFF_SECONDS
        except Exception:
            log.exception("Metrics export failed unexpectedly")
            self._record_failure("unexpected error")
        else:
            if self._failures:
                log.info("Metrics export recovered after %d failed attempt(s)", self._failures)
            self._failures = 0
            self._stats.export_success += 1
            self._stats.export_last_success_unix = time.time()
            return interval
        return min(interval * 2 ** min(self._failures, 8), max(interval, MAX_BACKOFF_SECONDS))

    def _record_failure(self, message: str) -> None:
        self._failures += 1
        self._stats.export_failures += 1
        # First failure and then every power of two: visible without flooding the log.
        if self._failures & (self._failures - 1) == 0:
            log.warning("Metrics export failed (attempt %d): %s", self._failures, message)

    async def run(self) -> None:
        log.info("Metrics export to %s enabled, every %ds", self._settings.db_type.value,
                 self._settings.metrics_export_interval_seconds)
        while True:
            delay = await self.cycle()
            await self._clock.sleep(delay)
