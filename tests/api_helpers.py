#!/usr/bin/env python3
#
# tests/api_helpers.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Builds the application in-process against the fake device network."""

import asyncio
import contextlib
import json
import struct
import tempfile
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from app.admin.store import AdminStore
from app.api.app_factory import create_app
from app.catalog.registry import RegistryCatalog
from app.clock import SystemClock
from app.config import DeviceEntry, Settings
from app.dispatch.capabilities import CapabilityName, CapabilityRecord, CapabilityStatus
from app.dispatch.store import DispatchStore
from app.gateway.base import DeviceState
from app.security.pat import PAT_PREFIX, _pat_checksum
from app.security.tokens import TokenStore
from tests.fakes import FakeNetwork

HOST = "192.0.2.77"
PORT = 48899
SLAVE_NETWORK_ID = 7654321
def _fixed_pat(label: str) -> str:
    body = PAT_PREFIX + label.ljust(40, "0")
    return body + _pat_checksum(body)


READ_TOKEN = _fixed_pat("ReadToken")
WRITE_TOKEN = _fixed_pat("WriteToken")


REGISTRY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "objects.json"


def make_settings(**overrides) -> Settings:
    """Settings without any file or environment input; tuned so tests do not wait."""
    base = {
        "object_registry_path": REGISTRY_FIXTURE,
        "write_allowlist_path": REGISTRY_FIXTURE.with_name("objects_write_allowed.json"),
        "devices": [
            DeviceEntry(device_id="main", host=HOST, port=PORT),
            DeviceEntry(device_id="slave1", host=HOST, port=PORT, network_id=SLAVE_NETWORK_ID),
        ],
        "min_request_interval_ms": 0,
        "heartbeat_interval_seconds": 3600,
        "read_retries": 0,
        "rate_limit_requests": 10_000,
        "device_budget_transactions": 10_000,
        "response_timeout_seconds": 0.5,
        "shutdown_grace_seconds": 5.0,
        "shutdown_periodic_reserve_seconds": 1.0,
    }
    return Settings(_env_file=None, **{**base, **overrides})


def default_payloads(settings: Settings) -> dict[int, bytes]:
    catalog = RegistryCatalog.from_file(settings.object_registry_path)
    return {catalog.object_entry(settings.heartbeat_metric_name).object_id: b"\x03"}


def float_payload(value: float) -> bytes:
    return struct.pack(">f", value)


@dataclass(slots=True)
class Harness:
    app: FastAPI
    client: httpx.AsyncClient
    net: FakeNetwork

    @property
    def runtime(self):
        return self.app.state.runtime


async def wait_settled(app: FastAPI, timeout: float = 3.0) -> None:
    """Wait until every device left the ``starting`` state (first heartbeat done)."""
    runtime = app.state.runtime
    async with asyncio.timeout(timeout):
        while any(runtime.gateway.device_status(d).state is DeviceState.STARTING for d in runtime.devices):
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def running_app(
    settings: Settings | None = None,
    *,
    settle: bool = True,
    behavior=None,
    fail_connects: int = 0,
    authorize: bool = True,
) -> AsyncIterator[Harness]:
    settings = settings or make_settings()
    clock = SystemClock()
    net = FakeNetwork(clock, fail_connects=fail_connects)
    net.payloads = default_payloads(settings)
    if behavior is not None:
        net.behavior = behavior
    with tempfile.TemporaryDirectory(prefix="rct-api-test-") as directory:
        app = create_app(settings, clock=clock, connector=net.connect)
        store = AdminStore(Path(directory) / "admin.db", "s" * 48)
        password = store.initialize()
        assert store.change_password(password, "replacement-test-password")
        with patch("app.admin.store.generate_pat", side_effect=[READ_TOKEN, WRITE_TOKEN]):
            store.create_token("read test token", "read", None)
            store.create_token("write test token", "read/write", None)
        app.state.security.tokens = TokenStore(auth_required=settings.auth_required, admin_store=store)
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
                default = {"Authorization": f"Bearer {READ_TOKEN}"} if authorize else {}  # explicit headers override it
                async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=default) as client:
                    if settle:
                        await wait_settled(app)
                    yield Harness(app, client, net)
        finally:
            store.close()  # no background write may outlive the temporary directory


TARGET_NAME = "battery_target"
TARGET_OBJECT_ID = 0x1234ABCD
ACTION_NAME = "com_service"


