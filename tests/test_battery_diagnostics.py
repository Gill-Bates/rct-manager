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
            "battery_charged_amp_hours": 19016.32,
            "battery_discharged_amp_hours": 18233.95,
            "battery_efficiency": 0.93,
            "battery_ah_capacity": 25.0,
            "power_mng_amp_hours_measured": 23.85,
            "power_mng_amp_hours": 11.2,
            "battery_soh": 0.97,
            "battery_cycles": 128,
            "battery_status2": 0,
            "battery_status": 4,
            "battery_bat_status": 8,
            "power_mng_bat_next_calib_date": 1793750400,
            "power_mng_calib_charge_power": 3000.0,
            "power_mng_bat_calib_reqularity": 30,
            "power_mng_bat_calib_days_in_advance": 3,
            "bat_mng_struct_bat_calib_soc_thresh": 0.5,
            "bat_mng_struct_bat_calib_soc_threshold": 0.97,
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
    assert first == [*diag.COUNTER_NAMES, *diag.SLOW_NAMES, *diag.CONFIG_NAMES]
    assert quiet == len(first), "no fresh read between the counter minutes"
    assert after_minute == list(diag.COUNTER_NAMES)
    records = _records(tmp_path)
    assert [r["type"] for r in records][:4] == [
        "periodic_fast",
        "periodic_counters",
        "periodic_slow",
        "periodic_config",
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
    gateway.read_seconds = 0.5  # the first tick can overrun a raster slot
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
        *diag.CONFIG_NAMES,
    }
    assert {
        name: snapshot["values"][name]["status"]
        for name in (*diag.COUNTER_NAMES, *diag.SLOW_NAMES, *diag.CONFIG_NAMES)
    } == dict.fromkeys(
        (*diag.COUNTER_NAMES, *diag.SLOW_NAMES, *diag.CONFIG_NAMES), "fresh"
    )
    assert snapshot["values"]["battery_discharged_amp_hours"]["value"] == 18233.95
    assert snapshot["values"]["battery_status"]["value"] == 4
    assert snapshot["values"]["battery_bat_status"]["value"] == 8
    assert snapshot["values"]["bat_mng_struct_bat_calib_soc_threshold"]["measured_at"]


def test_missing_stale_failed_and_busy_values_are_recorded_as_such_never_filled_in(
    tmp_path,
):
    gateway = _Gateway(_clock())
    del gateway.cache["battery_soc"]
    gateway.put("battery_power", float("nan"), CacheFreshness.GRACE, age=40.0)
    gateway.errors["battery_used_energy"] = DeviceTimeout("response_timeout")
    gateway.errors["battery_soh"] = RuntimeError("decoder broke")
    gateway.errors["power_mng_calib_charge_power"] = RuntimeError("decoder broke")
    absent = {
        "power_mng_amp_hours",
        "battery_efficiency",
        "bat_mng_struct_bat_calib_soc_threshold",
    }
    recorder = _recorder(tmp_path, gateway, exists=lambda name: name not in absent)
    asyncio.run(recorder.tick("main"))
    gateway.queue_length = 2
    gateway.clock.advance(60)
    asyncio.run(recorder.tick("main"))
    records = _records(tmp_path)
    fast, counters, slow, config = records[:4]
    assert fast["values"]["battery_soc"] == {"value": None, "status": "missing"}
    assert fast["values"]["battery_power"]["value"] is None
    assert fast["values"]["battery_power"]["status"] == "stale"
    assert counters["values"]["battery_used_energy"] == {
        "value": None,
        "status": "error",
        "error": "response_timeout",
    }
    assert counters["values"]["battery_stored_energy"]["value"] == 812345.0
    assert slow["values"]["battery_efficiency"] == {
        "value": None,
        "status": "not_in_catalog",
    }
    assert counters["values"]["battery_charged_amp_hours"]["value"] == 19016.32
    # An unexpected failure is isolated to its register; the rest of the tick is still written.
    assert slow["values"]["battery_soh"] == {
        "value": None,
        "status": "error",
        "error": "RuntimeError",
    }
    assert slow["values"]["battery_cycles"]["value"] == 128
    assert slow["values"]["power_mng_calib_charge_power"]["status"] == "error"
    assert config["values"]["power_mng_bat_calib_reqularity"]["value"] == 30
    assert config["values"]["bat_mng_struct_bat_calib_soc_threshold"] == {
        "value": None,
        "status": "not_in_catalog",
    }
    assert slow["values"]["power_mng_amp_hours"] == {
        "value": None,
        "status": "not_in_catalog",
    }
    busy = next(r for r in records[4:] if r["type"] == "periodic_counters")
    assert {
        name: v["status"] for name, v in busy["values"].items() if name not in absent
    } == dict.fromkeys(set(diag.COUNTER_NAMES) - absent, "skipped_busy")
    gateway.clock.advance(diag.CONFIG_INTERVAL_SECONDS)
    asyncio.run(recorder.tick("main"))
    busy_config = next(
        r
        for r in reversed(_records(tmp_path, "2026-10-20"))
        if r["type"] == "periodic_config"
    )
    assert (
        busy_config["values"]["bat_mng_struct_bat_calib_soc_threshold"]["status"]
        == "not_in_catalog"
    )
    assert {
        name: value["status"]
        for name, value in busy_config["values"].items()
        if name not in absent
    } == dict.fromkeys(set(diag.CONFIG_NAMES) - absent, "skipped_busy")


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
        and "source_contract" not in source
    )
    assert not [line for line in imports if "battery_inventory" in line], imports


