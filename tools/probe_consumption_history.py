#!/usr/bin/env python3
#
# tools/probe_consumption_history.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Read-only hardware diagnostic for the RCT logger consumption-history registers.

This standalone CLI probes an inverter's logger history time series so an operator can verify the
unit, the day/time assignment and the retained history depth *before* the 60-day consumption
backfill is enabled (``Settings.consumption_daily_backfill_enabled``). This tool NEVER touches that
gate, any production start path or the UI; it only reads.

The two metrics probed are:

    logger_day_eload_log_ts      (used by the day backfill; one point per day assumed)
    logger_minutes_eload_log_ts  (not yet used anywhere; its grid/unit must be clarified first)

STRICTLY READ-ONLY. The only frame sent is the logger history query, which is a WRITE-frame on the
wire that carries a timestamp but stores nothing on the device (see
``RctGateway.query_history_series`` and ``app/protocol/timeseries.py``). This tool emits no other
frame; in particular it NEVER writes ``logger_log_rate`` or any other register. A runtime guard
(``_ReadOnlyGuard``) asserts that every frame leaving the transport is such a history query and
aborts otherwise.

Host and port come ONLY from the command line. This tool does not import or read ``setup.conf`` or
any other configuration file, and it never writes the device address (or any credential) into its
output, logs or the report file.

Usage::

    python tools/probe_consumption_history.py --host 192.168.x.y --port 8899 --pages 4
    python tools/probe_consumption_history.py --check     # dry self-check, opens no socket
    python tools/probe_consumption_history.py --help
