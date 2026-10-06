#!/usr/bin/env python3
#
# tests/test_contract_properties.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Properties 9, 10, 12 and 13: neutral contract, error contract, no device load for rejected requests."""

import asyncio
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.api.problems import ErrorCode
from app.catalog.registry import RegistryCatalog
from app.config import Settings
from app.scheduling.shutdown import ShutdownPhase, ShutdownPlan
from app.transport.endpoint import LockReason
from tests.api_helpers import (
    ACTION_NAME,
    HOST,
    PORT,
    READ_TOKEN,
    SLAVE_NETWORK_ID,
    TARGET_NAME,
    WRITE_TOKEN,
    make_settings,
    running_app,
    write_fixtures,
)

CATALOG = RegistryCatalog.from_file(make_settings().object_registry_path)
KNOWN = list(CATALOG.names())
FIELD_ONLY = {ErrorCode.INVALID_FLOAT, ErrorCode.DECODE_LENGTH_MISMATCH}
TOP_LEVEL_CODES = {c.value for c in ErrorCode} - {c.value for c in FIELD_ONLY}


def _forbidden() -> list[re.Pattern[str]]:
    patterns = [
        re.escape(HOST),
        rf"(?<![\d.]){PORT}(?!\d)",
        rf"(?<![\d.]){SLAVE_NETWORK_ID}(?!\d)",
        "bootloader",
        re.escape(READ_TOKEN.lower()),
        re.escape(WRITE_TOKEN.lower()),
        r"\bt_(float|bool|uint8|int8|uint16|int16|uint32|int32|enum|string|struct)\b",
    ]
    for entry in CATALOG.entries():
        patterns += [f"0x{entry.object_id:08x}", rf"(?<![0-9a-f]){entry.object_id:08x}(?![0-9a-f])"]
        patterns.append(rf"(?<!\d){entry.object_id}(?!\d)")
    return [re.compile(p) for p in patterns]


FORBIDDEN = _forbidden()


def _app_settings() -> Settings:
    return make_settings(enable_periodic_reads=False, max_metrics_per_request=4,
                         max_fresh_metrics_per_request=2)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Spec:
    method: str
    path: str
    params: dict[str, str]
    token: str | None
    body: bytes | None
    request_id: str | None


# No digits: a generated number echoed in an error text must not look like a protocol detail.
_word = st.text(alphabet="ghijklmnopqrstuvwxyz_-", min_size=1, max_size=12)
_device = st.one_of(st.sampled_from(["main", "slave1"]), _word)
_metric = st.one_of(st.sampled_from(KNOWN), _word)
_paths = st.one_of(
    st.sampled_from(["/health", "/api/v1/readiness", "/api/v1/metrics", "/api/v1/devices", "/openapi.json"]),
    st.sampled_from(["/api/v1/vendor/rct/objects", "/api/v1/vendor/rct/transports", "/", "/metrics"]),
    _word.map(lambda w: f"/{w}"),
    _device.map(lambda d: f"/api/v1/devices/{d}/metrics"),
    st.tuples(_device, _metric).map(lambda t: f"/api/v1/devices/{t[0]}/metrics/{t[1]}"),
    st.tuples(_device, _word).map(lambda t: f"/api/v1/devices/{t[0]}/actions/{t[1]}"),
)
_params = st.fixed_dictionaries(
    {},
    optional={
        "names": st.lists(_metric, max_size=6).map(",".join),
        "fresh": st.sampled_from(["true", "false", "maybe", ""]),
    },
)
_request_ids = st.one_of(st.none(), st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), max_size=80))
_specs = st.builds(
    Spec,
    method=st.sampled_from(["GET", "GET", "GET", "POST", "PUT", "DELETE", "PATCH"]),
    path=_paths,
    params=_params,
    token=st.sampled_from([None, READ_TOKEN, WRITE_TOKEN, "not-a-valid-token"]),
    body=st.one_of(st.none(), st.binary(max_size=40)),
    request_id=_request_ids,
)


async def _fire(client: httpx.AsyncClient, spec: Spec) -> httpx.Response:
    headers = {}
    if spec.token is not None:
        headers["Authorization"] = f"Bearer {spec.token}"
    if spec.request_id is not None:
        headers["X-Request-Id"] = spec.request_id
    if spec.body is not None:
        headers["Content-Type"] = "application/json"
    return await client.request(
        spec.method, spec.path, params=spec.params, headers=headers, content=spec.body, timeout=10
    )


