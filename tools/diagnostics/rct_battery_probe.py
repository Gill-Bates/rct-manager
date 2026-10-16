#!/usr/bin/env python3
#
# tools/diagnostics/rct_battery_probe.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Hardware diagnosis of battery_soc_target (0x8B9FF008) and battery capacity candidates.

Default mode sends only normal READ frames and observes passively. Run from the repository root
with the rct-manager app STOPPED (one socket per device):

    python tools/diagnostics/rct_battery_probe.py --device home=192.168.1.50:8899 \
        --confirm-app-stopped --json-out probe.json --csv-out probe.csv

The tool reuses the app's transport layer (TransportEndpoint, AccessSerializer, RctGateway), so
connection handling and timeouts are the production ones. A guard around TransportEndpoint.execute
aborts on any command outside the mode's allow-list. It never writes, never starts a
PeriodicManager, never touches app databases or settings and never prints secrets.

The INVASIVE periodic test (--invasive-periodic-test) registers READ_PERIODICALLY on the device;
see tools/diagnostics/README.md before using it.
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

if "app" not in sys.modules:
    sys.path.insert(0, os.getcwd())  # run from the repository root

from app.api.app_factory import (
    _MODULE_INVENTORY_NAMES,
    _build,
    _effective_periodic_names,
)
from app.clock import SystemClock
from app.config import DeviceEntry, Settings, parse_devices
from app.errors import DeviceApiError, DeviceTimeout, ProtocolError
from app.gateway.battery_inventory import (
    _battery_module_slot_states,
    _battery_tower_present,
    _raw_battery_module_report,
)
from app.protocol.types import Command
from app.transport.endpoint import EndpointState
from app.transport.types import TransactionOrigin, TransactionRequest, make_frame

SOC_TARGET = "battery_soc_target"
HOUSEHOLD = "household_load_power"
CAPACITY_NAMES = (
    "power_mng_n_batteries",
    "power_mng_amp_hours_measured",
    "battery_ah_capacity",
    "battery_placeholder_0_ah_capacity",
)
TOWER1_CHARACTERIZATION = ("battery_soc", "battery_voltage")
TOWER2_CHARACTERIZATION = ("battery_placeholder_0_soc", "battery_placeholder_0_voltage")
ENERGY_NAMES = ("battery_stored_energy", "battery_used_energy")  # characterization only
IDENTITY_NAMES = ("android_description", "svnversion")  # model / firmware; no serial number
READ_COMMANDS = frozenset({Command.READ})
INVASIVE_COMMANDS = frozenset({Command.READ, Command.READ_PERIODICALLY})
CONFIRM_INVASIVE = "--i-understand-this-registers-periodic-reads"
INVASIVE_BANNER = (
    "INVASIVE TEST ACTIVE: READ_PERIODICALLY registrations are sent to the device. The device keeps\n"
    "them until the connection ends (cleanup semantics unclear) and they may recreate the stale\n"
    "periodic subscriptions under investigation. Use a freshly restarted device/app window."
)
CSV_FIELDS = (
    "phase",
    "metric",
    "object_id",
    "status",
    "error_code",
    "raw_value",
    "value",
    "unit",
    "timestamp_utc",
    "duration_s",
    "t_since_start_s",
    "t_since_connect_s",
    "epoch_before",
    "epoch_after",
    "reconnect_triggered",
)
_DROP_RE = re.compile(r"no response to object (0x[0-9a-fA-F]{8})")


@dataclass
class Probe:
    gateway: Any
    catalog: Any
    endpoint: Any
    device_id: str
    device_key: Any
    start: float = field(default_factory=time.monotonic)
    connect_at: float = 0.0
    epoch_first_seen: dict[int, float] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    reads: list[dict] = field(default_factory=list)

    def t(self) -> float:
        return time.monotonic() - self.start

    def log_event(self, kind: str, **data: Any) -> None:
        self.events.append({"t_s": round(self.t(), 3), "kind": kind, "epoch": self.endpoint.connection_epoch, **data})


class _EventHandler(logging.Handler):
    """Collects the endpoint's connection log lines as structured events."""

    def __init__(self, probe: Probe) -> None:
        super().__init__(logging.INFO)
        self._probe = probe

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        if not text.startswith(("Connection to", "Initial connection", "Endpoint ", "Bootloader")):
            return
        match = _DROP_RE.search(text)
        self._probe.log_event(
            "connection_dropped_no_response" if match else "endpoint_log",
            object_id=match.group(1) if match else None,
            message=text,
        )


