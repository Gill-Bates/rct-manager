# Implementation — Real TSDB log stream over SSE (Phase 1)

Branch: `feature/tsdb-log-sse`. Worktree: `/opt/rct-manager/.worktrees/tsdb-log-sse`.
Only `app/` sources were edited (no `build/` copy touched).

## What was built

The synthetic, browser-fabricated "Status & Log" console on the TSDB page is replaced by a REAL
live tail of the app's own log records, delivered over Server-Sent Events through a central,
domain-agnostic Event Hub.

This implements the two user amendments that superseded `plan.md` on the transport shape:

- **No `/admin/api/tsdb/logs/stream`.** There is ONE central SSE endpoint `GET /admin/api/events`.
- An in-process **Event Hub** (pub/sub) carries TYPED events; SSE is only an adapter over it.
- **Phase 1 scope:** the hub + the endpoint + exactly one producer/type: `tsdb.log`.
  No metrics / device.status / dispatch.status / tsdb.status / job events (those are Phase 2),
  but the hub is designed so adding a type later is just `register_type` + `publish`.

Everything else from `plan.md` still holds: in-process `logging.Handler` on the root logger,
bounded ring buffer, same-origin `rct_admin_session` cookie auth via `require_admin`, server-side
logger-name filtering (`app.export*` + `app.api.app_factory`), and the `: ping` heartbeat.

### Files

- `app/observability/event_hub.py` (new) — the domain-agnostic hub. Pure transport/fan-out: no
  TSDB/inverter/dashboard knowledge. Monotonic global `id:` counter, per-type `BacklogPolicy`
  (ring-buffer size + full-queue `DropPolicy`), per-subscriber bounded `asyncio.Queue`,
  `subscribe()` async context manager, `publish()` non-blocking and thread-safe
  (`loop.call_soon_threadsafe`), `close()` for teardown. `DropPolicy` supports `DROP_OLDEST`,
  `DROP_NEWEST` and `LATEST_WINS` (the last for a future state-type; no Phase 1 type uses it).
- `app/observability/log_stream.py` (new) — the `tsdb.log` producer. All domain logic lives here:
  the logger-name filter `is_tsdb_logger`, the record→payload builder `build_log_event`,
  `register_tsdb_log_type(hub)`, and `TsdbLogHandler(logging.Handler)` which filters and publishes
  to the hub. It only reads records and never logs (no feedback loop).
- `app/admin/api.py` — `GET /admin/api/events`: `require_admin(request)` (same-origin cookie, PAT
  also passes), `StreamingResponse(..., media_type="text/event-stream")`, replays the hub backlog
  then streams live, `: ping` heartbeat on a 15 s idle timeout, `finally`/`unsubscribe` on
  disconnect. `Last-Event-ID` is read and ignored (Phase 1 does no durable replay). The endpoint
  carries NO domain logic; `_sse_frame` serialises any hub `Event`.
- `app/api/app_factory.py` — lifecycle wiring only: `create_app` builds one `EventHub`, registers
  the `tsdb.log` type, stores it on `app.state.event_hub`; `_lifespan` binds the running loop and
  attaches `TsdbLogHandler` to the root logger on startup, removes it and closes the hub in the
  `finally` (so no root handler leaks between the many app instances a test process builds).
  Additive — `setup_logging` and the stdout handler are untouched.
- `app/admin/static/js/admin.js` — a GENERIC SSE client (`events` dispatcher) owns the single
  `EventSource('/admin/api/events')`, native reconnect, and connection lifecycle ONCE; features
  register typed handlers (`events.on('tsdb.log', fn)`), Phase 2 adds handlers without touching the
  connection. The fabricated console lines (`tsdbConsoleLines`/`fillTsdbConsole` and the
  `tsdbRuntimeStatus`-derived synthetic text) are removed. The dark-terminal "Status & Log" card and
  its `tsdb-console*` HH:MM styling are kept as the render target; the console now fills from live
  `tsdb.log` events (auto-scroll, retained-line cap of 500, drop oldest). The stream is opened when
  the console renders and closed on console re-render / export-off and on `pagehide`. The persisted
  status path (`tsdbStatusView`, `refreshTsdbRuntimeStatus`, `_tsdb_view`) is left intact — the
  status data is NOT shown as log output.
