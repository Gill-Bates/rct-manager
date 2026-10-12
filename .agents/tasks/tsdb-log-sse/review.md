# Real TSDB log tail over a central SSE Event Hub

Replaces the browser-fabricated "Status & Log" console on the TSDB admin page with a genuine
`text/event-stream` live tail of the app's own `LogRecord`s. A domain-agnostic in-process Event Hub
(`app/observability/event_hub.py`) buffers and fans out typed events; a single producer
(`app/observability/log_stream.py`) taps the root logger, filters to TSDB-relevant loggers by name
prefix, and publishes `tsdb.log` events; the admin endpoint `GET /admin/api/events` is a thin SSE
adapter over the hub, guarded by the same `require_admin` the rest of the admin read API uses. The
frontend gains a generic single-`EventSource` dispatcher that feeds the kept dark-terminal card. The
log tap is pure in-process logging — no `docker logs`, no Docker socket, no new capabilities — so the
hardened-container constraints stay intact.

Watch for: a now-dead `tsdbStatusView()` function left behind by the synthetic-console removal
(confirmed) — cosmetic, not a regression. The `_tsdb_view` / REST status path and the status data flow
are untouched (confirmed).

**Verdict**: APPROVED

## High-level view

The transport is split cleanly into three layers that each do one thing: the hub knows how to number,
buffer and fan out events but nothing about logs; the producer owns all the domain knowledge (which
loggers qualify, how a record becomes a payload); the endpoint owns only subscription lifecycle and
wire serialisation. This separation is honoured in the code, not just claimed, which is what makes the
"Phase 2 adds a type with `register_type` + `publish`" path credible.

Bounded-memory and non-blocking guarantees hold at every stage: a per-type ring buffer (500) for
backlog replay, a separate bounded per-subscriber queue (1000), and a `publish` path that never blocks
or raises into the caller — a full subscriber queue drops the oldest event (tail semantics) instead of
back-pressuring the logging call. Delivery is marshalled onto the loop thread via
`call_soon_threadsafe`, so a record emitted on a worker thread is safe.

The auth posture matches the requirement exactly: the endpoint calls `require_admin(request)` with no
weakening and no token-in-URL, so the browser's same-origin `rct_admin_session` cookie gates it like
every other admin GET; an unauthenticated request gets 401. Nothing here is world-readable.

Disconnect handling is real: the SSE generator subscribes inside an `async with`, so a client
disconnect (or app teardown) always runs `unsubscribe`, and a test drives an actual `http.disconnect`
to prove `subscriber_count` returns to zero. Lifespan teardown detaches the root handler and closes the
hub, which matters because the test process builds many app instances.

The frontend removes the fabricated console synthesis and keeps the existing `tsdb-console*` card as
the render target, filled from live events with auto-scroll, a 500-line DOM cap, native `EventSource`
reconnect, and open-on-render / close-on-leave via `pagehide`. The one loose end is a `tsdbStatusView()`
helper that is now defined but never called.

<details>
<summary>Issues (1)</summary>

1. **Dead `tsdbStatusView()`** — the helper is defined in `admin.js` but has no remaining caller after
   the synthetic console was removed; remove it (non-blocking cleanup).

</details>

<details>
<summary>Details</summary>

### Logger-name prefix filter

The prefix filter avoids the obvious trap: `is_tsdb_logger` matches `name == p`
or `name.startswith(p + ".")`, so `app.export.pusher` matches but `app.exporter` and
`app.api.app_factory_helper` do not. The test asserts exactly those non-child near-misses.

### Backlog replay then live, with no gap or duplication

```
publish ──► assign global id ──► append to type ring buffer
                                └► call_soon_threadsafe(_deliver) ─► put_nowait into each subscriber queue

subscribe ─► snapshot = backlog()          (synchronous, no await)
          ─► subscribers.add(sub)          (synchronous, no await)
          ─► yield (snapshot, queue)
```

Because `subscribe` takes the backlog snapshot and registers the subscriber with no `await` between the
two statements, no `_deliver` callback can interleave on the loop thread in that window. An event
published before the snapshot is in the backlog but not delivered live (the subscriber was not yet in
the set when its `_deliver` ran); an event published after registration is delivered live only. So a
reconnecting client sees each retained event once, not twice, and loses nothing in the handover. The
endpoint yields the whole backlog first, then streams the live queue — matching the "snapshot then
deltas" contract.

### Non-blocking fan-out and the drop policy

`_offer` is where the bounded-memory guarantee is enforced: on a full queue, `DROP_OLDEST` discards the
head and enqueues the new event, `DROP_NEWEST` keeps what is queued and discards the incoming event, and
`LATEST_WINS` drains to a single newest event. `tsdb.log` uses `DROP_OLDEST`, which is the right choice
for a terminal tail — a slow browser loses the oldest lines, never the newest activity, and the logging
call is never back-pressured. `publish` returns early if the hub is closed, if no loop is bound, or if
there are no subscribers, and the actual queue mutation happens on the loop thread. The tests overflow a
size-2 queue with five events and confirm both the surviving set (`m3,m4` for drop-oldest; `m0,m1` for
drop-newest) and that the producer never blocked.

### Auth: same gate as the rest of the admin read API

`events()` calls `require_admin(request)` with no arguments, so it is exactly the posture of the other
admin GETs: the same-origin `rct_admin_session` cookie (sent automatically by `EventSource`) authorises,
a valid PAT also passes, and a missing session raises 401. No token is placed in the URL query string,
no new secret is introduced, and the stream is not world-readable. `Last-Event-ID` is read and
deliberately ignored (Phase 1 does no durable replay), which is a defensible scope line and documented
as such. The 401-on-unauthenticated path is covered by a test against the real app.

