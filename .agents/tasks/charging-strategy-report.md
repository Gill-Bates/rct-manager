# Charging-Strategy Integration Report

Status on `feature/v1.2.2`, verified against the code on 2026-10-17: the read-only plan route, the
automatic battery-capacity source and the planning service are wired in `app/`; automatic hardware
dispatch is still disabled and **`PlanDispatchHandoff` is still not instantiated in `app/`**
(definition in `app/energy/plan_handoff.py`, constructed only in `tests/test_energy_plan_handoff.py`).
`enable_automatic_dispatch` is `False` and is not in `OPERATOR_FIELDS`. The checklist in section 4
requires real-device verification and operator sign-off; `open-tasks-session.md` lists the remaining
logger and provider probes.

Historical (integration of 2026-10, no longer current): worktree `.worktrees/charging-integrate`,
branch `charging-integrate` (HEAD `c485bad`), base `feature/v1.2.1` (`20ea9ec`). The test numbers in
section 2 belong to that state, not to the current branch.

- **Toolchain at integration time:** Python 3.13.5 · ruff 0.16.5 · node v22.22.2
- **Sources:** this report consolidates `implementation-evidence.md`, `integration-verification.md`,
  and `charging-integrate-review.md` (verdict APPROVED at that time). Those three files no longer
  exist in `.agents/tasks/`; their results survive only as summarized here.

## 1. Strategy summary

The charging strategy is a **plan-only, default-OFF** dynamic battery optimizer. It never commands
hardware in its shipped state; it computes a reasoned proposal that a read-only API serves and that
a separately gated handoff may later offer to the existing Energy Manager.

### What the optimizer decides (`app/optimizer/greedy.py`, not the production optimizer)

> Current-state note: `build_runtime_planning_service` (`app/optimizer/runtime_sources.py`) builds the
> service with `DynamicChargeOptimizer` (`app/optimizer/planner.py`), a dry-run planner that plans grid
> charging into cheap windows only and no discharge, export or arbitrage. `GreedyEnergyOptimizer`
> below is exported from `app.optimizer` but has no production caller, and `cheap_percentile` /
> `expensive_percentile` are consumed only by it. Re-evaluate this section before the handoff is
> wired: the handoff currently maps only explicit grid-charge slots to commands.

`GreedyEnergyOptimizer.plan()` is a pure function of its `PlanningInput` snapshot: it reads no clock
other than `planning_input.now`, does no I/O, and never touches a device. It resamples the three
input series (spot prices, solar forecast, consumption forecast) onto one common **15-minute UTC
grid** (`app/optimizer/align.py`, anchored at the Unix epoch so slots stay exactly 900 s across DST)
and sweeps the grid once, forward in time, choosing exactly one action per slot under a **fixed
total branch precedence (first match wins)**:

1. **PV surplus** — `residual_wh < 0` (solar exceeds load) and SoC below the window top -> `CHARGE`.
2. **Cheap grid window** — slot price `<=` the cheap threshold and SoC below the top -> `CHARGE` from grid.
3. **Expensive grid window** — slot price `>=` the expensive threshold, a non-degenerate curve
   (`cheap < expensive`), and SoC above the reserve floor -> `DISCHARGE` to load.
4. Otherwise `HOLD` (price in the mid band, no actionable surplus), or `IDLE` when the slot has no
   price signal at all (a price gap).

Because branch 1 outranks 2 and 2 outranks 3, overlapping conditions and a degenerate flat price
curve always resolve to exactly one branch; a flat curve (`cheap == expensive`) can never discharge.
Charging fills as early as safely possible and discharging covers load as late as possible, so equal
prices never produce order-dependent output. `EXPORT` is never emitted; `AUTO` is reserved for the
handback path.

**Thresholds** are the cheap/expensive **percentiles** of the horizon's own price distribution
(`cheap_percentile = 25`, `expensive_percentile = 75` by default; cross-validated so cheap < expensive),
computed with `Decimal` arithmetic so a tie against the threshold is exact.