def install_command_guard(endpoint: Any, allowed: frozenset[Command]) -> None:
    """Refuse every frame outside the allow-list, before it reaches the socket."""
    original = endpoint.execute

    async def guarded(request: TransactionRequest, **kwargs: Any):
        if request.frame.command not in allowed:
            raise RuntimeError(f"command guard: refused command 0x{int(request.frame.command):02x}")
        return await original(request, **kwargs)

    endpoint.execute = guarded


def _status_for(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, DeviceTimeout):
        return "timeout", exc.code
    if isinstance(exc, ProtocolError):
        return "protocol_error", exc.code
    if isinstance(exc, DeviceApiError):
        return "error", exc.code
    return "error", type(exc).__name__


async def timed_read(probe: Probe, name: str, phase: str) -> dict:
    """One normal READ (uncached, budget-exempt) with timing, epoch and reconnect bookkeeping."""
    entry = probe.catalog.object_entry(name)
    epoch_before = probe.endpoint.connection_epoch
    drops_before = sum(1 for e in probe.events if e["kind"] == "connection_dropped_no_response")
    started_abs = probe.t()
    started = time.monotonic()
    status, code, value, unit, stamp = "success", "", None, entry.unit, None
    try:
        reading = await probe.gateway.read_system(probe.device_id, name)
        value, unit, stamp = reading.value, reading.unit, reading.measured_at.isoformat()
    except Exception as exc:  # noqa: BLE001  # every failure is data here
        status, code = _status_for(exc)
    duration = time.monotonic() - started
    epoch_after = probe.endpoint.connection_epoch
    if epoch_after != epoch_before or epoch_after not in probe.epoch_first_seen:
        probe.epoch_first_seen.setdefault(epoch_after, started_abs)
    dropped = sum(1 for e in probe.events if e["kind"] == "connection_dropped_no_response") > drops_before
    raw = value
    if isinstance(value, int | float) and not isinstance(value, bool) and entry.scale != 1.0:
        raw = value / entry.scale
    row = {
        "phase": phase,
        "metric": name,
        "object_id": f"0x{entry.object_id:08X}",
        "status": status,
        "error_code": code,
        "raw_value": raw,
        "value": value,
        "unit": unit,
        "timestamp_utc": stamp or datetime.now(UTC).isoformat(),
        "duration_s": round(duration, 3),
        "t_since_start_s": round(started_abs, 3),
        "t_since_connect_s": round(started_abs - probe.connect_at, 3),
        "t_since_epoch_s": round(started_abs - probe.epoch_first_seen.get(epoch_after, started_abs), 3),
        "epoch_before": epoch_before,
        "epoch_after": epoch_after,
        "reconnect_triggered": dropped,
    }
    probe.reads.append(row)
    print(
        f"[{row['t_since_start_s']:7.1f}s] {phase:<14} {name:<36} {status:<14} {duration:5.2f}s value={value}",
        file=sys.stderr,
        flush=True,
    )
    return row


async def periodic_registration_probe(probe: Probe, name: str, phase: str) -> dict:
    """Send READ_PERIODICALLY for one object and observe the immediate first value.

    No pas.period WRITE is sent (writes are forbidden), so only the registration answer is
    judged, not the later periodic pushes. The registration is withdrawn locally afterwards; the
    device forgets it when the connection ends.
    """
    entry = probe.catalog.object_entry(name)
    epoch_before = probe.endpoint.connection_epoch
    drops_before = sum(1 for e in probe.events if e["kind"] == "connection_dropped_no_response")
    started_abs = probe.t()
    started = time.monotonic()
    probe.endpoint.register_periodic(probe.device_key, entry.object_id)
    request = TransactionRequest(
        probe.device_key,
        make_frame(probe.device_key.network_id, Command.READ_PERIODICALLY, entry.object_id),
        TransactionOrigin.SYSTEM_READ,
        "read",
        SystemClock().now(),
    )
    status, code = "success", ""
    try:
        result = await probe.gateway._device(probe.device_id).serializer.submit(request)
        if result.error is not None:
            status, code = _status_for(result.error)
        elif result.frame is None:
            status, code = "error", "no_frame"
    except Exception as exc:  # noqa: BLE001
        status, code = _status_for(exc)
    finally:
        probe.endpoint.unregister_periodic(probe.device_key, entry.object_id)
    dropped = sum(1 for e in probe.events if e["kind"] == "connection_dropped_no_response") > drops_before
    row = {
        "phase": phase,
        "metric": name,
        "object_id": f"0x{entry.object_id:08X}",
        "status": status,
        "error_code": code,
        "raw_value": None,
        "value": None,
        "unit": entry.unit,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "duration_s": round(time.monotonic() - started, 3),
        "t_since_start_s": round(started_abs, 3),
        "t_since_connect_s": round(started_abs - probe.connect_at, 3),
        "epoch_before": epoch_before,
        "epoch_after": probe.endpoint.connection_epoch,
        "reconnect_triggered": dropped,
        "command": "READ_PERIODICALLY",
    }
    probe.reads.append(row)
    print(
        f"[{row['t_since_start_s']:7.1f}s] {phase:<14} {name:<36} {status:<14} {row['duration_s']:5.2f}s (periodic)",
        file=sys.stderr,
        flush=True,
    )
    return row


