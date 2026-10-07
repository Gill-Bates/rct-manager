#!/usr/bin/env python3
#
# tests/test_export_pusher.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Push export against a local fake HTTP server (failures stay contained) and line protocol formatting."""

import json
import math
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.config import Settings
from app.export.lineprotocol import batches, build_lines, escape_key, escape_measurement
from app.export.pusher import MAX_BACKOFF_SECONDS, PushExporter
from app.observability.exporter import MetricsExporter, _labels
from app.observability.stats import ServiceCounters

BASE = {"devices": "wr1=10.0.0.5:8899"}


class _Server:
    def __init__(self, status: int = 204) -> None:
        self.status, self.requests = status, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, body: bytes = b"", status: int | None = None) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append((self.command, self.path, dict(self.headers), self.rfile.read(length).decode()))
                self.send_response(status or outer.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                self._answer(b"denied" if outer.status >= 400 else b"")

            def do_GET(self) -> None:
                self._answer(json.dumps({"dataset": []}).encode(), 200)

            def log_message(self, *args) -> None:
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _Metrics:
    def collect(self):
        return [("rct_grid_power", {"device": "main"}, 12.5)]


class _Clock:
    async def sleep(self, seconds: float) -> None:
        return None


@pytest.fixture
def server():
    srv = _Server()
    yield srv
    srv.close()


def _exporter(**kwargs) -> tuple[PushExporter, ServiceCounters]:
    stats = ServiceCounters(export_enabled=True)
    return PushExporter(Settings(_env_file=None, **BASE, **kwargs), _Metrics(), stats, _Clock()), stats


INFLUX = {"db_type": "influxdb_v2", "influxdb_token": "tok", "influxdb_organization": "my org", "influxdb_bucket": "b"}


async def test_influxdb_write_success(server) -> None:
    exporter, stats = _exporter(**INFLUX, influxdb_hostname=server.url)
    assert await exporter.cycle() == 30
    method, path, headers, body = server.requests[0]
    assert method == "POST" and path.startswith("/api/v2/write?org=my%20org&bucket=b&precision=ns")
    assert headers["Authorization"] == "Token tok"
    assert body.startswith("rct,device=main rct_grid_power=12.5 ")
    assert (stats.export_success, stats.export_failures) == (1, 0) and stats.export_last_success_unix


async def test_auth_failure_backs_off_without_raising(server) -> None:
    server.status = 401
    exporter, stats = _exporter(**INFLUX, influxdb_hostname=server.url)
    first = await exporter.cycle()
    second = await exporter.cycle()
    assert stats.export_failures == 2 and stats.export_success == 0
    assert first == second == MAX_BACKOFF_SECONDS


@pytest.mark.parametrize(("status", "expected_delay"), [(400, MAX_BACKOFF_SECONDS), (429, 60)])
async def test_http_failure_retry_delay(server, status, expected_delay) -> None:
    server.status = status
    exporter, stats = _exporter(**INFLUX, influxdb_hostname=server.url)
    assert await exporter.cycle() == expected_delay
    assert stats.export_failures == 1


async def test_server_down_does_not_crash() -> None:
    srv = _Server()
    url = srv.url
    srv.close()
    exporter, stats = _exporter(**INFLUX, influxdb_hostname=url)
    assert await exporter.cycle() > 30
    assert stats.export_failures == 1


async def test_questdb_basic_auth_and_provisioning(server) -> None:
    exporter, stats = _exporter(
        db_type="questdb", questdb_hostname=server.url, questdb_username="u", questdb_password="p",
        questdb_retention_days=0,
    )
    assert await exporter.cycle() == 30
    posts = [r for r in server.requests if r[0] == "POST"]
    assert posts[0][1] == "/write" and posts[0][2]["Authorization"].startswith("Basic ")
    assert any("CREATE+TABLE" in r[1] or "CREATE%20TABLE" in r[1] for r in server.requests if r[0] == "GET")
    assert stats.export_success == 1


async def test_collect_runs_on_the_event_loop_thread(server) -> None:
    seen: list[int] = []

    class Recording(_Metrics):
        def collect(self):
            seen.append(threading.get_ident())
            return super().collect()

    stats = ServiceCounters(export_enabled=True)
    settings = Settings(_env_file=None, **BASE, **INFLUX, influxdb_hostname=server.url)
    exporter = PushExporter(settings, Recording(), stats, _Clock())
    assert await exporter.cycle() == 30
    assert seen == [threading.get_ident()]


def test_escaping() -> None:
    assert escape_measurement("my table,x") == "my\\ table\\,x"
    assert escape_key("a b,c=d") == "a\\ b\\,c\\=d"
    assert "\n" not in escape_key("a\nb")


def test_groups_fields_by_tag_set_and_writes_floats() -> None:
    samples = [
        ("rct_grid_power", {"device": "main"}, 5.0),
        ("rct_battery_soc", {"device": "main"}, 0.5),
        ("rct_api_requests_total", {}, 7.0),
        ("rct_state", {"device": "main", "state": "on x"}, 1.0),
        ("bad", {"device": "main"}, math.nan),
        ("inf", {"device": "main"}, math.inf),
    ]
    lines = build_lines(samples, "rct", 42)
    assert lines == [
        "rct rct_api_requests_total=7.0 42",
        "rct,device=main rct_battery_soc=0.5,rct_grid_power=5.0 42",
        "rct,device=main,state=on\\ x rct_state=1.0 42",
    ]


def test_empty_tag_values_are_dropped_and_batches_split() -> None:
    assert build_lines([("m", {"device": ""}, 1.0)], "t", 1) == ["t m=1.0 1"]
    chunks = batches([f"l{i}" for i in range(5)], size=2)
    assert chunks == ["l0\nl1\n", "l2\nl3\n", "l4\n"]


def test_labels_keep_pairs_for_the_exporter() -> None:
    assert _labels(device="a").pairs == {"device": "a"}
    assert _labels() == ""
    assert MetricsExporter.collect is not None
