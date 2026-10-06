#!/usr/bin/env python3
#
# tests/test_access_log_level.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import logging

import pytest

from app.api.middleware import access_log_level


@pytest.mark.parametrize(
    ("method", "path", "status", "level"),
    [
        ("GET", "/admin/static/css/admin.css", 200, logging.DEBUG),
        ("GET", "/login", 200, logging.DEBUG),
        ("GET", "/", 303, logging.DEBUG),
        ("GET", "/ui/dashboard", 200, logging.DEBUG),
        ("GET", "/admin/api/session", 200, logging.DEBUG),
        ("GET", "/admin/api/devices", 200, logging.DEBUG),
        ("POST", "/admin/api/login", 200, logging.INFO),
        ("POST", "/admin/api/logout", 200, logging.INFO),
        ("POST", "/admin/api/change-password", 200, logging.INFO),
        ("PUT", "/admin/api/settings", 200, logging.INFO),
        ("DELETE", "/admin/api/tokens/abc", 200, logging.INFO),
        ("GET", "/api/v1/devices", 200, logging.INFO),
        ("GET", "/metrics", 200, logging.DEBUG),
        ("GET", "/metrics", 401, logging.WARNING),
        ("GET", "/health", 200, logging.INFO),
        ("POST", "/admin/api/login", 401, logging.WARNING),
        ("GET", "/admin/static/missing.js", 404, logging.WARNING),
        ("GET", "/api/v1/devices", 500, logging.ERROR),
    ],
)
def test_access_log_level(method, path, status, level):
    assert access_log_level(method, path, status) == level
