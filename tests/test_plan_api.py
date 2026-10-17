#!/usr/bin/env python3
#
# tests/test_plan_api.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Tests for the read-only charging-plan API and default-OFF runtime handoff."""

import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.admin.store import AdminStore
from app.dispatch.models import DeviceLimits
from app.dispatch.store import DispatchStore
from app.energy.models import EnergyMode, ModeRecord
from app.energy.plan_handoff import PlanDispatchHandoff
from app.energy.readings import DeviceReading
from app.optimizer.consumption import BaselineConsumptionForecastProvider
from app.optimizer.models import (
    BatteryState,
    EnergyAction,
    EnergyPlan,
    EnergyPlanSlot,
    SolarForecast,
    SolarForecastInterval,
)
from app.optimizer.planner import DynamicChargeOptimizer, PlanParameters, planning_start
from app.optimizer.scheduler import InMemoryPlanScheduler
from app.optimizer.service import PlanningService
from app.spotmarket.models import (
    SpotMarketProvider,
    SpotPriceInterval,
    SpotPriceSnapshot,
)
from tests.api_helpers import make_settings, running_app

HORIZON = timedelta(hours=1)


class _PricesSource:
    def __init__(self, snapshot: SpotPriceSnapshot | None) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> SpotPriceSnapshot | None:
        return self._snapshot


class _SolarSource:
    def __init__(self, forecast: SolarForecast) -> None:
        self._forecast = forecast

    def forecast(self, now: datetime, horizon: timedelta) -> SolarForecast:
        return self._forecast


class _BatterySource:
    def __init__(self, state: BatteryState | None) -> None:
        self._state = state

    def current(self, device_id: str) -> BatteryState | None:
        return self._state


def _prices(now: datetime) -> SpotPriceSnapshot:
    start = planning_start(now)
    intervals = tuple(
        SpotPriceInterval(
            start + timedelta(minutes=15 * index),
            start + timedelta(minutes=15 * (index + 1)),
            Decimal("0.05") if index == 0 else Decimal("0.30"),
        )
        for index in range(4)
    )
    return SpotPriceSnapshot(
        provider=SpotMarketProvider.ENERGY_CHARTS,
        bidding_zone="DE-LU",
        currency="EUR",
        fetched_at=now,
        intervals=intervals,
    )


def _solar(now: datetime) -> SolarForecast:
    start = planning_start(now)
    return SolarForecast(
        created_at=now,
        intervals=tuple(
            SolarForecastInterval(
                start + timedelta(minutes=15 * index),
                start + timedelta(minutes=15 * (index + 1)),
                50.0,
            )
            for index in range(4)
        ),
    )


def _battery(now: datetime) -> BatteryState:
    return BatteryState(
        soc_percent=50.0,
        usable_capacity_wh=10_000.0,
        max_charge_power_w=3000.0,
        max_discharge_power_w=3000.0,
        measured_at=now,
    )


def _service(harness, *, battery: BatteryState | None) -> PlanningService:
    clock = harness.runtime.clock
    now = clock.now()
    return PlanningService(
        device_id="main",
        clock=clock,
        optimizer=DynamicChargeOptimizer(PlanParameters()),
        scheduler=InMemoryPlanScheduler(),
        prices=_PricesSource(_prices(now)),
        solar=_SolarSource(_solar(now)),
        consumption=BaselineConsumptionForecastProvider(400.0),
        battery=_BatterySource(battery),
        horizon=HORIZON,
        plan_ttl=timedelta(minutes=30),
        recompute_interval_seconds=300.0,
        spot_max_age_seconds=3600.0,
        solar_max_age_seconds=3600.0,
        consumption_max_age_seconds=3600.0,
        battery_max_age_seconds=300.0,
    )


async def test_plan_endpoint_reports_current_plan_with_data_age() -> None:
    async with running_app() as harness:
        service = _service(harness, battery=_battery(harness.runtime.clock.now()))
        await service.recompute()
        harness.app.state.planning_services = {"main": service}

        response = await harness.client.get("/api/v1/devices/main/plan")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["available"] is True
        assert body["slots"], "a valid plan should carry slots"
        assert body["data_age"]["spot_seconds"] is not None
        assert body["data_age"]["battery_seconds"] is not None
        assert body["created_at"] is not None
        assert body["valid_until"] is not None