**Bounds and capacity come only from trusted sources, never from hardware registers:**
- SoC window = `[max(7, min_soc), min(97, max_soc)]`, with `TARGET_SOC_MIN_PERCENT = 7` /
  `TARGET_SOC_MAX_PERCENT = 97` from `app/limits.py` intersected with the operator `DispatchConfig`
  window (default 5/95).
- Power = `min(battery limit, MAX_POWER_W)`; `MAX_POWER_W = 50 000 W` from `app/limits.py`.
- Capacity: in production the service resolves it through `resolve_device_battery_facts`
  (`app/dispatch/battery_facts.py`): a persisted expert override first, else the cache-only verified
  automatic source, else unavailable (then no plan). `battery_usable_capacity_wh` is the setting the
  optimizer's own tests and the `resolve_battery_facts` fallback use.

Every `CHARGE`/`DISCHARGE` slot carries a concrete in-window `target_soc_percent` and a
positive-finite `max_power_w`; `HOLD`/`IDLE` carry both as `None`. Every slot carries a bounded
printable-ASCII `reason` (≤ 128 chars). SoC is a hard constraint, re-asserted post-build.

### How the plan is produced and served (`app/optimizer/service.py`)

`PlanningService` runs a single bounded recompute loop
(`await asyncio.wait_for(self._wake.wait(), timeout=interval)` on its **own private** event, never a
shared refresh event). On each tick it re-reads the four inputs — spot prices, solar forecast,
household-consumption forecast, measured battery state. Spot and solar are judged by interval
**coverage** of the plan horizon (not by fetch age), consumption by `consumption_max_age_seconds`,
and the battery provider enforces `battery_max_age_seconds` itself. Only when **all four are present
and fresh/covering** does it build a `PlanningInput`, run the
optimizer, and store the plan. Any stale or missing required input takes the **no-plan path**: the
previous plan expires naturally and nothing is charged on stale data. `valid_until` is tightened to
`min(created_at + plan_ttl, now + horizon, earliest input expiry)`, so a plan is never valid past
the point any input would be stale. The battery state is built from the readings cache (SoC),
static per-device `DeviceLimits` (power bounds), and operator config (capacity); any missing source
yields `None` -> no plan.

### How a decision could reach hardware — and why it does not yet (`app/energy/plan_handoff.py`)

`PlanDispatchHandoff` is the only component that may offer a slot to the Energy Manager, and it is
**OFF by default**. The master switch is the `enable_automatic_dispatch` config flag
(`False` by default, **code-switch-only**, deliberately **excluded from `OPERATOR_FIELDS`**). While
OFF — the shipped state — the handoff computes and **logs** the command it would send but issues no
`command()` call. When enabled, it always calls `EnergyManager.command(..., actor="charging-strategy")`
with a fixed **non-admin** actor, so every existing Energy Manager guard (mode authorization, write
enable, allowlist, capability gate, static limits, SoC re-validation, auto recovery) applies
unchanged — the handoff adds no bypass. It is idempotent: a per-device last-offered intent suppresses
unchanged offers, and on restart (no in-memory state) it reads live device status first and skips a
re-apply when the device already runs the slot's intent, so a restart cannot double-command. Expected
refusal codes are logged and swallowed; unexpected ones propagate.

## 2. Test results — new-vs-pre-existing failure split (historical, integration state `c485bad`)

Current-branch check on 2026-10-17 (no code changed): `pytest tests/test_energy_plan_handoff.py
tests/test_plan_api.py tests/test_dynamic_adapters.py tests/test_optimizer_service.py
tests/test_config_validation.py` -> 133 passed; `pytest tests/test_admin_ui.py` -> 134 passed.