def _run(specs: list[Spec]):
    async def scenario() -> list[tuple[Spec, httpx.Response]]:
        async with running_app(_app_settings(), settle=False, authorize=False) as h:
            return [(spec, await _fire(h.client, spec)) for spec in specs]

    return asyncio.run(scenario())


# Feature: rct-rest-api, Property 12: the vendor-neutral contract exposes no protocol details
@settings(max_examples=100, deadline=None)
@given(specs=st.lists(_specs, min_size=1, max_size=3))
def test_neutral_contract_leaks_no_protocol_details(specs: list[Spec]) -> None:
    for spec, response in _run(specs):
        if spec.path.startswith("/api/v1/vendor"):
            continue  # the diagnostic area is outside the neutral contract
        cid = response.headers["x-request-id"].lower()  # a client supplied id is echoed by design
        headers = " ".join(f"{k}:{v}" for k, v in response.headers.items() if k != "x-request-id")
        body = response.text
        if response.headers["content-type"].startswith("application/problem+json"):
            problem = response.json()
            # `instance` echoes the client's own request path (Requirement 25.2), like the request id.
            body = json.dumps({k: v for k, v in problem.items() if k != "instance"})
        text = body.lower().replace(cid, "") + headers.lower()
        for pattern in FORBIDDEN:
            assert not pattern.search(text), (spec, pattern.pattern)


# Feature: rct-rest-api, Property 13: every error response satisfies the error contract
@settings(max_examples=100, deadline=None)
@given(specs=st.lists(_specs, min_size=1, max_size=3))
def test_every_error_response_meets_the_error_contract(specs: list[Spec]) -> None:
    for spec, response in _run(specs):
        cid = response.headers["x-request-id"]
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid)
        if spec.request_id is not None and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", spec.request_id):
            assert cid == spec.request_id  # a valid supplied id is adopted
        assert response.headers["cache-control"] == "no-store"
        if response.status_code < 400:
            continue
        assert response.headers["content-type"].startswith("application/problem+json"), spec
        body = response.json()
        assert body["code"] in TOP_LEVEL_CODES
        assert body["status"] == response.status_code
        assert body["type"].startswith("urn:device-api:problem")
        assert body["correlation_id"] == cid
        for key in ("title", "detail", "instance", "timestamp"):
            assert body[key]


# --- Property 9 ---------------------------------------------------------------------------------
_ALL = ",".join(KNOWN[:6])
_LOAD_FREE = ("/health", "/api/v1/readiness", "/api/v1/metrics", "/api/v1/devices", "/docs")


async def _rejecting(scenario: str, h, repeat: int) -> None:
    bearer = {"Authorization": f"Bearer {READ_TOKEN}"}
    plan = ShutdownPlan(h.runtime.clock.now(), 0.0, 0.0, ShutdownPhase.DRAIN_WORK)
    match scenario:
        case "no_token":
            request, expected = ("/api/v1/devices/main/metrics/battery_soc", {}), 401
        case "bad_token":
            request, expected = ("/api/v1/devices/main/metrics/battery_soc", {"Authorization": "Bearer nope"}), 401
        case "unknown_device":
            request, expected = ("/api/v1/devices/ghost/metrics/battery_soc", bearer), 404
        case "unknown_metric":
            request, expected = ("/api/v1/devices/main/metrics/ghost", bearer), 404
        case "unknown_name_in_query":
            request, expected = ("/api/v1/devices/main/metrics?names=battery_soc,ghost", bearer), 422
        case "invalid_parameter":
            request, expected = ("/api/v1/devices/main/metrics?fresh=maybe", bearer), 422
        case "batch_too_large":
            request, expected = (f"/api/v1/devices/main/metrics?names={_ALL}", bearer), 422
        case "fresh_batch_too_large":
            request, expected = (f"/api/v1/devices/main/metrics?names={','.join(KNOWN[:3])}&fresh=true", bearer), 422
        case "maintenance":
            h.runtime.gateway._devices["main"].endpoint._lock_reason = LockReason.BOOTLOADER_MAGIC
            request, expected = ("/api/v1/devices/main/metrics/ac_power?fresh=true", bearer), 503
        case "shutdown":
            h.runtime.shutdown.plan = plan
            request, expected = ("/api/v1/devices/main/metrics/battery_soc?fresh=true", bearer), 503
        case "unload":
            request, expected = (
                (_LOAD_FREE[repeat % len(_LOAD_FREE)], bearer),
                200,
            )
        case _:
            raise AssertionError(scenario)
    path, headers = request
    response = await h.client.get(path, headers=headers)
    assert response.status_code == expected, (scenario, response.text)