def test_calibration_registers_follow_their_cadences_without_topology_io(
    tmp_path,
):
    gateway = _Gateway(_clock())
    capacity = _Capacity()
    recorder = BatteryDiagnosticsRecorder(
        gateway,
        gateway.clock,
        tmp_path / "battery-diagnostics",
        lambda: ["main"],
        lambda _n: True,
        capacity,
    )
    reads_per_tick: list[list[str]] = []

    async def scenario():
        for _ in range(21):  # 0 s .. 300 s on the 15 s raster
            before = len(gateway.reads)
            await recorder.tick("main")
            reads_per_tick.append(gateway.reads[before:])
            gateway.clock.advance(15)

    asyncio.run(scenario())
    config = {
        "power_mng_bat_calib_reqularity",
        "power_mng_bat_calib_days_in_advance",
        "bat_mng_struct_bat_calib_soc_thresh",
        "bat_mng_struct_bat_calib_soc_threshold",
    }
    assert config == set(diag.CONFIG_NAMES)
    assert {
        "power_mng_calib_charge_power",
        "battery_status",
        "battery_bat_status",
    } <= set(diag.SLOW_NAMES)
    assert {
        "battery_charged_amp_hours",
        "battery_discharged_amp_hours",
    } <= set(diag.COUNTER_NAMES)
    assert "battery_efficiency" in diag.SLOW_NAMES
    slow_ticks = [
        i
        for i, reads in enumerate(reads_per_tick)
        if "power_mng_calib_charge_power" in reads
    ]
    config_ticks = [i for i, reads in enumerate(reads_per_tick) if config & set(reads)]
    counter_ticks = [i for i, reads in enumerate(reads_per_tick) if reads]
    assert slow_ticks == [0, 20]
    assert config_ticks == [0]
    assert counter_ticks == [0, 4, 8, 12, 16, 20]
    assert reads_per_tick[20] == [*diag.COUNTER_NAMES, *diag.SLOW_NAMES]
    # Topology comes from the settled inventory only: every record has it, no read serves it.
    fast = [r for r in _records(tmp_path) if r["type"] == "periodic_fast"]
    assert len(fast) == 21 and {tuple(r["module_counts"]) for r in fast} == {(6,)}
    assert all(
        name in (*diag.COUNTER_NAMES, *diag.SLOW_NAMES, *diag.CONFIG_NAMES)
        for name in gateway.reads
    )


def test_config_is_read_on_first_tick_and_again_after_24_hours(tmp_path):
    gateway = _Gateway(_clock())
    recorder = _recorder(tmp_path, gateway)

    async def scenario():
        reads = []
        for advance in (0, 300, diag.CONFIG_INTERVAL_SECONDS - 301, 1):
            gateway.clock.advance(advance)
            before = len(gateway.reads)
            await recorder.tick("main")
            reads.append(gateway.reads[before:])
        return reads

    reads = asyncio.run(scenario())
    assert all(name in reads[0] for name in diag.CONFIG_NAMES)
    assert all(name not in tick for tick in reads[1:3] for name in diag.CONFIG_NAMES)
    assert all(name in reads[3] for name in diag.CONFIG_NAMES)


def test_reset_device_starts_a_new_evidence_chain_for_the_same_id(tmp_path):
    gateway = _Gateway(_clock())
    recorder = _recorder(tmp_path, gateway)

    async def scenario():
        await recorder.tick("main")
        gateway.clock.advance(300)
        gateway.fresh["power_mng_bat_next_calib_date"] = 1796342400
        await recorder.tick("main")
        gateway.clock.advance(15)
        gateway.put("battery_status2", 0x400)
        gateway.fresh["power_mng_bat_next_calib_date"] = 1798934400
        recorder.reset_device("main")
        before = len(gateway.reads)
        await recorder.tick("main")
        return gateway.reads[before:]

    new_reads = asyncio.run(scenario())
    assert new_reads == [*diag.COUNTER_NAMES, *diag.SLOW_NAMES, *diag.CONFIG_NAMES]
    records = _records(tmp_path)
    assert len([r for r in records if r["type"] == "periodic_config"]) == 2
    assert not [r for r in records if r["type"] == "status_transition"]
    assert len([r for r in records if r["type"] == "candidate_change"]) == 1