Full-suite rerun on 2026-10-18 (branch `feature/v1.2.2`, HEAD `81f4c45`): `python -m pytest -q -rfE`
-> 2331 passed, 2 xfailed, 0 failed (~9m51s); `node --test tests/js/*.test.js` -> 147 passed,
1 failed. The single JS failure (`Charging plan shows forecast confidence from the plan fields and
explains a weak forecast`, `tests/js/admin-energy-setup.test.js:875`) is a PRE-EXISTING admin-UI
forecast-confidence / `energy-plan-confidence-note` failure outside the charging-strategy/optimizer/
energy/dispatch domain; it belongs to the admin-UI follow-up, was not caused by and is not fixed by
this work. The one Python failure seen earlier
(`tests/test_energy_api.py::test_spot_market_preview_summarises_available_series_when_tomorrow_unpublished`,
`sqlite3.IntegrityError: UNIQUE constraint failed: spot_market_intervals.interval_start`) was a
flaky test caused by wall-clock-dependent fixture construction, not a product defect: when the live
15-minute quarter rounded to 00:30 or 18:00 local, the fixture's `current_start` interval collided
with a static today interval and seeded two rows with the same persisted `interval_start`. Fixed in
the test only by anchoring the fixture and the preview route's `now` to one fixed instant; the
store's `interval_start` PRIMARY KEY (a correct integrity guarantee) and `replace_spot_market_intervals`
were left unchanged.

Pulled from `implementation-evidence.md` and `integration-verification.md` (both tied to HEAD
`c485bad`; working tree clean, so results are reused rather than re-run):

| Check | Command | Result |
|-------|---------|--------|
| ruff check | `ruff check .` | PASS — all checks passed |
| ruff format | `ruff format --check .` | 98 files would reformat — see note below |
| charging-strategy targeted tests | 8 targeted test files | PASS — 105 passed |
| named pre-existing CI failures + structure smoke | 2 named tests + `test_structure_smoke.py` | PASS — both now pass, no architecture rule weakened |
| full Python suite | `python -m pytest -q -rfE` | PASS — 1741 passed, 2 xfailed, 0 failed (~7m54s / ~8m03s) |
| JS suite | `node --test tests/js/*.test.js` | PASS — 79/79 |

**New-vs-pre-existing split:**
- **New test failures introduced by this integration: 0.**
- **Pre-existing test failures: 0.** The full suite is green on both the integration (`c485bad`) and
  the clean baseline (`20ea9ec`). The 2 `xfailed` are declared expected-failures in the suite, not
  errors.
- The only non-green signal is `ruff format --check`, a **pre-existing, repository-wide systemic
  drift** under ruff 0.16.5 (base `20ea9ec` already reports 92–93 files; the integration adds the 6
  charging-strategy files to the same disagreement). It is the documented PY-02 house-style scenario
  in `pyproject.toml` (the committed manual multi-line wrapping disagrees with this formatter version
  across the whole tree). It is **not** a test or correctness failure and was deliberately **not**
  reformatted, to avoid churning house style and to avoid making 6 files inconsistent with 90+ base
  files written the same way. Flagged, not silently changed.

## 3. Remaining assumptions

