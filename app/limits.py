#!/usr/bin/env python3
#
# app/limits.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared product limits used across settings, planning, and execution layers."""

TARGET_SOC_MIN_PERCENT = 7.0
TARGET_SOC_MAX_PERCENT = 97.0
MAX_POWER_W = 50_000.0
# Upper input bound for a per-device usable battery capacity (expert override / planning seed).
# Single source so the dispatch API, the planning API and the config seeds cannot drift apart.
MAX_USABLE_CAPACITY_WH = 1_000_000.0
# Conservative fallback for a device without configured or stored power limits. The limit can only
# lower the requested power and the inverter/BMS keeps its own protection, so a low value is safe.
DEFAULT_DEVICE_POWER_LIMIT_W = 3_000.0
