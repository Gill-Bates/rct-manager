#!/usr/bin/env python3
#
# tests/test_container_bind_warning.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""A loopback bind inside a container is logged loudly but never refused (R5)."""

import logging
from types import SimpleNamespace

from app.api import server


def _settings(address: str) -> SimpleNamespace:
    return SimpleNamespace(bind_address=address, bind_port=8000)


def test_warns_on_loopback_in_container(monkeypatch, caplog) -> None:
    # IPv4 and IPv6 loopback hit the same is_loopback branch; one address covers it.
    monkeypatch.setattr(server, "_in_container", lambda: True)
    with caplog.at_level(logging.WARNING, logger=server.log.name):
        server.warn_if_loopback_in_container(_settings("127.0.0.1"))
    assert any("loopback" in r.getMessage() and "0.0.0.0" in r.getMessage() for r in caplog.records)
    assert any("behind reverse proxy" in r.getMessage() for r in caplog.records)


def test_silent_for_wildcard_bind_in_container(monkeypatch, caplog) -> None:
    monkeypatch.setattr(server, "_in_container", lambda: True)
    with caplog.at_level(logging.WARNING, logger=server.log.name):
        server.warn_if_loopback_in_container(_settings("0.0.0.0"))
    assert not caplog.records


def test_silent_for_loopback_outside_container(monkeypatch, caplog) -> None:
    monkeypatch.setattr(server, "_in_container", lambda: False)
    with caplog.at_level(logging.WARNING, logger=server.log.name):
        server.warn_if_loopback_in_container(_settings("127.0.0.1"))
    assert not caplog.records


def test_marker_variable_marks_container(monkeypatch) -> None:
    monkeypatch.setenv("RCT_API_CONTAINER", "1")
    assert server._in_container()