def write_fixtures(
    directory: Path, *, minimum: float = 0.0, maximum: float = 1.0, step: float | None = 0.01, approve: bool = True
) -> dict[str, Path]:
    """Registry with one writable float next to the real entries plus a matching write allowlist."""
    registry = json.loads(Path(make_settings().object_registry_path).read_text(encoding="utf-8"))
    registry["entries"].append(
        {
            "name": TARGET_NAME,
            "object_id": f"0x{TARGET_OBJECT_ID:08X}",
            "data_type": "t_float",
            "unit": "ratio",
            "value_type": "number",
            "writable": True,
            "idempotent_write": True,
            "is_action": False,
            "preselected": False,
        }
    )
    entries = [{"name": ACTION_NAME, "data_type": "t_enum", "allowed_values": [1]}]
    if approve:
        entry = {"name": TARGET_NAME, "data_type": "t_float", "minimum": minimum, "maximum": maximum}
        entries.append({**entry, "step": step} if step is not None else entry)
    registry_path, allowlist_path = directory / "objects_read.json", directory / "objects_write_allowed.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    allowlist_path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
    return {"object_registry_path": registry_path, "write_allowlist_path": allowlist_path}


def dispatch_capabilities(
    directory: Path,
    device_ids: Iterable[str],
    *,
    secret: str,
    verified: bool = True,
    soc_strategy_external_code: int = 1,
) -> Path:
    """Pre-fill a dispatch database with one capability row per device and return its path.

    An integration test that drives hardware has to state the verification explicitly: the shipping
    state is ``unverified``, and that blocks dispatch. ``verified=False`` yields the shipping state,
    so a test can show that the gate refuses. The rows are written per device on purpose — a device
    that is not named here stays blocked.
    """
    path = directory / "dispatch.db"
    store = DispatchStore(path, secret)
    store.initialize()
    status = CapabilityStatus.VERIFIED if verified else CapabilityStatus.UNVERIFIED
    for device_id in device_ids:
        for name in CapabilityName:
            store.put_capability(
                CapabilityRecord(
                    device_id=device_id,
                    name=name,
                    status=status,
                    soc_strategy_external_code=(
                        soc_strategy_external_code if name is CapabilityName.WRITE_PATH else None
                    ),
                    enum_byte_width=1,
                    bool_byte_width=1,
                    write_frame_layout_verified=True,
                    apply_sequence_verified=True,
                    sequence_order_relevant=True,
                    export_limit_zero_blocks_export=True,
                    verified_device_model="RCT-Power-Storage-DC",
                    verified_firmware="1.0.0",
                    verified_by="integration test",
                )
            )
    return path


DISPATCH_WRITE_NAMES = (
    "power_mng_soc_strategy",
    "power_mng_soc_target_set",
    "power_mng_battery_power_extern",
    "power_mng_use_grid_power_enable",
)
DISPATCH_READ_NAMES = (
    "battery_soc",
    "grid_power",
    "battery_power",
    "household_load_power",
    # The two PV strings the Energy Manager's readings sum. Additive: the dispatch control loop does
    # not read them, so no existing expectation changes.
    "solar_a_power",
    "solar_b_power",
)


def dispatch_fixtures(directory: Path) -> dict[str, Path]:
    """Registry and allowlist containing the exact production dispatch metrics."""
    registry = json.loads(Path(make_settings().object_registry_path).read_text(encoding="utf-8"))
    present = {entry["name"] for entry in registry["entries"]}
    production = json.loads(
        (Path(__file__).resolve().parents[1] / "app" / "catalog" / "objects.json").read_text(encoding="utf-8")
    )
    required = set(DISPATCH_WRITE_NAMES + DISPATCH_READ_NAMES)
    registry["entries"].extend(
        entry for entry in production["entries"] if entry["name"] in required and entry["name"] not in present
    )
    by_name = {entry["name"]: entry for entry in production["entries"]}
    allowlist_entries = []
    for name in DISPATCH_WRITE_NAMES:
        item = {"name": name, "data_type": by_name[name]["data_type"]}
        if name == "power_mng_soc_strategy":
            item["allowed_values"] = [0, 1]
        elif name == "power_mng_soc_target_set":
            item.update(minimum=0.0, maximum=1.0)
        elif name == "power_mng_battery_power_extern":
            item.update(minimum=-5000.0, maximum=5000.0)
        allowlist_entries.append(item)
    registry_path = directory / "dispatch_objects.json"
    allowlist_path = directory / "dispatch_write_allowed.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    allowlist_path.write_text(json.dumps({"version": 1, "entries": allowlist_entries}), encoding="utf-8")
    return {"object_registry_path": registry_path, "write_allowlist_path": allowlist_path}


async def admin_session_headers(client, password: str) -> dict[str, str]:
    """Log the client in with a cookie session; returns the CSRF header every later admin call needs.

    Device, trust and token changes are session-only, so a PAT cannot drive them in tests.
    """
    csrf = (await client.get("/admin/api/session")).json()["csrf_token"]
    login = await client.post(
        "/admin/api/login", headers={"X-CSRF-Token": csrf}, json={"username": "admin", "password": password}
    )
    assert login.status_code == 200, login.text
    return {"X-CSRF-Token": login.json()["csrf_token"]}