1. **`PlanDispatchHandoff` is still not instantiated in `app/`** (re-verified 2026-10-17 by grep over
   `app/`). `PlanningService` is built per device by `build_runtime_planning_service`
   (`app/optimizer/runtime_sources.py`) from `start_planning_tasks` in `app/api/app_factory.py`, so the
   read-only plan route is live. The handoff (`app/energy/plan_handoff.py`) is exercised only by
   `tests/test_energy_plan_handoff.py`, which proves that a handoff built with
   `enable_automatic_dispatch=False` issues no `command()`. It was deliberately **not** wired as a
   log-only instance: wiring needs a design decision (the scheduler offers `active_slot(now)`, but nothing
   decides who calls `offer` on which tick), and any wiring is a release-gate change.
   Open question for the owner: wire it log-only (OFF) ahead of the hardware checks, or only together
   with the enable decision.
   NAMED BLOCKER (re-verified 2026-10-18, item left OPEN by decision): the wiring cannot be derived
   unambiguously from existing code, tests or docs, so no architectural decision was invented. Three
   things are undecided and nothing in the repository fixes them. (a) Which tick owns the offer: the
   only plan-domain runtime tick is `PlanningService.run()` -> `recompute()` -> `_recompute_once()`
   (`app/optimizer/service.py`), a plan-PRODUCTION loop that returns the full `EnergyPlan`, not
   `active_slot(now)`; nothing states it should also drive dispatch, nor whether an offer fires
   before or after `set_plan`. `active_slot(now)` is called today only by the read-only plan route
   (`app/api/routers/plan.py`, on HTTP request), which is not a scheduler tick and must not become
   one. (b) Who constructs/owns the handoff: `start_planning_tasks` (`app/api/app_factory.py`) and
   `build_runtime_planning_service` (`app/optimizer/runtime_sources.py`) build a `PlanningService`
   per device but offer no hook, field, or owner for a `PlanDispatchHandoff`, and no `app/` code
   constructs one (constructed only in `tests/test_energy_plan_handoff.py`). (c) The call site and
   device-id source for `offer(device_id, slot)` per tick are therefore undefined. Resolving (a)-(c)
   is a new architectural + release-gate decision for the owner; until then the item stays OPEN. The
   `log-only, default OFF` mandate (`enable_automatic_dispatch` is `bool = False`, not in
   `OPERATOR_FIELDS`) is untouched and was reconfirmed on the current tree on 2026-10-18.
2. ~~Handoff docstring overstates the enable flag.~~ Fixed: the docstring states a plain `bool` captured once.
3. **Battery-state inputs assume the configured or verified values are correct.** Usable capacity (expert
   override or cache-only verified source) and the static per-device power limits are taken as
   declared; the optimizer never cross-checks them against the RCT app or the physical system. A misconfigured capacity would skew the planned SoC trajectory
   (a planning estimate only; the real SoC is re-read every recompute).
4. **Forecast quality is out of scope.** The optimizer trusts the solar/consumption/price snapshots
   it is handed; forecast accuracy is a separate concern not addressed by this integration.

## 4. CHECKLIST — required BEFORE enabling automatic battery commands on real hardware

`enable_automatic_dispatch` is **`False` by default and MUST stay `False`** until every item below is
satisfied and signed off. It is intentionally code-switch-only (not in `OPERATOR_FIELDS`), so it
cannot be flipped from the operator surface.

- [ ] **Wire `PlanDispatchHandoff` into the production assembly** (`PlanningService` is already wired in
      `app_factory`), so the plan -> handoff -> Energy Manager path is exercised at runtime and not only by
      `tests/test_energy_plan_handoff.py` (assumption 1). Code-verifiable, but NOT done — left OPEN as a
      NAMED BLOCKER (triage 2026-10-18): the owner, the tick/when, and the call site cannot be derived
      unambiguously from existing code, tests, or docs, so wiring it would mean inventing an
      architectural decision, which is explicitly out of scope. Concretely undecided:
      (a) which loop owns the `offer` call — the only plan-domain tick is `PlanningService.run()` ->
          `recompute()` -> `_recompute_once()` (`app/optimizer/service.py`), but nothing in code/tests/doc
          assigns the offer to it; `active_slot(now)` is today called only by the read-only plan route
          (`app/api/routers/plan.py`) on HTTP request, which is not a dispatch tick;
      (b) when in that tick to offer (before/after `set_plan`, and whether off the recompute result or a
          fresh `scheduler.active_slot(now)` — `_recompute_once` returns the whole plan, not the active slot);
      (c) who constructs and holds the single `PlanDispatchHandoff` instance and passes the
          `EnergyManagerPort` — no `app/` factory, `Runtime` field, or `start_planning_tasks` hook exists.
      The existing mandate stays intact regardless: `enable_automatic_dispatch` is `bool = False`, absent
      from `OPERATOR_FIELDS`, and any future wiring must be log-only/OFF with the non-admin
      `charging-strategy` actor and no bypass of the Energy Manager gate chain.
