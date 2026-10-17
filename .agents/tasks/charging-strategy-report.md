# Charging-Strategy Integration Report

Current status on `feature/v1.2.2`: the plan preview and automatic battery-capacity source are
implemented, but automatic hardware dispatch is still disabled. The checklist in section 4
requires real-device verification and operator sign-off; see `open-tasks-session.md` for the
remaining logger and provider probes. Earlier branch/worktree details below are historical.

- **Worktree:** `/opt/rct-manager/.worktrees/charging-integrate` (branch `charging-integrate`, HEAD `c485bad`)
- **Base branch:** `feature/v1.2.1` (tip `20ea9ec` at integration time)
- **Toolchain:** Python 3.13.5 · ruff 0.16.5 · node v22.22.2
- **Sources:** this report consolidates `implementation-evidence.md`, `integration-verification.md`,
  and the independent review `charging-integrate-review.md` (verdict APPROVED, no blocking findings),
  all in this same `.agents/tasks/` directory.

## 1. Strategy summary

The charging strategy is a **plan-only, default-OFF** dynamic battery optimizer. It never commands
hardware in its shipped state; it computes a reasoned proposal that a read-only API serves and that
a separately gated handoff may later offer to the existing Energy Manager.

### What the optimizer decides (`app/optimizer/greedy.py`)

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
- Capacity = `battery_usable_capacity_wh` from operator config.

Every `CHARGE`/`DISCHARGE` slot carries a concrete in-window `target_soc_percent` and a
positive-finite `max_power_w`; `HOLD`/`IDLE` carry both as `None`. Every slot carries a bounded
printable-ASCII `reason` (≤ 128 chars). SoC is a hard constraint, re-asserted post-build.

### How the plan is produced and served (`app/optimizer/service.py`)

`PlanningService` runs a single bounded recompute loop
(`await asyncio.wait_for(self._wake.wait(), timeout=interval)` on its **own private** event, never a
shared refresh event). On each tick it re-reads the four inputs — spot prices, solar forecast,
household-consumption forecast, measured battery state — and checks each against its configured
maximum age. Only when **all four are present and fresh** does it build a `PlanningInput`, run the
optimizer, and store the plan. Any stale or missing required input takes the **no-plan path**: the
previous plan expires naturally and nothing is charged on stale data. `valid_until` is tightened to
`min(created_at + plan_ttl, now + horizon, earliest input expiry)`, so a plan is never valid past
the point any input would be stale. The battery state is built from the readings cache (SoC),
static per-device `DeviceLimits` (power bounds), and operator config (capacity); any missing source
yields `None` -> no plan.

### How a decision could reach hardware — and why it does not yet (`app/optimizer/handoff.py`)

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

## 2. Test results — new-vs-pre-existing failure split

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

1. **`PlanDispatchHandoff` is still not instantiated in `app/`.** `PlanningService` is now built per device
   by `build_runtime_planning_service` in `app/api/app_factory.py` (`start_planning_tasks`), so the read-only
   plan route is live. The handoff (`app/energy/plan_handoff.py`, moved from `app/optimizer/handoff.py`) is
   exercised only by `tests/test_energy_plan_handoff.py`; the runtime path plan -> handoff -> Energy Manager is
   proven by tests, not by app assembly. This is intended while `enable_automatic_dispatch` stays `False`.
2. ~~Handoff docstring overstates the enable flag.~~ Fixed: the docstring states a plain `bool` captured once.
3. **Battery-state inputs assume operator configuration is correct.** Usable capacity and the static
   per-device power limits are taken from operator config as declared; the optimizer never
   cross-checks them against hardware. A misconfigured capacity would skew the planned SoC trajectory
   (a planning estimate only; the real SoC is re-read every recompute).
4. **Forecast quality is out of scope.** The optimizer trusts the solar/consumption/price snapshots
   it is handed; forecast accuracy is a separate concern not addressed by this integration.

## 4. CHECKLIST — required BEFORE enabling automatic battery commands on real hardware

`enable_automatic_dispatch` is **`False` by default and MUST stay `False`** until every item below is
satisfied and signed off. It is intentionally code-switch-only (not in `OPERATOR_FIELDS`), so it
cannot be flipped from the operator surface.

- [ ] **Wire `PlanDispatchHandoff` into the production assembly** (`PlanningService` is already wired in
      `app_factory`), so the plan -> handoff -> Energy Manager path is exercised at runtime and not only by
      `tests/test_energy_plan_handoff.py` (assumption 1).
- [ ] **Validate operator config against the physical system:** `battery_usable_capacity_wh` and the
      per-device static `DeviceLimits` (max charge/discharge power) match the installed hardware's
      real ratings and safe operating limits (assumption 3).
- [ ] **Confirm the SoC reserve floor and ceiling are safe for the specific battery** — the effective
      window `[max(7, min_soc), min(97, max_soc)]` must respect the manufacturer's limits; adjust the
      operator `DispatchConfig` window if the hardware needs a tighter range.
- [ ] **Verify input freshness thresholds are conservative for live control:** `spot_max_age`,
      `solar_max_age`, `consumption_max_age`, `battery_max_age_seconds`, `plan_ttl_seconds`, and
      `plan_recompute_interval_seconds` are tuned so a stale input reliably forces the no-plan path
      before any command could act on outdated data.
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
      complete; flag absent from `OPERATOR_FIELDS`; no architecture rule weakened).
- [ ] **Obtain explicit operator/owner sign-off** that automatic hardware dispatch may be enabled,
      recorded against the specific hardware and configuration validated above.

Only after every box is checked may `enable_automatic_dispatch` be set to `True` in code for the
validated deployment.