### Disconnect and lifecycle cleanup

The SSE generator wraps its subscription in `async with hub.subscribe()`, so client disconnect, generator
close, and task cancellation all run `unsubscribe` through the context manager's exit — no leaked queue
or task. The end-to-end disconnect test is stronger than a task cancel: it drives a real `http.disconnect`
over the ASGI protocol and asserts `subscriber_count == 0` afterwards. On the app side, `_lifespan`
detaches the `TsdbLogHandler` from the root logger and calls `hub.close()` in its `finally`, which clears
subscribers, backlogs and the bound loop. This is the right ordering (detach the tap before closing the
hub) and it prevents a root-handler leak bleeding records across the many app instances a test process
constructs.

### Frontend: generic dispatcher, kept card, lifecycle

The `events` dispatcher owns one `EventSource('/admin/api/events')`, opens lazily on the first `on()`,
binds each registered type with `addEventListener`, and closes when the last handler is removed or on
`pagehide`. `pagehide` (rather than a leave-page dialog) is the correct close hook and avoids design rule
R4; it is also the reliable one on mobile Safari. The TSDB console registers an `on('tsdb.log', …)`
handler when its node renders and tears the subscription down on the next render via the returned
unsubscribe, so a replaced node stops receiving and the stream closes once no console is listening —
this is the open-on-show / close-on-leave behaviour the task asked for. `appendTsdbConsoleLine` parses the
payload, renders the kept `tsdb-console-line` / `tsdb-console-time` HH:MM markup via the existing `hhmm()`
helper, caps the DOM at 500 lines dropping the oldest, and auto-scrolls. The synthetic `tsdbConsoleLines`
/ `fillTsdbConsole` and all `syncTsdbStatusSurfaces` calls are gone, so no browser-invented log text
remains.

The `_tsdb_view` dict and the REST status path survive: `tsdbRuntimeStatus` is still populated by
`refreshTsdbRuntimeStatus` (and the `devices` payload at init), and the status data is not rendered as log
output. The leftover is `tsdbStatusView()`: in the base branch its only caller was `tsdbConsoleLines()`,
which this change deleted, so the function is now defined with no caller. It is dead code, not a
regression — the status it computed was exactly the synthetic console content the task required removing.
Removing the function would leave the tree clean.

### Tests and recorded verification

`tests/test_tsdb_log_stream.py` covers the hub and producer in isolation (prefix filter including
non-child near-misses, filtered-reaches / unrelated-dropped, backlog replay, monotonic ids, full-queue
drop-oldest without blocking, the drop-newest branch, ring `maxlen`, the documented `tsdb.log` policy,
and unsubscribe leaving no subscriber while a sibling keeps working) and the endpoint over the real ASGI
protocol (401 unauth, 200 + `text/event-stream`, an `app.export.pusher` record surfacing as a `tsdb.log`
frame while an `app.transport.receiver` record does not, and a real `http.disconnect` leaving no
subscriber). The implementation note and commit record ruff clean on the five touched files, 13 passing
on the new file, the static design-rule / admin-UI / admin-security suites passing, and a full suite of
1204 passed / 2 xfailed with a single pre-existing Hypothesis flake in `test_contract_properties`
(write-transaction contract, unrelated to this change, passing in isolation). Per the task, suites were
not re-run here; the recorded evidence is present and specific, so there is no articulable doubt that
warranted a spot-check.

Not tested: no automated frontend/browser test exercises the `EventSource` reconnect or the DOM-line cap
(the JS is covered only by the static design-rule / asset checks). That is consistent with the repo's
existing approach to `admin.js` and not a new gap introduced by this change.

### Hardened-container and scope

The log source is an in-process `logging.Handler` on records the app already emits to stdout: no
`docker logs`, no Docker socket, no new Linux capabilities, no writable root filesystem. The handler is
additive — `setup_logging` and the stdout handler are untouched. The diff is confined to the six feature
files; no `build/` copy was touched. The two out-of-scope rulings recorded in the implementation note
(the pre-existing `app_factory.py` findings against an old worktree state, and the `running_app()`
dual-`AdminStore` harness weakness) are not part of this diff and correctly do not block it.

</details>

<details>
<summary>File map</summary>

- `app/observability/event_hub.py` (new) — domain-agnostic pub/sub hub: typed events, global id,
  per-type ring buffer, bounded per-subscriber queues, pluggable drop policy, loop-threadsafe publish.
- `app/observability/log_stream.py` (new) — the `tsdb.log` producer: logger-name prefix filter, payload
  builder, type registration, and the root-logger `TsdbLogHandler`.
- `app/admin/api.py` — `GET /admin/api/events`: `require_admin`-guarded `StreamingResponse`, backlog
  replay then live stream, `: ping` heartbeat, unsubscribe on disconnect.
- `app/api/app_factory.py` — lifecycle wiring: build the hub, register `tsdb.log`, bind the loop and
  attach/detach the handler in `_lifespan`, close the hub on teardown.
- `app/admin/static/js/admin.js` — generic `EventSource` dispatcher; synthetic console removed; kept card
  filled from live `tsdb.log` events with auto-scroll, 500-line cap, reconnect, pagehide close; leftover
  dead `tsdbStatusView()`.
- `tests/test_tsdb_log_stream.py` (new) — hub/producer unit tests and SSE endpoint tests over the real
  ASGI protocol.

Full diff: `git -C /opt/rct-manager/.worktrees/tsdb-log-sse diff main...feature/tsdb-log-sse`

</details>