"""

import argparse
import asyncio
import itertools
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

# The tool lives in tools/; make the project importable when run as a script.
_PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from app.admin.consumption_backfill import PAUSE_BETWEEN_QUERIES_SECONDS
from app.catalog.registry import RegistryCatalog
from app.clock import SystemClock
from app.config import DeviceEntry, EndpointKey
from app.errors import DeviceApiError, UnknownMetric
from app.gateway.cache import MemoryCache
from app.gateway.rct import DeviceBinding, RctGateway
from app.gateway.rct_history import DAY_LOAD_LOG_METRIC
from app.protocol.frames import Frame
from app.protocol.timeseries import MAX_TIMESERIES_POINTS, TimeSeriesPoint
from app.protocol.types import (
    WRITE_COMMANDS,
    Command,
    DataType,
    FrameKind,
)
from app.scheduling.serializer import AccessSerializer
from app.security.allowlist import Allowlist
from app.transport.endpoint import EndpointConfig, TransportEndpoint

MINUTES_LOAD_LOG_METRIC = "logger_minutes_eload_log_ts"
DEFAULT_METRICS = (DAY_LOAD_LOG_METRIC, MINUTES_LOAD_LOG_METRIC)
DEFAULT_PAGES = 4
_DEVICE_ID = "probe"  # internal only; the device address is never recorded
_REPORT_PATH = (
    _PROJECT_DIR / ".agents" / "tasks" / "consumption-history-probe-report.md"
)


class ReadOnlyViolation(RuntimeError):
    """A frame other than the read-only history query was about to be sent; the probe aborts."""


class _ReadOnlyGuard:
    """Wraps the transport's value sink to assert the tool only ever emits history queries.

    The guard is installed as the endpoint's ``on_value`` callback purely so an object exists to
    hold the invariant close to the transport; the real enforcement is ``inspect``, called before
    the gateway submits any transaction. Any command/object that is not the exact history query of
    a probed metric raises ``ReadOnlyViolation`` so a stray write can never reach the device.
    """

    def __init__(self, allowed_object_ids: frozenset[int]) -> None:
        self._allowed_object_ids = allowed_object_ids
        self.frames_sent = 0

    def inspect(self, command: Command, object_id: int) -> None:
        # The history query is a WRITE-frame carrying a timestamp (read-only on the device). It is
        # the ONLY frame this tool is allowed to emit. logger_log_rate and every other register are
        # rejected here before anything leaves the socket.
        if command is not Command.WRITE:
            raise ReadOnlyViolation(
                f"refusing to send command 0x{int(command):02x}; only the read-only history query is allowed"
            )
        if command not in WRITE_COMMANDS:  # defensive: WRITE must be classified as such
            raise ReadOnlyViolation("history query is not a WRITE command")
        if object_id not in self._allowed_object_ids:
            raise ReadOnlyViolation(
                f"refusing to touch object 0x{object_id:08x}; only the probed history metrics are allowed"
            )
        self.frames_sent += 1

    def on_value(self, _frame: Frame, _kind: FrameKind) -> None:
        # Received periodic values are irrelevant to a history probe; drop them.
        return None


class _GuardedGateway(RctGateway):
    """RctGateway that inspects every outgoing history query against the read-only guard."""

    def __init__(self, *args: object, guard: _ReadOnlyGuard, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._guard = guard

    async def query_history_series(
        self, device_id: str, name: str, before: datetime
    ) -> tuple[TimeSeriesPoint, ...]:
        entry = self._catalog.object_entry(name)
        # Assert read-only BEFORE the frame is built/submitted. The gateway sends exactly a WRITE
        # frame for this object id (the read-only history query); nothing else is permitted.
        self._guard.inspect(Command.WRITE, entry.object_id)
        return await super().query_history_series(device_id, name, before)


# --- analysis helpers (pure, hardware-free; unit-tested) ---------------------------------------


class MetricReport:
    """Collected per-metric observations across all fetched pages."""

    def __init__(self, metric: str) -> None:
        self.metric = metric
        self.pages: list[list[TimeSeriesPoint]] = []
        self.errors: list[str] = []
        self.before_empty_after_oldest: bool | None = None
        self.before_returned_duplicates: bool | None = None

    @property
    def all_points(self) -> list[TimeSeriesPoint]:
        return [point for page in self.pages for point in page]

    def unique_by_timestamp(self) -> dict[datetime, float]:
        return {point.timestamp: point.value for point in self.all_points}


def iso(stamp: datetime) -> str:
    return stamp.astimezone(UTC).isoformat()


def deltas(points: Sequence[TimeSeriesPoint]) -> list[timedelta]:
    """Signed deltas between consecutive timestamps in wire order."""
    ordered = sorted({point.timestamp for point in points})
    return [later - earlier for earlier, later in itertools.pairwise(ordered)]


def duplicate_timestamps(points: Sequence[TimeSeriesPoint]) -> list[datetime]:
    """Timestamps that appear more than once (within a page or across pages)."""
    counts = Counter(point.timestamp for point in points)
    return sorted(stamp for stamp, n in counts.items() if n > 1)


def infer_grid(points: Sequence[TimeSeriesPoint]) -> str:
    """Human-readable guess at the sampling resolution from the median delta."""
    gaps = [
        delta.total_seconds() for delta in deltas(points) if delta.total_seconds() > 0
    ]
    if not gaps:
        return "unknown (fewer than two distinct timestamps)"
    median = statistics.median(gaps)
    if abs(median - 86400.0) <= 3600.0:
        return f"~daily (median spacing {median / 3600.0:.1f} h)"
    if median >= 60.0:
        return f"~{median / 60.0:.1f} min spacing (median)"
    return f"~{median:.0f} s spacing (median)"


def value_magnitude_hint(points: Sequence[TimeSeriesPoint]) -> str:
    """A note on observed value magnitude, to sanity-check the assumed Wh unit."""
    values = [point.value for point in points]
    if not values:
        return "no points to judge the unit"
    lo, hi = min(values), max(values)
    median = statistics.median(values)
    return (
        f"min={lo:.3f}, median={median:.3f}, max={hi:.3f} "
        f"(Wh assumed; a daily household load is typically a few thousand to tens of thousands of Wh)"
    )


# --- probing (hardware) ------------------------------------------------------------------------


async def _probe_metric(
    gateway: RctGateway, metric: str, pages: int, clock: SystemClock
) -> MetricReport:
    """Walk ``pages`` backwards through one metric's history, read-only, pausing between queries."""
    report = MetricReport(metric)
    before = clock.now()
    for page in range(pages):
        if page:
            await clock.sleep(
                PAUSE_BETWEEN_QUERIES_SECONDS
            )  # gentle on the shared device queue
        try:
            points = await gateway.query_history_series(_DEVICE_ID, metric, before)
        except DeviceApiError as exc:
            report.errors.append(f"page {page}: {type(exc).__name__}: {exc}")
            break
        report.pages.append(list(points))
        if not points:
            report.before_empty_after_oldest = page > 0
            break
        oldest = min(point.timestamp for point in points)
        if oldest >= before:
            # before= did not move the window back: the log is exhausted or ignores the cursor.
            report.before_empty_after_oldest = False
            break
        before = oldest

    # Characterise the before= cursor against the overall oldest point.
    all_points = report.all_points
    if all_points and report.before_empty_after_oldest is None:
        oldest = min(point.timestamp for point in all_points)
        try:
            await clock.sleep(PAUSE_BETWEEN_QUERIES_SECONDS)
            older = await gateway.query_history_series(_DEVICE_ID, metric, oldest)
        except DeviceApiError as exc:
            report.errors.append(f"before-probe: {type(exc).__name__}: {exc}")
        else:
            seen = report.unique_by_timestamp()
            strictly_older = [point for point in older if point.timestamp < oldest]
            report.before_empty_after_oldest = not strictly_older
            report.before_returned_duplicates = any(
                point.timestamp in seen for point in older
            )
    return report


# --- rendering ---------------------------------------------------------------------------------


