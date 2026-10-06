#!/usr/bin/env python3
#
# tests/conftest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared test doubles."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.config import _REMOVED_ENV_NAMES, Settings


def settings_env_names() -> list[str]:
    """Every environment variable name Settings reads now, plus removed/legacy names it used to
    read (``_REMOVED_ENV_NAMES``), so a deprecated export cannot leak into a test either."""
    return [name.upper() for name in Settings.model_fields] + list(_REMOVED_ENV_NAMES)


@pytest.fixture(autouse=True)
def _isolate_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep exported operator variables of the developer shell out of the tests."""
    for name in settings_env_names():
        monkeypatch.delenv(name, raising=False)


class ManualClock:
    """Clock with separate wall and monotonic axes; sleepers wake when time is advanced."""

    def __init__(self) -> None:
        self._wall = datetime(2026, 1, 1, tzinfo=UTC)
        self._mono = 1000.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self._mono + seconds, future))
        await future

    def advance(self, seconds: float) -> None:
        """Advance both axes and wake every sleeper whose deadline has passed."""
        self._mono += seconds
        self._wall += timedelta(seconds=seconds)
        due = [item for item in self._sleepers if item[0] <= self._mono]
        self._sleepers = [item for item in self._sleepers if item[0] > self._mono]
        for _, future in due:
            if not future.done():
                future.set_result(None)

    def jump_wall(self, seconds: float) -> None:
        """Simulate a system time step that must not affect monotonic deadlines."""
        self._wall += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


class AutoClock(ManualClock):
    """Clock whose sleep advances time at once; for properties that need no concurrency in time."""

    async def sleep(self, seconds: float) -> None:
        self.advance(max(seconds, 0))
        await asyncio.sleep(0)
