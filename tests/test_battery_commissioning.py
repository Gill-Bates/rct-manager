"""Commissioning date derived from the inverter energy loggers, persisted per inverter serial."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.admin.battery_commissioning import BACKOFF_BASE, EMPTY_RETRY, BatteryCommissioningService
from app.admin.store import AdminStore
from app.errors import DeviceApiError
from app.gateway.rct_history import MONTH_ENERGY_LOG_METRIC, YEAR_ENERGY_LOG_METRIC, RctCommissioningHistory
from app.protocol.timeseries import TimeSeriesPoint

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def _p(year: int, month: int, value: float) -> TimeSeriesPoint:
    return TimeSeriesPoint(datetime(year, month, 1, tzinfo=UTC), value)


class _Gateway:
    """Serves each logger as pages: a query returns the entries strictly before `before`, newest first."""

    def __init__(self, logs: dict[str, list[TimeSeriesPoint]], page: int = 2, fail: set[str] | None = None) -> None:
        self.logs, self.page, self.fail, self.queries = logs, page, fail or set(), []

    async def query_history_series(self, device_id, name, before):
        self.queries.append((name, before))
        if name in self.fail:
            raise DeviceApiError("device_unreachable")
        older = sorted((p for p in self.logs.get(name, []) if p.timestamp < before), key=lambda p: p.timestamp)
        return tuple(older[-self.page:])


async def _no_pause(_seconds: float) -> None:
    return None


def _earliest(gateway: _Gateway) -> datetime | None:
    return asyncio.run(RctCommissioningHistory(gateway, _no_pause).earliest_energy("main", now=NOW))


def test_earliest_non_zero_entry_is_found_by_paging_backward_over_both_loggers() -> None:
    gateway = _Gateway({
        YEAR_ENERGY_LOG_METRIC: [_p(2020, 1, 0.0), _p(2021, 1, 5000.0), _p(2022, 1, 9000.0), _p(2023, 1, 9500.0)],
        MONTH_ENERGY_LOG_METRIC: [_p(2025, 11, 0.0), _p(2025, 12, 300.0), _p(2026, 1, 400.0)],
    })
    # Zero entries are paged through but never chosen; the year log reaches back furthest.
    assert _earliest(gateway) == datetime(2021, 1, 1, tzinfo=UTC)
    year_queries = [before for name, before in gateway.queries if name == YEAR_ENERGY_LOG_METRIC]
    assert year_queries[0] == NOW and len(year_queries) == 3  # two pages plus the empty terminator
    assert year_queries == sorted(year_queries, reverse=True)


def test_month_log_wins_when_it_reaches_further_back_and_implausible_dates_are_ignored() -> None:
    gateway = _Gateway({
        YEAR_ENERGY_LOG_METRIC: [TimeSeriesPoint(datetime(1970, 1, 1, tzinfo=UTC), 99.0), _p(2024, 1, 7000.0)],
        MONTH_ENERGY_LOG_METRIC: [_p(2023, 6, 100.0), _p(2023, 7, 200.0)],
    })
    assert _earliest(gateway) == datetime(2023, 6, 1, tzinfo=UTC)


def test_empty_or_all_zero_logs_give_none() -> None:
    assert _earliest(_Gateway({})) is None
    assert _earliest(_Gateway({YEAR_ENERGY_LOG_METRIC: [_p(2021, 1, 0.0)]})) is None


def test_a_failing_logger_raises_instead_of_returning_a_partial_date() -> None:
    gateway = _Gateway({YEAR_ENERGY_LOG_METRIC: [_p(2022, 1, 5.0)]}, fail={MONTH_ENERGY_LOG_METRIC})
    try:
        _earliest(gateway)
    except DeviceApiError:
        return
    raise AssertionError("expected DeviceApiError")


class _Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> datetime:
        return self.at


class _Provider:
    def __init__(self, results: list) -> None:
        self.results, self.calls = results, 0

    async def earliest_energy(self, device_id, *, now):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _service(tmp_path: Path, provider, serial: str | None = "SN-A", ready: bool = True):
    store = AdminStore(tmp_path / "admin.db", "x" * 32)
    store.initialize()
    clock = _Clock()
    service = BatteryCommissioningService(
        provider, store, clock, lambda: ["main"], lambda _d: serial, lambda _d: ready
    )
    return service, store, clock


def test_service_persists_the_derived_date_once_per_serial(tmp_path: Path) -> None:
    provider = _Provider([datetime(2021, 5, 1, tzinfo=UTC)])
    service, store, _ = _service(tmp_path, provider)
    asyncio.run(service.run_due_once())
    asyncio.run(service.run_due_once())
    assert provider.calls == 1
    stored = store.get_inverter_commissioning("SN-A")
    assert (stored["source"], stored["commissioned_at"]) == ("logger_derived", "2021-05-01T00:00:00+00:00")
    store.close()


def test_service_retries_failures_with_backoff_and_empty_logs_much_later(tmp_path: Path) -> None:
    provider = _Provider([DeviceApiError("device_unreachable"), DeviceApiError("device_unreachable"), None,
                          datetime(2022, 3, 1, tzinfo=UTC)])
    service, store, clock = _service(tmp_path, provider)
    asyncio.run(service.run_due_once())
    asyncio.run(service.run_due_once())  # still inside the backoff window
    assert provider.calls == 1 and store.get_inverter_commissioning("SN-A") is None
    clock.at += BACKOFF_BASE
    asyncio.run(service.run_due_once())  # second failure doubles the wait
    clock.at += BACKOFF_BASE
    asyncio.run(service.run_due_once())
    assert provider.calls == 2
    clock.at += BACKOFF_BASE
    asyncio.run(service.run_due_once())  # logs empty: stays unknown, next try only after EMPTY_RETRY
    assert provider.calls == 3 and store.get_inverter_commissioning("SN-A") is None
    clock.at += EMPTY_RETRY - timedelta(minutes=1)
    asyncio.run(service.run_due_once())
    assert provider.calls == 3
    clock.at += timedelta(minutes=2)
    asyncio.run(service.run_due_once())
    assert store.get_inverter_commissioning("SN-A")["commissioned_at"].startswith("2022-03-01")
    store.close()


def test_manual_date_wins_and_needs_no_derivation(tmp_path: Path) -> None:
    provider = _Provider([datetime(2021, 5, 1, tzinfo=UTC)])
    service, store, _ = _service(tmp_path, provider)
    store.set_inverter_commissioning("SN-A", datetime(2021, 8, 1, tzinfo=UTC), "manual")
    asyncio.run(service.run_due_once())
    assert provider.calls == 0
    assert store.get_inverter_commissioning("SN-A")["source"] == "manual"
    store.close()


def test_service_skips_unknown_serial_and_unready_device(tmp_path: Path) -> None:
    provider = _Provider([datetime(2021, 5, 1, tzinfo=UTC)])
    for index, (serial, ready) in enumerate([(None, True), ("SN-A", False)]):
        (tmp_path / str(index)).mkdir()
        service, store, _ = _service(tmp_path / str(index), provider, serial=serial, ready=ready)
        asyncio.run(service.run_due_once())
        store.close()
    assert provider.calls == 0
