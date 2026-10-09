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
# How often the QuestDB rollup health is re-checked once its schema is provisioned, so a view that
# regresses to invalid or stops refreshing is noticed while the shortened raw TTL is still active.
PROVISION_RECHECK_SECONDS = 300.0
_BODY_LIMIT = 300
_RESPONSE_LIMIT = 1_048_576  # bounded read of QuestDB /exec JSON replies


def _backoff(interval: float, failures: int) -> float:
    """Delay after ``failures`` consecutive failures: doubling from ``interval``, capped."""
    return min(interval * 2 ** min(failures, 8), max(interval, MAX_BACKOFF_SECONDS))


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
        self._tables_ready = False
        self._provisioned_columns: frozenset[str] | None = None
        self._provision_failures = 0
        self._provision_retry_at = 0.0
        self._provision_recheck_at = 0.0
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
        if self._provisioner is not None and not self._tables_ready:
            self._provisioner.ensure_tables()  # symbol columns exist before the first line arrives
            self._tables_ready = True
        for body in batches(lines):
            write(self._target, body)

    async def cycle(self) -> float:
        """One export; returns the delay until the next one (grows with consecutive failures)."""
        interval = float(self._settings.metrics_export_interval_seconds)
        try:
            # collect() reads loop-owned state (queues, budget, counters), so it stays on the loop.
            samples = list(self._exporter.collect())
            lines = build_lines(samples, self.measurement, time.time_ns())
            await asyncio.to_thread(self._push_lines, lines)
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
            # Only a cycle that actually sent at least one line counts as a successful *export*: an
            # empty build_lines() (no samples, or all non-finite) makes no write() call, so marking
            # it as a last-success would let the TSDB status show "healthy / recently exported" when
            # not a byte reached the database.
            if lines:
                self._stats.export_last_success_unix = time.time()
            await self._provision(frozenset(name for name, _, _ in samples) | {k for _, t, _ in samples for k in t})
            return interval
        return _backoff(interval, self._failures)

    async def _provision(self, columns: frozenset[str]) -> None:
        """Retention and rollup health after a stored write; its failures never taint the export.

        Two concerns, deliberately separated: the schema DDL (``CREATE MATERIALIZED VIEW``) only
        needs to run when the column set changes, but the rollup *health* must keep being checked.
        The provisioner's ``run()`` does both — its DDL is ``IF NOT EXISTS`` (idempotent) and it
        re-checks ``_view_current`` and reconciles the TTL every time — so this method re-invokes it
        periodically even when the columns are unchanged. Without that re-check a view that turns
        ``invalid`` or stops refreshing days later goes unnoticed while the shortened raw TTL keeps
        deleting data no working rollup holds. A changed column set runs immediately (bypassing the
        throttle); an unchanged set runs on a periodic cadence.
        """
        if self._provisioner is None:
            return
        now = self._clock.monotonic()
        schema_changed = columns != self._provisioned_columns
        if not schema_changed and now < self._provision_recheck_at:
            return
        if now < self._provision_retry_at:
            return
        try:
            ready = await asyncio.to_thread(lambda: self._provisioner.run(ensure=False))
        except Exception as exc:  # noqa: BLE001 - provisioning must never fail the export
            self._provision_failures += 1
            interval = float(self._settings.metrics_export_interval_seconds)
            self._provision_retry_at = now + _backoff(interval, self._provision_failures)
            if self._provision_failures & (self._provision_failures - 1) == 0:
                log.warning("QuestDB provisioning failed (attempt %d): %s", self._provision_failures, exc)
            return
        self._provision_failures = 0
        # Re-check the rollup health on a steady cadence regardless of the result: a ``ready`` view
        # can later regress, and a not-yet-ready one must keep being retried.
        self._provision_recheck_at = now + PROVISION_RECHECK_SECONDS
        if ready:
            self._provisioned_columns = columns

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
