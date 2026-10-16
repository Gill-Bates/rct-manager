#!/usr/bin/env python3
#
# app/weather/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Read-only PV/solar forecast domain.

Open-Meteo supplies tilted-plane irradiance; :mod:`app.weather` turns it into the vendor-neutral
``SolarForecast`` the optimizer already defines. A separate read-only geocoding client resolves the
Weather tab's location search to coordinates and a timezone. Nothing in this package touches the
inverter, dispatch or battery control: a provider failure is a forecast gap, never a control-path
error.
"""