- [ ] **Validate operator config against the physical system:** `battery_usable_capacity_wh` and the
      per-device static `DeviceLimits` (max charge/discharge power) match the installed hardware's
      real ratings and safe operating limits (assumption 3).
- [ ] **Confirm the SoC reserve floor and ceiling are safe for the specific battery** — the effective
      window `[max(7, min_soc), min(97, max_soc)]` must respect the manufacturer's limits; adjust the
      operator `DispatchConfig` window if the hardware needs a tighter range. (Hardware/operator.)
- [ ] **Verify input freshness thresholds are conservative for live control:** `spot_max_age`,
      `solar_max_age`, `consumption_max_age`, `battery_max_age_seconds`, `plan_ttl_seconds`, and
      `plan_recompute_interval_seconds` are tuned so a stale input reliably forces the no-plan path
      before any command could act on outdated data. (Code side verified: stale or uncovered inputs take
      the no-plan path, `tests/test_optimizer_service.py`; spot/solar are coverage-based, so
      `spot_max_age_seconds`/`solar_max_age_seconds` only clamp `valid_until`. The "conservative
      enough" judgement is the operator's.)
- [ ] **Exercise the Energy Manager gate chain end-to-end with the `charging-strategy` actor on a
      non-production or isolated device:** confirm mode authorization, write enable, allowlist,
      capability gate, static limits, and SoC re-validation all apply to handoff commands and that
      expected refusals are logged, not retried aggressively.
- [ ] **Verify idempotence and restart safety on real hardware:** a process restart mid-slot reads
      live device status and does not double-command; an unchanged intent across recompute/late data
      is a no-op; a changed intent is applied exactly once.
- [ ] **Confirm no duplicate or conflicting commands** when the plan handoff and any other controller
      path could act on the same device, and that the handoff never drives the admin surface.
- [ ] **Establish monitoring and a kill switch:** live logging/alerting on every issued command and a
      verified, fast way to set `enable_automatic_dispatch` back to `False` and return the device to
      its safe default (`NullLiveChargeLimitProvider` remains the dispatch default).
- [ ] **Re-run the full Python and JS suites plus the targeted charging tests** on the exact build to
      be deployed, and confirm the hard invariants still hold (dispatch OFF until this checklist is
      complete; flag absent from `OPERATOR_FIELDS`; no architecture rule weakened). The invariants hold
      on the current tree (`enable_automatic_dispatch: bool = False` in `app/config.py`, absent from
      `OPERATOR_FIELDS`). Code-side rerun done on this tree (HEAD `81f4c45`, 2026-10-18): full Python
      suite 2331 passed / 2 xfailed / 0 failed; JS 147/148 (the one failure is the pre-existing
      admin-UI forecast-note test, out of scope — see section 2). Box stays unchecked: it must be run
      again on the exact build to be deployed and the rest of this checklist is still open.
- [ ] **Obtain explicit operator/owner sign-off** that automatic hardware dispatch may be enabled,
      recorded against the specific hardware and configuration validated above.

Triage 2026-10-18: no box is checked. The code-side full-suite rerun was done on the working tree of
`feature/v1.2.2` (see section 2: Python 2331 passed / 2 xfailed / 0 failed, JS 147/148 with one
pre-existing admin-UI failure) and the hard invariants were reconfirmed, but item 9 stays OPEN
because it requires the run on the exact deployed build in the operator's release context, not an
agent's working-tree run. Wiring (item 1) stays OPEN as a named blocker (see item 1 above): the
tick, owner, and call site are undecided and an agent must not invent them. All other items need the
real installation, an isolated device, live providers, or the operator. Nothing here may be ticked
by an agent, and the sign-off item cannot be given by one.

Only after every box is checked may `enable_automatic_dispatch` be set to `True` in code for the
validated deployment.