def _render_metric(report: MetricReport) -> list[str]:
    lines: list[str] = [f"### Metric `{report.metric}`", ""]
    if report.errors:
        lines.append("Errors during probing:")
        lines.extend(f"- {err}" for err in report.errors)
        lines.append("")

    for index, page in enumerate(report.pages):
        lines.append(f"Page {index}: {len(page)} point(s)")
        for point in page:
            lines.append(
                f"  - {iso(point.timestamp)}  raw/decoded value={point.value!r}"
            )
        page_deltas = [str(delta) for delta in deltas(page)]
        if page_deltas:
            lines.append(f"  deltas: {', '.join(page_deltas)}")
        lines.append("")

    all_points = report.all_points
    if not all_points:
        lines.append("_No points returned; see errors above or run against hardware._")
        lines.append("")
        return lines

    unique = report.unique_by_timestamp()
    oldest = min(unique)
    newest = max(unique)
    dupes = duplicate_timestamps(all_points)
    lines.extend(
        [
            "#### Summary",
            f"- points total (incl. duplicates): {len(all_points)}",
            f"- distinct timestamps: {len(unique)}",
            f"- points per page: {[len(page) for page in report.pages]} (MAX_TIMESERIES_POINTS={MAX_TIMESERIES_POINTS})",
            f"- oldest point: {iso(oldest)} = {unique[oldest]!r}",
            f"- newest point: {iso(newest)} = {unique[newest]!r}",
            f"- history depth reached: {(newest - oldest)} (newest minus oldest)",
            f"- inferred grid/resolution: {infer_grid(all_points)}",
            f"- value magnitude / unit hint: {value_magnitude_hint(all_points)}",
            f"- duplicate timestamps: {len(dupes)}"
            + (f" (e.g. {iso(dupes[0])})" if dupes else " (none detected)"),
            _before_summary(report),
            _day_assignment_hint(report, unique),
            "",
        ]
    )
    return lines


def _before_summary(report: MetricReport) -> str:
    if report.before_empty_after_oldest is None:
        return "- before= behaviour: not characterised (no hardware run)"
    older = (
        "empty / not strictly older"
        if report.before_empty_after_oldest
        else "strictly older data"
    )
    dupes = (
        "unknown"
        if report.before_returned_duplicates is None
        else ("yes" if report.before_returned_duplicates else "no")
    )
    return f"- before=<oldest-ts> returns: {older}; duplicates across the boundary: {dupes}"


def _day_assignment_hint(report: MetricReport, unique: dict[datetime, float]) -> str:
    if report.metric != DAY_LOAD_LOG_METRIC:
        return "- day/time assignment: n/a for a sub-daily metric; verify interval-begin vs interval-end from the grid"
    # Does each day point sit on a midnight boundary (interval edge) in UTC? The operator should
    # re-read this against the SITE-LOCAL zone; UTC is only the raw wire view here.
    midnights = sum(1 for stamp in unique if stamp.time() == datetime.min.time())
    return (
        f"- day/time assignment: {midnights}/{len(unique)} points fall on a 00:00:00 UTC boundary; "
        "compare against the site-local midnight to decide interval-begin vs interval-end"
    )


def _render_report(reports: list[MetricReport], *, pages: int, live: bool) -> str:
    header = [
        "# Consumption-history probe report",
        "",
        "Read-only diagnostic of the RCT logger consumption-history registers. This report records",
        "NO host, port or credential: the device address is supplied via CLI, not recorded.",
        "",
        f"- generated at: {datetime.now(UTC).isoformat()}",
        f"- pages per metric requested: {pages}",
        f"- metrics probed: {', '.join(report.metric for report in reports)}",
        "- mode: "
        + (
            "live hardware run"
            if live
            else "TEMPLATE / skeleton (no hardware; run the command below)"
        ),
        "",
        "This probe is strictly read-only: the only frame it emits is the logger history query, a",
        "WRITE-frame carrying a timestamp that stores nothing on the device. It never writes",
        "`logger_log_rate` or any other register, and it does not touch",
        "`consumption_daily_backfill_enabled`.",
        "",
        "## Operator run command",
        "",
        "```bash",
        "python tools/probe_consumption_history.py \\",
        "  --host <DEVICE_HOST> \\",
        "  --port 8899 \\",
        f"  --pages {pages}",
        "```",
        "",
        "Replace `<DEVICE_HOST>` with the inverter's address at runtime. Host and port are read from",
        "these arguments only and are never written to this file.",
        "",
        "## Per-metric observations",
        "",
    ]
    body: list[str] = []
    for report in reports:
        body.extend(_render_metric(report))
    if not live:
        body.append(
            "_Skeleton rendered without hardware. Run the command above against the device to fill "
            "the per-metric pages, grid, retention and before= behaviour._"
        )
    return "\n".join(header + body) + "\n"


# --- gateway construction (single device, single transport) ------------------------------------