def topology(probe: Probe, passes: int) -> dict:
    runtime = SimpleNamespace(gateway=probe.gateway, catalog=probe.catalog)
    towers = {}
    for key, prefix in (("tower_1", "battery"), ("tower_2", "battery_placeholder_0")):
        states = _battery_module_slot_states(runtime, probe.device_id, prefix)
        raw = _raw_battery_module_report(states)
        populated = raw["populated_module_slots"]
        towers[key] = {
            "prefix": prefix,
            "present": _battery_tower_present(runtime, probe.device_id, prefix, populated),
            "slot_states": states,
            "populated_module_slots": populated,  # slot indices only; serial numbers are not printed
            "module_count": raw["module_count"],
            "module_count_status": raw["module_count_status"],
            "inventory_passes": passes,
        }
    return towers


def periodic_set_status(probe: Probe, exposed: list[str] | None) -> dict:
    settings = Settings(_env_file=None, hmac_secret=secrets.token_urlsafe(32), devices=[])
    names = _effective_periodic_names(settings, probe.catalog, exposed)
    return {
        "basis": "repository defaults" if exposed is None else "--exposed-names",
        "note": "Your running instance may differ (admin-UI metric selection, METRICS_EXPOSED_NAMES).",
        "set_size": len(names),
        "battery_soc_target_in_set": SOC_TARGET in names,
        "household_load_power_in_set": HOUSEHOLD in names,
    }


def classify(soc_reads: list[dict], periodic: list[dict], invasive: bool) -> dict:
    normal = [r for r in soc_reads if r.get("command") != "READ_PERIODICALLY"]
    bad = [r for r in normal if r["status"] != "success"]
    base = {"normal_reads": len(normal), "normal_failures": len(bad), "invasive": invasive}
    if not normal and not periodic:
        label, why = "undecided", "no battery_soc_target read was made"
    elif normal and len(bad) == len(normal):
        label, why = "C" if invasive else "read_unstable", "every normal READ failed"
    elif bad and all(r["t_since_connect_s"] < 45 for r in bad):
        label, why = "B" if invasive else "read_unstable_early", "normal READs failed only early after connect"
    elif bad:
        label, why = "undecided" if invasive else "read_unstable_mixed", "normal READs failed at mixed times"
    elif not invasive:
        label, why = "read_ok", "every normal READ succeeded; periodic behaviour is not tested in this mode"
    else:
        per_bad = [r for r in periodic if r["status"] != "success"]
        if per_bad:
            label, why = "A", "normal READs succeeded while the periodic registration failed"
        elif periodic:
            label, why = "D", "no problem reproduced in this run"
        else:
            label, why = "D?", "normal READs succeeded; no periodic registration was made"
        base.update(periodic_probes=len(periodic), periodic_failures=len(per_bad))
    legend = (
        "Hint from one run, not a decision. Cases A-D need the periodic comparison (invasive mode only)."
        if not invasive
        else "A normal ok/periodic unstable; B both unstable only at startup; C both persistently "
        "unstable; D not reproducible. A hint from one run, not a decision."
    )
    return {"hint": label, "reason": why, "legend": legend, **base}