async def test_plan_endpoint_reports_absent_when_no_service_or_no_plan() -> None:
    async with running_app() as harness:
        # No service registered -> absent.
        response = await harness.client.get("/api/v1/devices/main/plan")
        assert response.status_code == 200, response.text
        assert response.json()["available"] is False

        # A service whose required battery input is missing never builds a plan -> absent.
        service = _service(harness, battery=None)
        await service.recompute()
        harness.app.state.planning_services = {"main": service}
        response = await harness.client.get("/api/v1/devices/main/plan")
        assert response.json()["available"] is False


async def test_factory_starts_registered_planning_service_and_serves_its_plan(
    tmp_path: Path
) -> None:
    settings = make_settings(
        hmac_secret="s" * 48,
        admin_db_path=tmp_path / "admin.db",
        dispatch_db_path=tmp_path / "dispatch.db",
        plan_horizon_hours=1.0,
    )
    store = AdminStore(settings.admin_db_path, "s" * 48)
    password = store.initialize()
    assert store.change_password(password, "replacement-test-password")
    store.close()
    limits_store = DispatchStore(settings.dispatch_db_path, "s" * 48)
    limits_store.initialize()
    limits_store.put_device_config(
        "main", DeviceLimits(3000.0, 3000.0, usable_capacity_wh=9000.0)
    )

    async with running_app(settings) as harness:
        service = harness.app.state.planning_services["main"]
        # Off is the factory state and suspends planning; this test needs the active computation.
        harness.runtime.energy._mode_states["main"] = ModeRecord("main", mode=EnergyMode.MANUAL)
        harness.runtime.energy.activity.notify()
        assert any(
            task.get_coro().__qualname__ == "PlanningService.run"
            for task in harness.runtime.tasks
        )
        now = harness.runtime.clock.now()
        service._prices = _PricesSource(_prices(now))
        service._solar = _SolarSource(_solar(now))
        service._consumption = BaselineConsumptionForecastProvider(400.0)
        service._battery._readings = SimpleNamespace(
            readings=lambda _: SimpleNamespace(
                battery_soc_percent=DeviceReading(50.0, 0.0, False)
            )
        )
        assert service._battery.current("main").usable_capacity_wh == 9000.0

        # The factory's background PlanningService.run loop shares a per-device SingleFlight with this
        # on-demand recompute: a call issued while the loop's own recompute is still in flight is
        # collapsed onto that earlier computation, which ran against the real (empty) sources before
        # the overrides above and yields None. Re-issue until a call starts fresh against the
        # overridden sources, which always produce a plan. Bounded so a genuine failure still fails.
        plan = None
        for _ in range(10):
            plan = await service.recompute()
            if plan is not None:
                break
        assert plan is not None
        response = await harness.client.get("/api/v1/devices/main/plan")
        assert response.status_code == 200
        assert response.json()["available"] is True