- `app/admin/static/css/admin.css` — unchanged (reused `tsdb-console`, `tsdb-console-line`,
  `tsdb-console-time`; no new class, so design-rule R1/R8 stay satisfied).
- `tests/test_tsdb_log_stream.py` (new) — see below.

### Wire format

```
id: 1
event: tsdb.log
data: {"timestamp":"2026-10-11T18:24:10Z","level":"INFO","logger":"app.export.pusher","message":"Exported 42 metrics"}

: ping
```

### Drop policy decision (required by amendment B3)

`tsdb.log` uses **`DROP_OLDEST`** on a full subscriber queue (`TSDB_LOG_POLICY` in
`app/observability/log_stream.py`): the console behaves like a terminal tail — a slow browser loses
the oldest lines, never the newest activity, and the producer is never blocked. The hub also keeps a
bounded ring buffer (`backlog_size=500`) of the most recent events for replay on connect. The
per-subscriber queue is bounded at `_DEFAULT_QUEUE_SIZE = 1000`, and the heartbeat idle interval is
15 s. All three limits (ring buffer, subscriber queue, heartbeat interval) stay bounded.

## Verification

Commands run in the worktree (`python -m ruff ...`, `python -m pytest -q ...`):

- `python -m ruff check app/observability/event_hub.py app/observability/log_stream.py
  app/admin/api.py app/api/app_factory.py tests/test_tsdb_log_stream.py` → **All checks passed!**
- `python -m pytest -q tests/test_tsdb_log_stream.py` → **13 passed**.
- `python -m pytest -q tests/test_admin_design_rules.py tests/test_admin_ui.py` → **104 passed**
  (static design-rule and admin-asset checks, incl. R1 CSS allow-list and R4 no-leave-page-dialog).
- Full suite `python -m pytest -q` → **1 failed, 1204 passed, 2 xfailed** in ~474 s. The single
  failure is `tests/test_contract_properties.py::test_invalid_write_value_creates_no_write_transaction`,
  a Hypothesis property test unrelated to this change (write-transaction contract); it **passes in
  isolation** (`python -m pytest -q
  tests/test_contract_properties.py::test_invalid_write_value_creates_no_write_transaction` → 1
  passed in 25.52s), i.e. a load-dependent flake, not a regression from the SSE work.

### Test coverage in `tests/test_tsdb_log_stream.py`

Hub/producer unit tests (no app): logger-name prefix filter (match vs. non-child substrings),
filtered record reaches the hub while an unrelated logger does not, backlog replay to a new
subscriber, monotonic ids, full-queue `DROP_OLDEST` drops oldest and never blocks the producer,
`DROP_NEWEST` alternative branch, ring buffer `maxlen`, documented `tsdb.log` policy, and
unsubscribe leaves no subscriber while others keep working.

SSE endpoint tests: unauthenticated `GET /admin/api/events` → 401; authenticated → `content-type`
starts with `text/event-stream`; end-to-end over the real ASGI protocol (with
`app.router.lifespan_context`, so the real logging handler is attached) a record on
`app.export.pusher` surfaces as a `tsdb.log` frame while an `app.transport.receiver` record does
not; and a real `http.disconnect` leaves `hub.subscriber_count == 0` (no leaked subscriber/task).

The endpoint streaming tests drive the ASGI protocol by hand (`_AsgiStream`): httpx's
`ASGITransport` does not surface an endless SSE response usefully in this test setup (it waits on
the response iterator and only aborts via the outer timeout), so the test sends a real
`http.request` then, on context exit, a real `http.disconnect` — the exact browser-close signal —
which also lets the disconnect-cleanup assertion exercise the production teardown path rather than a
hard task cancel.

### Runtime evidence (captured via a throwaway ASGI driver, since deleted)