async def run(args: argparse.Namespace) -> dict:
    entries = parse_devices(args.device)
    if len(entries) != 1:
        raise SystemExit("--device takes exactly one id=host:port[@network_id]")
    entry: DeviceEntry = entries[0]
    settings = Settings(
        _env_file=None,
        hmac_secret=secrets.token_urlsafe(32),
        devices=[entry],
        enable_periodic_reads=False,  # no PeriodicManager: nothing can write pas.period
        read_retries=0,  # one attempt per read, so each timeout is visible
        response_timeout_seconds=args.response_timeout,
    )
    clock = SystemClock()
    gateway, catalog, parts, _coordinator = _build(settings, clock, None)
    endpoint, serializer = parts.endpoints[0], parts.serializers[0]
    install_command_guard(endpoint, INVASIVE_COMMANDS if args.invasive_periodic_test else READ_COMMANDS)
    key = parts.device_endpoints[0][0].key
    probe = Probe(gateway, catalog, endpoint, entry.device_id, key)
    logger = logging.getLogger("app.transport.endpoint")
    handler = _EventHandler(probe)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    serializer.start()
    try:
        await endpoint.start()
        if endpoint.state is not EndpointState.CONNECTED:
            raise SystemExit("initial connection failed; nothing to measure")
        probe.connect_at = probe.t()
        probe.epoch_first_seen[endpoint.connection_epoch] = probe.connect_at
        probe.log_event("connected")
        soc_reads: list[dict] = []
        household: list[dict] = []
        periodic_rows: list[dict] = []
        offsets = sorted(args.offsets)
        for index, offset in enumerate(offsets):
            await asyncio.sleep(max(0.0, probe.connect_at + offset - probe.t()))
            if index == 0 and args.invasive_periodic_first:
                row = await periodic_registration_probe(probe, SOC_TARGET, f"soc_T+{offset:g}_periodic")
                periodic_rows.append(row)
                soc_reads.append(row)
            else:
                soc_reads.append(await timed_read(probe, SOC_TARGET, f"soc_T+{offset:g}"))
            if not args.skip_household:
                household.append(await timed_read(probe, HOUSEHOLD, f"household_T+{offset:g}"))
        capacity = {name: await timed_read(probe, name, "capacity") for name in CAPACITY_NAMES}
        passes = 0
        towers: dict = {}
        if not args.skip_topology:
            for _ in range(6):  # an unanswered slot needs three failed passes; a second tower starts one pass later
                sent = await gateway.read_module_inventory(entry.device_id, _MODULE_INVENTORY_NAMES)
                passes += 1
                probe.log_event("inventory_pass", reads_sent=sent)
                if not sent:
                    break
            towers = topology(probe, passes)
        characterization = {}
        for name in TOWER1_CHARACTERIZATION:
            characterization[name] = await timed_read(probe, name, "characterization")
        if towers.get("tower_2", {}).get("present"):
            for name in TOWER2_CHARACTERIZATION:
                characterization[name] = await timed_read(probe, name, "characterization")
        if args.with_energy:
            for name in ENERGY_NAMES:
                characterization[name] = await timed_read(probe, name, "characterization")
        identity = {name: await timed_read(probe, name, "identity") for name in IDENTITY_NAMES}
        if args.invasive_periodic_test:
            for name in (SOC_TARGET, *(() if args.skip_household else (HOUSEHOLD,))):
                row = await periodic_registration_probe(probe, name, f"periodic_{name}")
                periodic_rows.append(row)
                if name == SOC_TARGET:
                    soc_reads.append(row)
                    await timed_read(probe, SOC_TARGET, "soc_after_periodic")
        periodic = periodic_set_status(probe, args.exposed_names)
        periodic["registration_probes"] = periodic_rows
        periodic["probe_limit"] = (
            "Registration answer only; later periodic pushes need a pas.period WRITE, which this tool never sends."
            if args.invasive_periodic_test
            else "No periodic registration was made (default mode)."
        )
        for row in identity.values():  # device identity: text values only, no serial number
            row.pop("raw_value", None)
        result = {
            "device": {
                "device_id": entry.device_id,
                "host": entry.host,
                "port": entry.port,
                "model": identity["android_description"]["value"],
                "firmware_svnversion": identity["svnversion"]["value"],
                "started_utc": datetime.now(UTC).isoformat(),
                "mode": _mode_label(args),
            },
            "topology": towers,
            "capacity_candidates": capacity,
            "characterization_only": characterization,
            "soc_target_reads": soc_reads,
            "household_load_power_reads": household,
            "periodic_status": periodic,
            "connection_events": probe.events,
            "classification_hint": classify(soc_reads, periodic_rows, args.invasive_periodic_test),
            "all_reads": probe.reads,
        }
        return result
    finally:
        logger.removeHandler(handler)
        await serializer.stop()
        await endpoint.close()


def _mode_label(args: argparse.Namespace) -> str:
    if not args.invasive_periodic_test:
        return "read-only"
    return "INVASIVE periodic-first" if args.invasive_periodic_first else "INVASIVE read-first"