async def test_factory_default_off_handoff_logs_slots_without_commanding(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.setattr("app.api.app_factory._PLAN_HANDOFF_POLL_SECONDS", 0.01)
    settings = make_settings(
        hmac_secret="s" * 48,
        admin_db_path=tmp_path / "admin.db",
        dispatch_db_path=tmp_path / "dispatch.db",
    )
    store = AdminStore(settings.admin_db_path, "s" * 48)
    password = store.initialize()
    assert store.change_password(password, "replacement-test-password")
    store.close()

    async with running_app(settings) as harness:
        service = harness.app.state.planning_services["main"]
        assert settings.enable_automatic_dispatch is False
        assert harness.runtime.energy is not None
        command = AsyncMock(
            side_effect=AssertionError("default-OFF handoff must not command hardware")
        )
        monkeypatch.setattr(harness.runtime.energy, "command", command)
        active_now = harness.runtime.clock.now()
        active_slot = EnergyPlanSlot(
            start=active_now - timedelta(seconds=1),
            end=active_now + timedelta(minutes=1),
            action=EnergyAction.CHARGE,
            target_soc_percent=80.0,
            max_power_w=1500.0,
            reason="cheap spot price",
        )
        service.scheduler.set_plan(
            EnergyPlan(
                created_at=active_slot.start,
                valid_until=active_slot.end,
                slots=(active_slot,),
            )
        )
        with caplog.at_level(logging.INFO, logger="app.energy.plan_handoff"):
            async with asyncio.timeout(1.0):
                while not any(
                    "would offer charge" in record.message
                    for record in caplog.records
                    if record.name == "app.energy.plan_handoff"
                ):
                    await asyncio.sleep(0.01)
            caplog.clear()
            service.scheduler.clear()
            async with asyncio.timeout(1.0):
                while not any(
                    "would offer auto" in record.message
                    for record in caplog.records
                    if record.name == "app.energy.plan_handoff"
                ):
                    await asyncio.sleep(0.01)
            caplog.clear()
            active_slot_lookup = service.scheduler.active_slot

            def broken_slot_lookup(_now):
                raise RuntimeError("test slot lookup failure")

            monkeypatch.setattr(service.scheduler, "active_slot", broken_slot_lookup)
            async with asyncio.timeout(1.0):
                while not any(
                    "Plan handoff failed" in record.message
                    for record in caplog.records
                    if record.name == "app.api.app_factory"
                ):
                    await asyncio.sleep(0.01)
            monkeypatch.setattr(service.scheduler, "active_slot", active_slot_lookup)
            caplog.clear()
            service.scheduler.set_plan(
                EnergyPlan(
                    created_at=active_slot.start,
                    valid_until=active_slot.end,
                    slots=(active_slot,),
                )
            )
            async with asyncio.timeout(1.0):
                while not any(
                    "would offer charge" in record.message
                    for record in caplog.records
                    if record.name == "app.energy.plan_handoff"
                ):
                    await asyncio.sleep(0.01)
        command.assert_not_awaited()


@pytest.mark.parametrize("recovery_succeeds", [True, False])
async def test_enabled_handoff_waits_for_successful_recovery(
    tmp_path: Path, monkeypatch, recovery_succeeds: bool
) -> None:
    settings = make_settings(
        hmac_secret="s" * 48,
        admin_db_path=tmp_path / "admin.db",
        dispatch_db_path=tmp_path / "dispatch.db",
        enable_write_support=True,
        enable_automatic_dispatch=True,
    )
    store = AdminStore(settings.admin_db_path, "s" * 48)
    password = store.initialize()
    assert store.change_password(password, "replacement-test-password")
    store.close()
    recover_entered = asyncio.Event()
    finish_recovery = asyncio.Event()
    offered = asyncio.Event()

    async def held_recovery(_controller):
        recover_entered.set()
        await finish_recovery.wait()
        if not recovery_succeeds:
            raise RuntimeError("test recovery failure")

    async def record_offer(handoff, _device_id, _slot):
        assert handoff._enabled is True
        offered.set()
        return False

    monkeypatch.setattr(
        "app.dispatch.controller.DispatchController.recover", held_recovery
    )
    monkeypatch.setattr(PlanDispatchHandoff, "offer", record_offer)
    async with running_app(settings) as harness:
        await asyncio.wait_for(recover_entered.wait(), timeout=1.0)
        assert not offered.is_set()
        finish_recovery.set()
        if recovery_succeeds:
            await asyncio.wait_for(offered.wait(), timeout=1.0)
            assert harness.runtime.dispatch_recovery_failed is False
        else:
            async with asyncio.timeout(1.0):
                while not harness.runtime.dispatch_recovery_failed:
                    await asyncio.sleep(0.01)
            assert not offered.is_set()


async def test_plan_endpoint_rejects_unknown_device() -> None:
    async with running_app() as harness:
        response = await harness.client.get("/api/v1/devices/ghost/plan")
        assert response.status_code == 404, response.text


def test_plan_router_module_imports_no_rct_adapter() -> None:
    # The structural guarantee is enforced centrally by
    # tests/test_structure_smoke.py::test_routers_do_not_import_the_rct_adapter over every router;
    # here we assert the plan router carries no actual import statement for the adapter.
    source = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "routers" / "plan.py"
    ).read_text()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "gateway.rct" not in stripped


async def test_plan_endpoint_exposes_forecast_enums_and_openapi_documents_them() -> (
    None
):
    from app.optimizer.models import ForecastBasis, ForecastConfidence

    async with running_app() as harness:
        service = _service(harness, battery=_battery(harness.runtime.clock.now()))
        await service.recompute()
        harness.app.state.planning_services = {"main": service}
        body = (await harness.client.get("/api/v1/devices/main/plan")).json()
        # Free-form assumption text never leaks into the contract field.
        service._forecast_quality = (
            ForecastBasis.HISTORY_FALLBACK,
            ForecastConfidence.LOW,
        )
        body = (await harness.client.get("/api/v1/devices/main/plan")).json()
        assert (body["forecast_basis"], body["forecast_confidence"]) == (
            "history_fallback",
            "low",
        )

        schemas = harness.app.openapi()["components"]["schemas"]
        assert schemas["ForecastBasis"]["enum"] == [m.value for m in ForecastBasis]
        assert schemas["ForecastConfidence"]["enum"] == [
            m.value for m in ForecastConfidence
        ]
        assert schemas["PlanResponse"]["properties"]["forecast_basis"]["description"]