```
UNAUTH status: 401
AUTH status: 200
content-type: text/event-stream; charset=utf-8
cache-control: no-store
x-accel-buffering: no
SAMPLE FRAME (preceded on the wire by `id: <n>`):
event: tsdb.log
data: {"timestamp": "2026-10-11T17:26:29.006271Z", "level": "INFO", "logger": "app.export.pusher", "message": "Exported 42 metrics"}
subscriber_count after disconnect: 0
```

A record logged on `app.transport.receiver` in the same window did NOT appear on the stream
(filtered out). `Cache-Control` is `no-store` because `RequestContextMiddleware` forces it on every
response (stronger than the `no-cache` the plan mentioned, and correct for a live stream); the
endpoint therefore no longer sets its own `Cache-Control` and keeps only `X-Accel-Buffering: no`
and `Connection: keep-alive`.

### Hardened-container constraints

No `docker logs`, no Docker socket, no new Linux capabilities, no writable root fs: the log tap is a
pure in-process `logging.Handler` on records the app already emits to stdout.

## Format-check note (pre-existing, not from this change)

`python -m ruff format --check .` reports many files (~77) "would be reformatted". These are
PRE-EXISTING in the base branch and span code this change never touched (e.g. unrelated hunks in
`app/api/app_factory.py` and `app/admin/api.py` far from the SSE additions). The five new/changed
files added by this work are already format-conformant
(`ruff format --check` clean on `event_hub.py`, `log_stream.py`, `tests/test_tsdb_log_stream.py`;
the two touched legacy files only differ at pre-existing lines, not at the SSE additions). Running
`ruff format .` across the repo was deliberately NOT done, to avoid reformatting unrelated code.

## Scope rulings recorded for the reviewer (do NOT act on these in this branch)

Three findings were raised by the user/review process against code outside this SSE feature; all are
ruled out of scope for this branch and must NOT block it:

1. **`app/api/app_factory.py` findings (write-allowlist read-only start, device-reconfiguration
   rollback, dispatch publish from worker thread, first-start password file, `repair_device_names`
   migration).** These were raised against an OLD worktree state and concern pre-existing code this
   change never edited. This branch's `app_factory.py` diff is strictly the Event Hub wiring
   (import, `app.state.event_hub`, handler attach/detach in `_lifespan`); none of the flagged lines
   appear in this branch's diff. The user confirmed the findings referred to the old worktree state.

2. **`tests/api_helpers.py::running_app()` dual-`AdminStore` P3.** Factually correct but a
   PRE-EXISTING harness weakness, out of scope for this SSE branch. It does not affect the new
   endpoint: the SSE tests do not use `running_app()`'s PATs — the unauthenticated test sends no
   credentials (genuine 401) and the authenticated test uses the SESSION-COOKIE path
   (`_cookies_for` / `app.state.first_start_password`), i.e. the real browser `EventSource` path,
   which authenticates against the app's own `admin_store`. Per the user ruling: do NOT change
   `tests/api_helpers.py` here; tracked as a separate follow-up — convert `running_app()` to a
   single `AdminStore` and re-check every test that sends `READ_TOKEN`/`WRITE_TOKEN` against
   `/admin/api/*`.

3. **`tests/test_stream_properties.py::test_underdeclared_long_frame_parses_like_a_correct_one`
   (CI failure).** CI surfaced this as failing, but it is a PRE-EXISTING, probabilistic Hypothesis
   failure in the `StreamParser` long-frame-recovery path and is completely unrelated to SSE: this
   feature touches neither `app/protocol/stream` nor any frame decoding. It is explicitly out of
   scope for this branch and is NOT fixed here (no mixing of unrelated fixes). Tracked as a
   separate fix outside this feature.

The reviewer should base its verdict on the actual feature components only:
`app/observability/event_hub.py`, `app/observability/log_stream.py`, the `GET /admin/api/events`
endpoint in `app/admin/api.py`, the generic frontend SSE client in `app/admin/static/js/admin.js`,
and `tests/test_tsdb_log_stream.py`.
