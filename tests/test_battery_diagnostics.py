#
# tests/test_battery_diagnostics.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""The battery calibration evidence recorder: raster, sample identity, transitions, gaps and files."""

import asyncio
import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from app.admin import battery_diagnostics as diag
from app.admin.battery_diagnostics import BatteryDiagnosticsRecorder
from app.errors import DeviceTimeout
from app.gateway.base import DeviceState
from app.gateway.cache import CacheEntry, CacheFreshness
from tests.conftest import AutoClock, ManualClock


class _Gateway:
    """Cache entries and fresh reads as the RCT gateway offers them; records every fresh read."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.cache: dict[str, tuple] = {}
        for name, value in (
            ("battery_soc", 0.42),
            ("battery_power", -1834.0),
            ("battery_status2", 0),
        ):
            self.put(name, value)
        self.fresh: dict[str, object] = {
            "battery_stored_energy": 812345.0,
            "battery_used_energy": 790001.0,
            "battery_ah_capacity": 25.0,
            "power_mng_amp_hours_measured": 23.85,
            "power_mng_amp_hours": 11.2,
            "battery_soh": 0.97,
            "battery_cycles": 128,
            "battery_status2": 0,
            "power_mng_bat_next_calib_date": 1793750400,
            "battery_bms_sn": "BMS-1",
        }
        self.errors: dict[str, Exception] = {}
        self.reads: list[str] = []
        self.read_seconds = 0.0
        self.state = DeviceState.OK
        self.queue_length = 0

    def put(self, name, value, freshness=CacheFreshness.FRESH, age=3.0) -> None:
        entry = CacheEntry(
            value,
            measured_at=self.clock.now(),
            received_monotonic=self.clock.monotonic() - age,
            origin="periodic",
        )
        self.cache[name] = (entry, freshness)

    def cached_entry(self, device_id, name):
        return self.cache.get(name)

    async def read_system(self, device_id, name):
        self.reads.append(name)
        if self.read_seconds:
            self.clock.advance(self.read_seconds)
        if name in self.errors:
            raise self.errors[name]
        return SimpleNamespace(value=self.fresh[name], measured_at=self.clock.now())

    def device_status(self, device_id):
        return SimpleNamespace(state=self.state, queue_length=self.queue_length)

    def reported_serial(self, device_id):
        return "INV-1"


class _Capacity:
    def module_counts(self, device_id):
        return (6,)

    def verified_usable_capacity_wh(self, device_id):
        return 10368.0

    def is_settling(self, device_id):
        return False


def _clock(at: datetime = datetime(2026, 10, 19, 12, 0, tzinfo=UTC), kind=ManualClock):
    clock = kind()
    clock._wall = at
    return clock


def _recorder(tmp_path: Path, gateway: _Gateway, exists=lambda _n: True):
    return BatteryDiagnosticsRecorder(
        gateway,
        gateway.clock,
        tmp_path / "battery-diagnostics",
        lambda: ["main"],
        exists,
        _Capacity(),
    )


def _records(tmp_path: Path, day: str = "2026-10-19") -> list[dict]:
    path = tmp_path / "battery-diagnostics" / f"battery-diagnostics-{day}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_live_values_come_from_the_cache_while_counters_and_slow_values_are_read_fresh(
    tmp_path,
):
    gateway = _Gateway(_clock())
    recorder = _recorder(tmp_path, gateway)

    async def scenario():
        await recorder.tick("main")  # first tick: everything is due
        first = list(gateway.reads)
        for _ in range(3):  # 45 s: fast records only
            gateway.clock.advance(15)
            await recorder.tick("main")
        quiet = len(gateway.reads)
        gateway.clock.advance(
            15
        )  # 60 s: the counters are due again, the slow values are not
        await recorder.tick("main")
        return first, quiet, gateway.reads[quiet:]

    first, quiet, after_minute = asyncio.run(scenario())
    assert first == [*diag.COUNTER_NAMES, *diag.SLOW_NAMES]
    assert quiet == len(first), "no fresh read between the counter minutes"
    assert after_minute == list(diag.COUNTER_NAMES)
    records = _records(tmp_path)
    assert [r["type"] for r in records][:3] == [
        "periodic_fast",
        "periodic_counters",
        "periodic_slow",
    ]
    fast = records[0]
    assert (fast["device_id"], fast["inverter_serial"]) == ("main", "INV-1")
    assert (
        fast["timestamp"] == "2026-10-19T12:00:00+00:00" and fast["monotonic"] == 1000.0
    )
    assert fast["module_counts"] == [6]
    assert (fast["rated_usable_capacity_wh"], fast["topology_settling"]) == (
        10368.0,
        False,
    )
    assert records[2]["battery_bms_sn"] == "BMS-1"
    assert records[2]["values"]["power_mng_amp_hours"]["value"] == 11.2


def test_a_repeated_cache_value_keeps_one_sample_identity(tmp_path):
    # Integrating battery_power must count a measurement once, however many records repeat it.
    gateway = _Gateway(_clock())
    recorder = _recorder(tmp_path, gateway)

    async def scenario():
        await recorder.tick("main")
        gateway.clock.advance(15)
        await recorder.tick("main")  # the cache still holds the same measurement
        gateway.put("battery_power", -900.0, age=0.5)
        gateway.clock.advance(15)
        await recorder.tick("main")

    asyncio.run(scenario())
    power = [
        r["values"]["battery_power"]
        for r in _records(tmp_path)
        if r["type"] == "periodic_fast"
    ]
    assert power[0]["sample_monotonic"] == power[1]["sample_monotonic"] == 997.0
    assert power[0]["sample_measured_at"] == power[1]["sample_measured_at"]
    assert power[1]["age_seconds"] == 18.0
    assert power[2]["value"] == -900.0 and power[2]["sample_monotonic"] == 1014.5


def test_the_raster_follows_a_monotonic_deadline_and_skips_slots_of_a_slow_cycle(
    tmp_path,
):
    clock = _clock(kind=AutoClock)
    gateway = _Gateway(clock)
    gateway.read_seconds = 1.0  # the first tick reads ten registers: 10 s
    recorder = _recorder(tmp_path, gateway)
    starts: list[float] = []
    original = recorder.tick

    async def tick(device_id):
        starts.append(clock.monotonic())
        if len(starts) == 5:
            raise asyncio.CancelledError
        await original(device_id)
        if len(starts) == 3:
            clock.advance(20.0)  # this cycle overruns the next 15 s slot

    recorder.tick = tick
    try:
        asyncio.run(recorder.run())
    except asyncio.CancelledError:
        pass
    origin = starts[0]
    assert [round(s - origin, 3) for s in starts] == [0.0, 15.0, 30.0, 60.0, 75.0]


def test_a_status_change_in_the_cache_writes_a_full_snapshot_in_addition_to_the_raster(
    tmp_path,
):
    gateway = _Gateway(_clock())
    recorder = _recorder(tmp_path, gateway)

    async def scenario():
        await recorder.tick("main")
        gateway.clock.advance(15)
        gateway.put("battery_status2", 1032, age=1.0)
        await recorder.tick("main")
        gateway.clock.advance(15)
        await recorder.tick("main")  # unchanged since: no second transition

    asyncio.run(scenario())
    transitions = [r for r in _records(tmp_path) if r["type"] == "status_transition"]
    assert len(transitions) == 1
    snapshot = transitions[0]
    assert (snapshot["battery_status2_before"], snapshot["battery_status2_after"]) == (
        0,
        1032,
    )
    assert set(snapshot["values"]) == {
        *diag.CACHED_NAMES,
        *diag.COUNTER_NAMES,
        *diag.SLOW_NAMES,
    }
    assert snapshot["values"]["battery_stored_energy"]["status"] == "fresh"


def test_missing_stale_failed_and_busy_values_are_recorded_as_such_never_filled_in(
    tmp_path,
):
    gateway = _Gateway(_clock())
    del gateway.cache["battery_soc"]
    gateway.put("battery_power", float("nan"), CacheFreshness.GRACE, age=40.0)
    gateway.errors["battery_used_energy"] = DeviceTimeout("response_timeout")
    gateway.errors["battery_soh"] = RuntimeError("decoder broke")
    recorder = _recorder(
        tmp_path, gateway, exists=lambda name: name != "power_mng_amp_hours"
    )
    asyncio.run(recorder.tick("main"))
    gateway.queue_length = 2
    gateway.clock.advance(60)
    asyncio.run(recorder.tick("main"))
    records = _records(tmp_path)
    fast, counters, slow = records[:3]
    assert fast["values"]["battery_soc"] == {"value": None, "status": "missing"}
    assert fast["values"]["battery_power"]["value"] is None
    assert fast["values"]["battery_power"]["status"] == "stale"
    assert counters["values"]["battery_used_energy"] == {
        "value": None,
        "status": "error",
        "error": "response_timeout",
    }
    assert counters["values"]["battery_stored_energy"]["value"] == 812345.0
    # An unexpected failure is isolated to its register; the rest of the tick is still written.
    assert slow["values"]["battery_soh"] == {
        "value": None,
        "status": "error",
        "error": "RuntimeError",
    }
    assert slow["values"]["battery_cycles"]["value"] == 128
    assert slow["values"]["power_mng_amp_hours"] == {
        "value": None,
        "status": "not_in_catalog",
    }
    busy = next(r for r in records[3:] if r["type"] == "periodic_counters")
    assert {v["status"] for v in busy["values"].values()} == {"skipped_busy"}


def test_a_failed_bms_read_falls_back_to_the_cached_serial(tmp_path):
    gateway = _Gateway(_clock())
    gateway.put("battery_bms_sn", "BMS-CACHED")
    gateway.errors["battery_bms_sn"] = DeviceTimeout("response_timeout")
    asyncio.run(_recorder(tmp_path, gateway).tick("main"))
    slow = next(r for r in _records(tmp_path) if r["type"] == "periodic_slow")
    assert slow["values"]["battery_bms_sn"]["status"] == "error"
    assert slow["battery_bms_sn"] == "BMS-CACHED"


def test_files_rotate_by_record_day_compress_finished_days_and_keep_sixty_days(
    tmp_path,
):
    directory = tmp_path / "battery-diagnostics"
    directory.mkdir()
    old = (
        directory / "battery-diagnostics-2026-08-19.jsonl.gz"
    )  # 61 days before 2026-10-19
    kept = (
        directory / "battery-diagnostics-2026-08-20.jsonl.gz"
    )  # exactly 60 days: kept
    foreign = directory / "notes.txt"
    for path in (old, kept, foreign):
        path.write_bytes(b"x")
    gateway = _Gateway(_clock(datetime(2026, 10, 18, 23, 59, 50, tzinfo=UTC)))
    gateway.read_seconds = (
        2.0  # the first tick starts before and ends after UTC midnight
    )
    recorder = _recorder(tmp_path, gateway)

    async def scenario():
        await recorder.tick("main")
        gateway.read_seconds = 0.0
        gateway.clock.advance(15)
        await recorder.tick("main")  # a new day: housekeeping packs yesterday
        await recorder.tick("main")

    asyncio.run(scenario())
    assert not old.exists() and kept.exists() and foreign.exists()
    with gzip.open(
        directory / "battery-diagnostics-2026-10-18.jsonl.gz", "rt", encoding="utf-8"
    ) as handle:
        yesterday = [json.loads(line) for line in handle]
    assert {r["timestamp"][:10] for r in yesterday} == {"2026-10-18"}
    assert yesterday[0]["timestamp"] == "2026-10-18T23:59:50+00:00"
    today = _records(tmp_path, "2026-10-19")
    assert today and {r["timestamp"][:10] for r in today} == {"2026-10-19"}


def test_a_write_failure_is_logged_once_and_never_stops_the_recorder(tmp_path, caplog):
    (tmp_path / "battery-diagnostics").write_text(
        "a file where the directory should be", encoding="utf-8"
    )
    gateway = _Gateway(_clock())
    recorder = _recorder(tmp_path, gateway)
    asyncio.run(recorder.tick("main"))
    gateway.clock.advance(15)
    asyncio.run(recorder.tick("main"))
    assert sum("could not be written" in r.getMessage() for r in caplog.records) == 1


def test_the_recorder_reaches_no_planning_capacity_or_calibration_path():
    # Evidence only: a dependency on the planner, the capacity resolver or the calibration store
    # would let a diagnostic read change what the service plans or reports.
    source = Path(diag.__file__).read_text(encoding="utf-8")
    imports = [
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    ]
    assert not [
        line for line in imports if "app.optimizer" in line or "app.dispatch" in line
    ], imports
    assert (
        ".record_battery_calibration(" not in source
        and ".calibration_sample(" not in source
    )
