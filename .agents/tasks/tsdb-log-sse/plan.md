# Implementation Plan — Central admin SSE Event Hub + `tsdb.log` live stream (Phase 1)

All work happens in the worktree `/opt/rct-manager/.worktrees/tsdb-log-sse`. Edit only
`app/` sources (ignore any `build/` copy). Use `git -C /opt/rct-manager/.worktrees/tsdb-log-sse`
for git. Branch: `feature/tsdb-log-sse`.

## What this task is

Replace the fake, client-synthesized "Status & Log" console on the TSDB admin page with a REAL
live-tail of TSDB-relevant application logs delivered over SSE. Per the architecture amendment, the
transport is a **single, central, domain-agnostic SSE endpoint** `/admin/api/events` backed by an
in-process **Event Hub**; event *types* carry the semantics. **Phase 1 (this task) builds the hub +
the central endpoint + the frontend connection manager, and wires exactly ONE event type end-to-end:
`tsdb.log`.** Migrating dashboard metrics, `device.status`, `dispatch.status`, `tsdb.status`, job
events, etc. onto the hub is an explicit **Phase 2 follow-up and is OUT OF SCOPE here** — only lay the
correct public interface and prove it with `tsdb.log`.

## Design decisions (resolved by investigation)

**Central SSE endpoint, not a TSDB-specific one.** One endpoint `GET /admin/api/events`
(media_type `text/event-stream`). The browser opens a single `EventSource` and subscribes per type
via `addEventListener("tsdb.log", …)`. No `/admin/api/tsdb/logs/stream`.