def render_text(result: dict) -> str:
    out = []
    dev = result["device"]
    out.append(
        f"Device {dev['device_id']} ({dev['host']}:{dev['port']}) model={dev['model']!r} fw={dev['firmware_svnversion']!r}"
    )
    out.append(f"Mode: {dev['mode']}")
    if result["classification_hint"]["invasive"]:
        out.append(INVASIVE_BANNER)
    out.append("\nTopology (derived from module serial slots, not from numeric battery values):")
    for key, tower in result["topology"].items():
        out.append(
            f"  {key}: present={tower['present']} modules={tower['module_count']} "
            f"({tower['module_count_status']}) populated_slots={tower['populated_module_slots']}"
        )
    out.append("\nCapacity candidates / side-by-side (no interpretation, nothing is released as capacity):")
    for name, row in {**result["capacity_candidates"], **result["characterization_only"]}.items():
        out.append(
            f"  {name:<36} {row['object_id']} {row['status']:<14} value={row['value']} {row['unit']} ({row['duration_s']}s)"
        )
    out.append("\nbattery_soc_target reads:")
    for row in result["soc_target_reads"]:
        out.append(
            f"  T+{row['t_since_connect_s']:7.1f}s {row['phase']:<24} {row['status']:<14} {row['duration_s']:5.2f}s "
            f"value={row['value']} epoch {row['epoch_before']}->{row['epoch_after']} reconnect={row['reconnect_triggered']}"
        )
    out.append("\nhousehold_load_power reads:")
    for row in result["household_load_power_reads"]:
        out.append(
            f"  T+{row['t_since_connect_s']:7.1f}s {row['status']:<14} {row['duration_s']:5.2f}s reconnect={row['reconnect_triggered']}"
        )
    per = result["periodic_status"]
    out.append(
        f"\nPeriodic set ({per['basis']}): soc_target={per['battery_soc_target_in_set']} "
        f"household_load_power={per['household_load_power_in_set']} size={per['set_size']}"
    )
    for row in per["registration_probes"]:
        out.append(
            f"  registration {row['metric']}: {row['status']} {row['duration_s']}s reconnect={row['reconnect_triggered']}"
        )
    out.append("\nConnection events:")
    for ev in result["connection_events"]:
        out.append(f"  {ev['t_s']:8.1f}s epoch={ev['epoch']} {ev['kind']} {ev.get('object_id') or ''}")
    hint = result["classification_hint"]
    out.append(f"\nHint: {hint['hint']} - {hint['reason']}\n{hint['legend']}")
    return "\n".join(out)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--device", required=True, help="id=host:port[@network_id] (same syntax as DEVICES)")
    p.add_argument(
        "--confirm-app-stopped",
        action="store_true",
        help="required: confirms no rct-manager instance holds the connection to this device",
    )
    p.add_argument(
        "--offsets",
        type=lambda s: [float(x) for x in s.split(",")],
        default=[5, 30, 60, 300],
        help="seconds after connect for the battery_soc_target READs (default 5,30,60,300)",
    )
    p.add_argument("--response-timeout", type=float, default=5.0)
    p.add_argument(
        "--invasive-periodic-test",
        action="store_true",
        help="INVASIVE: after the timeline, register soc_target/household_load_power via "
        f"READ_PERIODICALLY (needs {CONFIRM_INVASIVE})",
    )
    p.add_argument(
        "--invasive-periodic-first",
        action="store_true",
        help="INVASIVE: first soc_target access is a READ_PERIODICALLY registration instead of a READ "
        "(requires --invasive-periodic-test)",
    )
    p.add_argument(
        CONFIRM_INVASIVE,
        dest="confirm_invasive",
        action="store_true",
        help="acknowledge that the invasive test changes the device's subscription state",
    )
    p.add_argument("--with-energy", action="store_true", help="also read battery_stored_energy/used_energy")
    p.add_argument("--skip-topology", action="store_true")
    p.add_argument("--skip-household", action="store_true")
    p.add_argument(
        "--exposed-names",
        type=lambda s: [x for x in s.split(",") if x],
        default=None,
        help="override the exposed metric names used to compute the periodic set",
    )
    p.add_argument("--json-out", type=Path)
    p.add_argument("--csv-out", type=Path)
    args = p.parse_args(argv)
    if not args.confirm_app_stopped:
        p.error("--confirm-app-stopped is required (only one connection per device is allowed)")
    if args.invasive_periodic_first and not args.invasive_periodic_test:
        p.error("--invasive-periodic-first requires --invasive-periodic-test")
    if args.invasive_periodic_test and not args.confirm_invasive:
        p.error(f"--invasive-periodic-test registers READ_PERIODICALLY on the device; add {CONFIRM_INVASIVE}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    result = asyncio.run(run(args))
    print(render_text(result))
    if args.json_out:
        args.json_out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    if args.csv_out:
        with args.csv_out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=[*CSV_FIELDS, "t_since_epoch_s", "command"], extrasaction="ignore")
            writer.writeheader()
            writer.writerows(result["all_reads"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