async def _run_live(
    args: argparse.Namespace, catalog: RegistryCatalog, metrics: list[str]
) -> int:
    clock = SystemClock()
    allowed_ids = frozenset(catalog.object_entry(name).object_id for name in metrics)
    guard = _ReadOnlyGuard(allowed_ids)
    gateway = _GuardedGateway(
        catalog,
        MemoryCache(ttl_seconds=0.0, grace_seconds=0.0),
        clock,
        Allowlist({}, catalog),
        guard=guard,
    )
    key = EndpointKey(args.host, args.port)
    entry = DeviceEntry(
        device_id=_DEVICE_ID, host=args.host, port=args.port, network_id=args.network_id
    )
    config = EndpointConfig(device_ids=(_DEVICE_ID,))
    endpoint = TransportEndpoint(
        _DEVICE_ID, key, config, clock, on_value=guard.on_value
    )
    serializer = AccessSerializer(endpoint, gateway.handler(endpoint))
    gateway.add_device(DeviceBinding(entry, endpoint, serializer))
    serializer.start()
    reports: list[MetricReport] = []
    try:
        await endpoint.start()
        for metric in metrics:
            reports.append(await _probe_metric(gateway, metric, args.pages, clock))
    finally:
        await serializer.stop()
        await endpoint.close()

    rendered = _render_report(reports, pages=args.pages, live=True)
    sys.stdout.write(rendered)
    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(rendered, encoding="utf-8")
    sys.stdout.write(f"\nReport written to {_REPORT_PATH}\n")
    return 0


def _resolve_metrics(catalog: RegistryCatalog, requested: str | None) -> list[str]:
    """Resolve and validate the metrics to probe against the shipped catalog."""
    metrics = [requested] if requested else list(DEFAULT_METRICS)
    for metric in metrics:
        entry = catalog.object_entry(metric)  # raises UnknownMetric if absent
        if entry.response_type is not DataType.TIMESERIES:
            raise UnknownMetric(name=metric)
    return metrics


def _check(args: argparse.Namespace, catalog: RegistryCatalog) -> int:
    """Dry self-check: validate catalog lookups and the skeleton render WITHOUT opening a socket."""
    sys.stdout.write("Dry self-check (no socket opened)\n")
    ok = True
    for metric in (args.metric,) if args.metric else DEFAULT_METRICS:
        try:
            entry = catalog.object_entry(metric)
        except UnknownMetric:
            sys.stdout.write(f"  MISSING  {metric}: not in the catalog\n")
            ok = False
            continue
        is_ts = entry.response_type is DataType.TIMESERIES
        ok = ok and is_ts
        sys.stdout.write(
            f"  {'OK      ' if is_ts else 'BAD TYPE'} {metric}: "
            f"object_id=0x{entry.object_id:08x} response_type={entry.response_type}\n"
        )
    metrics = _resolve_metrics(catalog, args.metric) if ok else list(DEFAULT_METRICS)
    skeleton = _render_report(
        [MetricReport(m) for m in metrics], pages=args.pages, live=False
    )
    sys.stdout.write(f"  render OK ({len(skeleton)} chars of skeleton report)\n")
    sys.stdout.write("Self-check passed\n" if ok else "Self-check FAILED\n")
    return 0 if ok else 1


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="probe_consumption_history.py",
        description=(
            "Read-only probe of the RCT logger consumption-history registers. Host and port come "
            "ONLY from these arguments and are never recorded. Strictly read-only: never writes "
            "logger_log_rate or any register, and never touches the backfill gate."
        ),
    )
    parser.add_argument("--host", help="device host/IP (runtime only; never recorded)")
    parser.add_argument(
        "--port", type=int, default=8899, help="device port (default 8899)"
    )
    parser.add_argument(
        "--network-id",
        type=int,
        default=None,
        help="optional plant network id of the device",
    )
    parser.add_argument(
        "--metric",
        default=None,
        help="probe a single metric instead of both defaults "
        f"({' and '.join(DEFAULT_METRICS)})",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=DEFAULT_PAGES,
        help=f"backwards pagination pages per metric (default {DEFAULT_PAGES})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="dry self-check of catalog lookup and argument parsing; opens no socket",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.pages < 1:
        sys.stderr.write("error: --pages must be at least 1\n")
        return 2
    catalog = RegistryCatalog.from_file(
        _PROJECT_DIR / "app" / "catalog" / "objects.json"
    )

    if args.check:
        return _check(args, catalog)

    if not args.host:
        sys.stderr.write(
            "error: --host is required for a live probe (use --check for a dry run)\n"
        )
        return 2

    try:
        metrics = _resolve_metrics(catalog, args.metric)
    except UnknownMetric as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    return asyncio.run(_run_live(args, catalog, metrics))


if __name__ == "__main__":
    raise SystemExit(main())