def test_reset_discards_an_inflight_tick_from_the_old_device(tmp_path):
    gateway = _Gateway(_clock())
    recorder = _recorder(tmp_path, gateway)

    async def scenario():
        started = asyncio.Event()
        resume = asyncio.Event()
        read_system = gateway.read_system

        async def blocked_read(device_id, name):
            started.set()
            await resume.wait()
            return await read_system(device_id, name)

        gateway.read_system = blocked_read
        old_tick = asyncio.create_task(recorder.tick("main"))
        await asyncio.wait_for(started.wait(), 1)
        recorder.reset_device("main")
        resume.set()
        await asyncio.wait_for(old_tick, 1)
        gateway.read_system = read_system
        await recorder.tick("main")

    asyncio.run(scenario())
    records = _records(tmp_path)
    assert [r["type"] for r in records] == [
        "periodic_fast",
        "periodic_counters",
        "periodic_slow",
        "periodic_config",
    ]


def test_only_discrete_candidate_changes_are_flagged_as_unverified_evidence_only(
    tmp_path,
):
    gateway = _Gateway(_clock())
    recorder = _recorder(tmp_path, gateway)

    async def scenario():
        await recorder.tick("main")  # first fresh read: a baseline, no change
        gateway.fresh["power_mng_amp_hours_measured"] = 24.1
        gateway.fresh["power_mng_bat_next_calib_date"] = 1796342400
        gateway.errors["battery_soh"] = DeviceTimeout("response_timeout")
        gateway.clock.advance(300)
        await recorder.tick("main")
        del gateway.errors["battery_soh"]  # the same value again after a gap: no change
        gateway.clock.advance(300)
        await recorder.tick("main")

    asyncio.run(scenario())
    changes = [r for r in _records(tmp_path) if r["type"] == "candidate_change"]
    assert [r["register"] for r in changes] == ["power_mng_bat_next_calib_date"]
    for change in changes:
        assert (change["evidence_only"], change["verified"]) == (True, False)
        assert change["trigger"] == "periodic_slow" and change["values"] == {}
        assert change["before"]["status"] == change["after"]["status"] == "fresh"
    assert (changes[0]["before"]["value"], changes[0]["after"]["value"]) == (
        1793750400,
        1796342400,
    )
    slow = [r for r in _records(tmp_path) if r["type"] == "periodic_slow"]
    assert [r["values"]["power_mng_amp_hours_measured"]["value"] for r in slow] == [
        23.85,
        24.1,
        24.1,
    ]


def test_a_transition_into_a_calibrating_status_adds_an_unverified_hint(tmp_path):
    gateway = _Gateway(_clock())
    recorder = _recorder(tmp_path, gateway)

    async def scenario():
        await recorder.tick("main")
        gateway.clock.advance(15)
        gateway.put("battery_status2", 0x400, age=1.0)
        await recorder.tick("main")
        gateway.clock.advance(15)
        gateway.put("battery_status2", 0x100, age=1.0)  # balancing only: still a hint
        await recorder.tick("main")
        gateway.clock.advance(15)
        gateway.put(
            "battery_status2", 0x800, age=1.0
        )  # no calibrating bit on either side
        await recorder.tick("main")

    asyncio.run(scenario())
    records = _records(tmp_path)
    hints = [r for r in records if r["type"] == "calibration_window_hint"]
    assert [
        (h["battery_status2_before"], h["battery_status2_after"]) for h in hints
    ] == [
        (0, 0x400),
        (0x400, 0x100),
    ]
    assert all((h["evidence_only"], h["verified"]) == (True, False) for h in hints)
    assert len([r for r in records if r["type"] == "status_transition"]) == 3


def test_the_documented_installer_app_efficiency_matches_the_lifetime_wh_counters():
    # Evidence for the counter mapping only; no health or capacity value is derived from it.
    assert round(9162862 / 9814451, 4) == 0.9336
    doc = (
        Path(__file__).parents[1] / "docs" / "reference" / "hardware-verification.md"
    ).read_text(encoding="utf-8")
    assert "9,162,862 / 9,814,451 = 0.9336" in doc
