#!/usr/bin/env python3
#
# tests/e2e/run_server.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Start the gateway for browser E2E runs against an isolated database and a simulated inverter.

Usage: python -m tests.e2e.run_server DB_PATH HTTP_PORT DEVICE_PORT [energy]
"energy" enables write support and the dispatch store (next to DB_PATH) for the Energy Manager smoke.
The admin database path is not operator-settable, so it is injected here and the real data/rct.db
is never touched.
"""

import sys
from pathlib import Path

from app.api.app_factory import create_app
from app.api.server import run_server
from app.config import Settings

SECRET = "e2e-" + "x" * 48


def main(db: str, http_port: str, device_port: str, mode: str = "") -> int:
    path = Path(db).resolve()
    if path == (Path(__file__).resolve().parents[2] / "data" / "rct.db"):
        raise SystemExit("refusing to use the real database")
    extra = (
        {
            "enable_write_support": True,
            "dispatch_db_path": path.with_name("dispatch.db"),
            "dispatch_max_charge_power_w": 3000,
            "dispatch_max_discharge_power_w": 5000,
        }
        if mode == "energy"
        else {}
    )
    settings = Settings(
        _env_file=None, hmac_secret=SECRET, admin_db_path=path, bind_port=int(http_port),
        devices=[{"device_id": "sim", "host": "127.0.0.1", "port": int(device_port), "display_name": "Simulator"}],
        # The browser driver issues admin requests far faster than an operator does, so the default
        # 60-per-minute admin limit returns 429 midway through a full run and breaks checks that are
        # about the UI, not about throttling. No assertion depends on the real limiter: the suite
        # mocks its own 429 client-side via page.route(), and the metrics_rate_limit_* keys it edits
        # are a different limit.
        rate_limit_requests=10_000,
        **extra,
    )
    app = create_app(settings)
    return run_server(app, app.state.runtime.settings)


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:5]))