# Feature: rct-rest-api, Property 9: load-free endpoints and rejected requests cause no device load
@settings(max_examples=100, deadline=None)
@given(
    scenario=st.sampled_from(
        [
            "no_token",
            "bad_token",
            "unknown_device",
            "unknown_metric",
            "unknown_name_in_query",
            "invalid_parameter",
            "batch_too_large",
            "fresh_batch_too_large",
            "maintenance",
            "shutdown",
            "unload",
        ]
    ),
    repeat=st.integers(1, 4),
)
def test_rejected_requests_create_no_device_load(scenario: str, repeat: int) -> None:
    async def run() -> None:
        async with running_app(_app_settings(), authorize=False) as h:
            endpoint = h.runtime.gateway._devices["main"].endpoint
            before = (h.net.writes, endpoint.counters.transactions)
            for i in range(repeat):
                await _rejecting(scenario, h, i)
            assert (h.net.writes, endpoint.counters.transactions) == before
            h.runtime.shutdown.plan = None  # let the lifespan exit run the normal shutdown

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["rate_limited", "budget_exhausted"])
def test_rate_and_budget_rejections_create_no_device_load(scenario: str) -> None:
    async def run() -> None:
        limits = {"rate_limit_requests": 2} if scenario == "rate_limited" else {"device_budget_transactions": 2}
        async with running_app(make_settings(**limits, enable_periodic_reads=False)) as h:  # setup would move counters
            endpoint = h.runtime.gateway._devices["main"].endpoint
            if scenario == "budget_exhausted":
                # The heartbeat is budget-exempt, so the fresh batch below uses the whole budget.
                first = await h.client.get("/api/v1/devices/main/metrics?names=battery_soc,ac_power&fresh=true")
                assert first.status_code == 200
            else:
                for _ in range(2):
                    assert (await h.client.get("/api/v1/devices")).status_code == 200
            before = (h.net.writes, endpoint.counters.transactions)
            for _ in range(3):
                response = await h.client.get("/api/v1/devices/main/metrics?names=grid_power&fresh=true")
                assert response.status_code == 429
                assert int(response.headers["retry-after"]) >= 1
            assert (h.net.writes, endpoint.counters.transactions) == before

    asyncio.run(run())


# --- Property 10 --------------------------------------------------------------------------------
_WRITER = {"Authorization": f"Bearer {WRITE_TOKEN}"}


@st.composite
def _write_case(draw):
    low = draw(st.integers(-100, 100))
    high = low + draw(st.integers(1, 100))
    step = draw(st.sampled_from([None, 0.5, 0.25]))
    kind = draw(st.sampled_from(["above", "below", "string", "bool", "null", "step", "unapproved", "action"]))
    if kind == "step" and step is None:
        kind = "above"
    overshoot = draw(st.integers(1, 1000))
    match kind:
        case "above":
            value = high + overshoot
        case "below":
            value = low - overshoot
        case "string":
            value = draw(st.text(max_size=8))
        case "bool":
            value = draw(st.booleans())
        case "null":
            value = None
        case "step":
            value = low + draw(st.integers(0, 50)) * step + step / 3
            if value > high:
                value = low + step / 3
        case _:
            value = low
    return float(low), float(high), step, kind, value


# Feature: rct-rest-api, Property 10: an invalid write value causes no write transaction
@settings(max_examples=100, deadline=None)
@given(case=_write_case())
def test_invalid_write_value_creates_no_write_transaction(case) -> None:
    low, high, step, kind, value = case
    expected = {"unapproved": 403, "action": 409}.get(kind, 422)

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixtures = write_fixtures(Path(tmp), minimum=low, maximum=high, step=step, approve=kind != "unapproved")
            cfg = make_settings(
                enable_write_support=True, enable_periodic_reads=False, **fixtures
            )  # fmt: skip
            # periodic setup writes to the device in the background and would be counted as the PUT's writes
            async with running_app(cfg) as h:
                endpoint = h.runtime.gateway._devices["main"].endpoint
                before = (h.net.writes, endpoint.counters.transactions)
                path = f"/api/v1/devices/main/metrics/{ACTION_NAME if kind == 'action' else TARGET_NAME}"
                response = await h.client.put(path, headers=_WRITER, json={"value": value})
                assert response.status_code == expected, (case, response.text)
                assert (h.net.writes, endpoint.counters.transactions) == before

    asyncio.run(run())