**Event Hub — in-process typed pub/sub; SSE is just an adapter over it.** Application producers
publish typed events to the hub; the SSE endpoint subscribes to the hub and serializes events to the
wire. A subscriber holds a bounded `asyncio.Queue`; publishers NEVER block on a slow/absent browser
(`put_nowait`, and on `QueueFull` drop/coalesce per the type's policy). Client disconnect removes the
subscriber — no leaked tasks/queues/handlers. All bounded-memory / non-blocking / disconnect-cleanup
criteria from the original task now attach to **the hub + endpoint**.

**Per-type buffering policy is pluggable.** The hub supports per-type policy so adding a type later
is "publish a new type" with its own policy:
- `tsdb.log` (Phase 1): a small bounded **ring buffer** (`collections.deque(maxlen=N)`, N≈500) whose
  contents are **replayed as backlog** to each new subscriber on connect (the "snapshot"), then live
  deltas follow.
- future types (Phase 2, NOT built now): e.g. metrics = **latest-wins / coalesce** (keep only the
  most recent value per key). The policy interface must make this a drop-in; only `tsdb.log` needs
  backlog replay in Phase 1.

**Log sourcing — in-process logging.Handler, NOT `docker logs`.**
`app/logging_setup.py::setup_logging()` installs exactly one `StreamHandler(sys.stdout)` on the root
logger and sets the root level from `LOG_LEVEL` (via `settings.log_level`). The hardened container
(read-only rootfs, dropped caps, no Docker socket) cannot run `docker logs` or read the daemon
socket. We attach an **additional** read-only `logging.Handler` to the **root** logger that taps the
same `LogRecord`s already going to stdout. It adds a handler; it never replaces or duplicates the
stdout handler (no feedback loop: the handler only reads records, it does not log).
No new capabilities, no writable fs, no Docker socket.

**Server-side log filtering stays mandatory and happens BEFORE the hub.** The handler decides whether
a record becomes a `tsdb.log` event; only matching loggers are published. Never ship all Python logs
to the browser and filter in JS. A record qualifies iff its `record.name` matches one of these
prefixes (documented in code as a module-level tuple):
- `app.export` — covers `app.export.pusher` and `app.export.questdb_admin` (both
  `logging.getLogger(__name__)`, names `app.export.*`) and the rest of `app/export/`.
- `app.api.app_factory` — the metrics-export lifecycle logging (`_export_loop` "Metrics export
  stopped", `_await_password_change` "starting … export"), logger name `app.api.app_factory`.
Prefix match: `record.name == p or record.name.startswith(p + ".")`.
(`app/admin/api.py::_tsdb_view` logs nothing export-specific, so `app.admin.api` is intentionally
excluded; its status stays a separate REST-fed tile, never log output.)

**Auth transport — same-origin session cookie, works with EventSource as-is.** The admin area
authenticates with the `rct_admin_session` cookie (`app/admin/api.py::admin_session` /
`require_admin`); a Bearer PAT is an *alternative*, not a requirement. `require_admin(request)` on a
GET accepts the cookie session and does not run the CSRF check (CSRF is mutation-only via `_csrf`).
Browser `EventSource` sends same-origin cookies automatically, so `/admin/api/events` is guarded by
`require_admin(request)` exactly like the other admin GETs. No token in the URL query string, no new
secret, no new auth mechanism.

**Snapshot + deltas principle (document in code/comments; only `tsdb.log` in scope now).** REST
remains the source of truth for initial snapshots, queries, config and commands; SSE carries live
changes. For the log specifically, the ring-buffer backlog replayed on connect IS the "snapshot".
SSE is not a full state store.

**SSE wire format — named event types with an `id:` sequence, plus heartbeat:**
```
id: 42
event: tsdb.log
data: {"timestamp":"2026-10-11T18:24:10Z","level":"INFO","logger":"app.export.pusher","message":"Exported 42 metrics"}

: ping
```
`timestamp` = `datetime.fromtimestamp(record.created, UTC)` ISO-8601 with `Z`; `message` =
`record.getMessage()`. `id:` is a monotonically increasing per-connection (or per-hub) sequence.
Heartbeat is an SSE comment line `: ping` sent on an idle interval so reverse proxies / idle timeouts
do not drop the connection. The heartbeat lives on this one central endpoint.

**Structure decision:** single coherent feature (hub + endpoint + frontend consumer + tests) — the
frontend is meaningless without the endpoint and vice versa, so it is NOT decomposed into separate
FEATs. The existing implement/review loop runs this plan; the reviewer writes the gate file
`/opt/rct-manager/.worktrees/tsdb-log-sse/.agents/tasks/tsdb-log-sse/review.json` with
`verdict` = `APPROVED`.

## Build / test commands (from docs/development/setup.md)

- Lint: `python -m ruff check .` (ruff 0.16.5; line-length 120; `FAST` + `S` selected).
- Format check (CI runs it): `python -m ruff format --check .`
- Tests: `python -m pytest -q` (`asyncio_mode = "auto"`; existing tests use `@pytest.mark.asyncio` —
  match that).
- Iterate on the new test file: `python -m pytest -q tests/test_admin_events.py`

---

## Plan

- [ ] 1. Create the central Event Hub with pluggable per-type buffering in `app/observability/event_hub.py`.
      Define: (a) an `Event` shape — `type: str`, `data: dict`, and a hub-assigned monotonic `id: int`
      (builder that serializes `data` with `json.dumps(..., ensure_ascii=False)` lives at the SSE
      adapter, not here). (b) A per-type **buffering policy** interface, e.g. `class BufferPolicy`
      with `record(event)` and `snapshot() -> list[Event]`; provide `RingBufferPolicy(maxlen=N)`
      (append-and-replay, used by `tsdb.log`) now, and leave a documented seam for a future
      `LatestWinsPolicy` (coalesce by key) — do NOT implement Phase-2 policies. (c) `EventHub` holding:
      a registry of `type -> BufferPolicy`, a set of subscribers (each a bounded
      `asyncio.Queue(maxsize=M)`, M≈1000), and a monotonic id counter. Methods:
      `register_type(type, policy)`; `publish(type, data)` — assign next id, let the type's policy
      record it, then for each subscriber `put_nowait`; on `asyncio.QueueFull` DROP/coalesce (never
      block, never raise into the caller); `subscribe(types) -> Subscription` yielding
      `(backlog: list[Event], queue)` where backlog is the concatenated `snapshot()` of the requested
      registered types (Phase 1: just `tsdb.log`); `unsubscribe(sub)` removing the queue. Capture the
      running loop at startup so a non-async producer (the logging handler, possibly on another thread)
      can publish via `loop.call_soon_threadsafe`; if no loop is bound, `publish` is a safe no-op.
      Keep `publish` non-blocking and exception-safe.
      Files: app/observability/event_hub.py
      Verify: `python -m pytest -q tests/test_admin_events.py` (added in step 6) hub-unit cases pass;
      `python -m ruff check app/observability/event_hub.py` clean.

- [ ] 2. Create the log→`tsdb.log` capture handler in `app/observability/log_events.py`.
      Define: (a) module-level `TSDB_LOGGER_PREFIXES = ("app.export", "app.api.app_factory")` and an
      `is_tsdb_logger(name)` prefix-match helper (documented). (b) `TSDB_LOG_EVENT = "tsdb.log"`.
      (c) a `log_event_data(record)` builder producing `{"timestamp": <iso-Z>, "level":
      record.levelname, "logger": record.name, "message": record.getMessage()}`. (d) a
      `TsdbLogHandler(logging.Handler)` constructed with a reference to the `EventHub`; its
      `emit(record)` returns early unless `is_tsdb_logger(record.name)`, else builds the data and calls
      `hub.publish("tsdb.log", data)`. It must never raise into logging (guard with try /
      `self.handleError(record)`), must not itself log (no feedback loop), and must do NO blocking work
      (publish is non-blocking by step 1). This keeps server-side filtering strictly before the hub.
      Files: app/observability/log_events.py
      Verify: `python -m pytest -q tests/test_admin_events.py` filter cases pass;
      `python -m ruff check app/observability/log_events.py` clean.

- [ ] 3. Wire the hub + handler into the app lifecycle in `app/api/app_factory.py`.
      In `create_app`: construct one `EventHub`, call `hub.register_type("tsdb.log",
      RingBufferPolicy(maxlen=~500))`, and store it as `app.state.event_hub` (near the other
      `app.state.*` assignments ~line 1179). In the `_lifespan` context manager: on startup bind the
      running loop to the hub (`asyncio.get_running_loop()`, mirroring the existing `app.state.loop`
      assignment ~line 954), instantiate `TsdbLogHandler(hub)`, set its level to `logging.NOTSET`
      (let the root level gate it), and `logging.getLogger().addHandler(handler)`. In the lifespan
      `finally`: `logging.getLogger().removeHandler(handler)` and clear the hub so nothing leaks
      between app instances (CRITICAL: the test suite builds many apps per process; a leaked root
      handler would bleed records across tests). Keep this additive — do NOT touch `setup_logging` or
      the stdout handler.
      Files: app/api/app_factory.py
      Verify: `python -m pytest -q tests/test_admin_security.py` still passes (no cross-test handler
      leak); `python -m ruff check app/api/app_factory.py` clean.

- [ ] 4. Add the central authenticated SSE endpoint in `app/admin/api.py`.
      Add `GET /admin/api/events` on the existing `router` (prefix `/admin/api`, so the full path is
      exactly `/admin/api/events`). Guard it with `require_admin(request)` (cookie session; a valid PAT
      also passes, consistent with the rest of the read API). Return a FastAPI
      `StreamingResponse(generator(), media_type="text/event-stream")` with headers
      `Cache-Control: no-cache`, `X-Accel-Buffering: no` (disable proxy buffering), `Connection:
      keep-alive`. The async generator: `subscribe(["tsdb.log"])` on `request.app.state.event_hub`
      (Phase 1 subscribes to all currently-registered types; `tsdb.log` is the only one), first yield
      each backlog event as an SSE frame, then loop awaiting the subscriber queue with a timeout equal
      to the heartbeat interval (~15s); on an event yield its frame, on timeout yield `: ping\n\n`.
      Frame format: `id: <n>\nevent: <type>\ndata: <json>\n\n` (serialize `data` with
      `json.dumps(..., ensure_ascii=False)`). Wrap the loop so `asyncio.CancelledError` (client
      disconnect) and generator close run `unsubscribe()` in a `finally` — no leaked queue/task. Add
      `from fastapi.responses import StreamingResponse` and `import json` at the top of the module if
      not present. Do NOT stream raw `LogRecord` objects — only the event `data` dict. Do NOT add a
      TSDB-specific route.
      Files: app/admin/api.py
      Verify: `python -m pytest -q tests/test_admin_events.py` auth + MIME + event-type cases pass;
      `python -m ruff check app/admin/api.py` clean.

- [ ] 5. Frontend: central EventSource connection manager + `tsdb.log` listener in `app/admin/static/js/admin.js`.
      Remove the browser-invented log synthesis entirely: delete `tsdbConsoleLines()` and the
      fabricating body of `fillTsdbConsole` (`Target: …` / status / `Last successful export`,
      ~3588–3609) and the `tsdbRuntimeStatus`-derived console content, so NO browser-invented log
      message remains. Keep `tsdbStatusView()` and the separate status TILE / `refreshTsdbRuntimeStatus`
      path intact — that is persisted REST status, shown separately, NOT as log. Keep the console node
      and the `card('console', 'Status & Log', [consoleNode], 'card-span-all')` creation (~3659) and
      its `tsdb-console` / `tsdb-console-line` / `tsdb-console-time` dark-terminal styling with HH:MM
      timestamps. Add a **central connection manager** (one object, created once) that owns a single
      `EventSource('/admin/api/events')`, handles `onerror`/reconnect and connection state, and exposes
      `on(type, handler)` / `off(type, handler)` that register/unregister via
      `source.addEventListener(type, …)` — built so Phase 2 types just register a listener, even though
      Phase 1 only uses `tsdb.log`. When the TSDB console node is rendered (export enabled+configured
      in `renderExportSettings`): ensure the manager is connected and register a `tsdb.log` listener
      that parses `event.data` JSON and appends a `.tsdb-console-line` (`<time
      class="tsdb-console-time">HH:MM</time>` from the event `timestamp` via the existing `hhmm()`
      helper + a `<span>` message, via `element(...)`), auto-scrolls to bottom, and caps retained DOM
      lines (keep last ~500, drop oldest). Unregister the `tsdb.log` listener when the console is
      replaced (re-render / export disabled). Open on TSDB page/card show; close the EventSource
      (`EventSource.close()`) on `pagehide`/`beforeunload` (each admin page is a full page load —
      `page = document.body.dataset.page`; navigating away unloads the page). Do not break
      `_tsdb_view`/`tsdbRuntimeStatus` consumption elsewhere.
      Files: app/admin/static/js/admin.js
      Verify: `python -m pytest -q tests/test_admin_ui.py tests/test_admin_design_rules.py` still pass
      (static-asset / design-rule checks); runtime check in step 7.

- [ ] 6. Add focused backend tests in `tests/test_admin_events.py`.
      Follow the harness in `tests/test_admin_security.py`: `_settings(tmp_path)`, `create_app`,
      `httpx.AsyncClient` + `httpx.ASGITransport`, the `_ready` login/change-password flow for an
      authenticated client, and the autouse `_restore_root_log_level` fixture (copy it; the handler
      touches the root logger). Cover, at minimum:
      (a) unauthenticated `GET /admin/api/events` is rejected (401);
      (b) authenticated request returns `content-type` starting with `text/event-stream`
          (use `client.stream("GET", …)`, assert headers, close promptly);
      (c) a record on an `app.export.pusher` (or `app.export`) logger emerges as a `tsdb.log` event
          (assert via the hub `subscribe()`/backlog or by reading a stream frame with
          `event: tsdb.log`), while a record on an unrelated logger (e.g. `app.transport.receiver`)
          does NOT appear;
      (d) backlog replay: events published before `subscribe()` appear in the backlog a new subscriber
          receives;
      (e) hub-level: `put_nowait` on a FULL subscriber queue drops/coalesces rather than blocking or
          raising; ring buffer respects `maxlen`;
      (f) disconnect cleanup: after `unsubscribe()` (or the subscription context exits) the hub holds
          no subscriber (no leaked queue) and `publish` still works for remaining subscribers.
      Keep the request-level SSE test fast and deterministic (assert headers / first frames; do not
      wait on the heartbeat).
      Files: tests/test_admin_events.py
      Verify: `python -m pytest -q tests/test_admin_events.py` — all new tests pass;
      `python -m ruff check tests/test_admin_events.py` clean.

- [ ] 7. Exercise the endpoint at runtime and capture evidence.
      With an authenticated session (test client or a short worktree script, deleted after): open
      `/admin/api/events`, log a record on `app.export.pusher` (e.g.
      `logging.getLogger("app.export.pusher").info("runtime-evidence %d", 42)`), and confirm an
      `event: tsdb.log` frame with that message arrives; confirm the content type is
      `text/event-stream`; confirm an unauthenticated open is rejected (401). Record the observed
      content-type, a sample `id:`/`event: tsdb.log`/`data:` frame, and the 401 in the feature findings
      and the implementer's step report. Remove any temporary script afterward.
      Files: (temporary verification only; no source change)
      Verify: evidence captured; `python -m pytest -q` (full suite) and
      `python -m ruff check .` + `python -m ruff format --check .` all green.

- [ ] 8. Full regression gate.
      Run the whole suite and linters in the worktree and fix any regression introduced by the change.
      Files: (none — verification step)
      Verify: `python -m ruff check . && python -m ruff format --check . && python -m pytest -q` all
      pass with no new failures.

## Notes / assumptions

- Ring-buffer size (~500), per-subscriber queue size (~1000) and heartbeat interval (~15s) are
  reasonable defaults; the implementer may tune them, but all must stay bounded.
- Threading: `TsdbLogHandler` runs in whatever thread emits the record; the hub hands events to the
  loop via `loop.call_soon_threadsafe` so queue mutation stays on the loop thread. A purely
  thread-safe (lock-guarded) hub polled by the generator is an acceptable alternative — pick one and
  keep `publish` non-blocking and never raising.
- PHASE 2 (OUT OF SCOPE, do not build): migrating dashboard metrics, `device.status`,
  `dispatch.status`, `tsdb.status`, job events onto the hub with their own policies
  (e.g. latest-wins/coalesce). Phase 1 only proves the interface with `tsdb.log`.
- The status TILE and `_tsdb_view` dict are deliberately untouched: persisted status may still be
  shown via REST, but separately from the log card, never as "log".
