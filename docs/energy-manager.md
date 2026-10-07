# Energy Manager

The Energy Manager turns one business decision — "charge to 80 % now" — into a battery dispatch
command, and it is the surface a tariff-driven automation will use later. This page describes the
API. The admin GUI page **Energy Manager** (live energy flow plus manual control) uses the same
control path: `EnergyManager.command()` and nothing else.

Two endpoints serve it, all under the same `read/write` token and the same write-support switch as
[battery dispatch](api/endpoints.md#battery-dispatch):

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/devices/{device_id}/energy` | State, readings, effective target window and per-action availability |
| POST | `/api/v1/devices/{device_id}/energy/command` | One action: `charge`, `discharge`, `hold`, `auto` |

## The four actions

| Action | `target_soc_percent` | `max_power_w` | What it does |
| --- | --- | --- | --- |
| `charge` | required | optional | Charges towards the target SoC and stops when it is reached |
| `discharge` | required | optional | Discharges following the house load and stops at the target SoC |
| `hold` | must be omitted | must be omitted | Holds the battery at 0 W under our control, grid charging off |
| `auto` | must be omitted | must be omitted | Hands control back to the inverter's own strategy |

`charge` and `discharge` need a target because the target is the stop condition — without it the
command has no end. A body that carries a target for `hold` or `auto`, or omits it for `charge` or
`discharge`, is rejected with 422 `invalid_request` instead of being quietly corrected.

`max_power_w` is optional. Omitted, the service uses the limit configured for that inverter
(`max_charge_power_w` or `max_discharge_power_w`, whichever the direction needs). An explicit value
is an upper bound and is clamped down to that configured limit; the status then reports
`power_limit_clamped: true`, so a clamp is visible rather than silent.

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"charge","target_soc_percent":80}' \
  http://127.0.0.1:8000/api/v1/devices/main/energy/command
```

### Why `hold` and `auto` differ

They look similar from the outside and are opposites underneath:

- `hold` keeps the inverter under our control and pins the battery at 0 W with grid charging off. It
  is a managed command with a TTL, and the status reports it as a running operation.
- `auto` ends our control. The captured pre-dispatch register snapshot is replayed and the inverter
  resumes its own strategy. If the replay cannot be confirmed, the command answers 409
  `dispatch_restore_required` and the record stays in its restore-pending state instead of reporting
  a handback that did not happen. Repeating `auto` is the retry.

### Command TTL

A command carries a time-to-live of one hour. When it expires, the service restores the inverter and
the battery returns to its own strategy. **Stage 1 does not renew a command automatically** — the
status publishes `until`, and a caller that wants a longer action re-issues it. Renewal is
automation and is deliberately out of scope here.

**A service restart ends a hold and returns the inverter to automatic operation.** On start-up the
dispatch controller hands back every record that is not cleanly idle, so a hold never outlives the
process, no matter how much of its TTL was left.

## Arming the Energy Manager

The Energy Manager is off per inverter until an operator switches it on in the admin GUI
(**Energy Manager ON/OFF**). Arming is deliberately **not** part of the public API: a token cannot
switch it on. A command to a device that is not armed answers 409 `energy_manager_disarmed`; arming
while write support is disabled answers 409 `energy_write_support_required` in the GUI.

Arming is additive and never destructive:

- it **adds** the four dispatch register approvals to the inverter's write selection if they are
  missing, in the same way the **Inverters** page would,
- it never resets, reorders or removes anything an operator selected,
- disarming removes **no** approval. An operator who wants the approvals gone revokes them on the
  **Inverters** page.

!!! warning "Arming widens the generic write allowlist"
    The four dispatch registers are written to the global admin write allowlist, so arming in the admin GUI
    also extends the generic
    `PUT /api/v1/devices/{id}/metrics` for every write token. Disarming does not revert this.

Disarming hands control back first. While the device is still controlled and the handback fails, the
device stays armed and the call answers 409 `dispatch_restore_required`.

Revoking the four register approvals on the **Inverters** page disables the actions on an armed
device: every action then reports `available: false` with reason `write_not_permitted`, and a command
answers 409 `energy_action_unavailable`.

!!! note "`armed: false` with a live state is not a bug"
    The expert endpoint `POST /api/v1/devices/{device_id}/battery/dispatch` is **not** gated by the
    armed switch, so a dispatch started there runs while the Energy Manager reads `armed: false` for
    the same device. The Energy Manager status still projects that state truthfully. This is
    intended: the armed switch guards the Energy Manager's own actions, not the expert API.

## Target SoC: business goal and device derivation

`target_soc_percent` on this API is the **business stop goal** — what an operator or a tariff engine
means by "charge to 80 %". It is deliberately not the same number as the device's SoC-target
register. How a device derives that register value from the business goal is a per-device policy on
the session-authenticated admin surface
(`GET`/`PUT /admin/api/energy/devices/{device_id}/soc-target-policy`). The shipped policy,
`business_target`, writes the business goal as-is and therefore reproduces today's behaviour exactly.
It is an assumption, not a measurement.

### Effective target window

A target must sit inside the product's hard bounds of **7 % to 97 %**, narrowed by the configured
`DISPATCH_MIN_SOC` and `DISPATCH_MAX_SOC` — with the defaults (5 % / 95 %) the effective window is
**7 % to 95 %**. The window travels on every status as `target_soc_window`, so a caller can read what
it has to stay inside. A target outside the window is rejected with 422 `value_out_of_range`; it is
**never silently clamped**, because moving the goal would change the decision.

## Readings on the status

The status carries battery SoC, grid power (positive = import), PV power and house load as advisory
figures with `age_seconds` and `stale`. They come from the periodic read cache, never from a device
read triggered by the status call. With `ENABLE_PERIODIC_READS` off — or before the first cycle has
run — they read as `null` with `stale: true`, and the actions keep working regardless. A broken
reading never blocks a command, in particular never the `auto` an operator needs.

## Deployment notes

- **A schema-3 dispatch database cannot be downgraded.** The dispatch store adds its Energy Manager
  tables on first start and bumps the schema version; an older build rejects that database. If you
  need to go back, restore the database from a backup.
- **A custom `WRITE_ALLOWLIST_PATH` must contain the four dispatch registers**
  (`power_mng_soc_strategy`, `power_mng_soc_target_set`, `power_mng_battery_power_extern`,
  `power_mng_use_grid_power_enable`). Arming refuses with 409 `energy_write_support_required` and
  names the missing entries if the configured allowlist cannot offer them.
- Capability verification works exactly as for battery dispatch; see
  [Battery dispatch capabilities](operation.md#battery-dispatch-capabilities). An action whose
  required capability is unverified reports `available: false` with reason `hardware_not_verified`,
  unless the device's engineering mode releases it — in which case the status reports
  `time_limited: true`.

## Unverified on this hardware

Nothing on this page is a hardware-verified fact, and no figure here is a measurement. Two
hypotheses are open and shape what the service does today.

### `hold` is unverified

**`HOLD` has not been demonstrated on this unit.** No run in the repository shows the hold state, so
it ships behind the same per-device capability gate as every other mode and should not be treated as
proven in production. A verification run would have to show all four of:

1. the apply reaches roughly 0 W battery power within one control cycle,
2. the setpoint is still 0 W after the full TTL without a re-write,
3. `auto` returns the unit to the captured snapshot values,
4. no grid charging occurs during the hold.

### The SoC-target derivation is unverified

External reports suggest an RCT under external power control may fall back to a trickle charge
unless its SoC-target register sits *below* the current SoC. That is not verified on this unit, and
the one documented charge run here is equally consistent with a sign-convention problem on the
battery power register. Resolving it needs a run that, on one device with a short TTL and polling
from the first second:

1. issues a charge with a target clearly above the current SoC under `business_target` and records
   **measured** battery power and the SoC trend,
2. repeats it under `below_current_soc`,
3. compares both against the roughly 100 W trickle threshold.

Full power under one policy and a trickle under the other would confirm the hypothesis for that
device and firmware; charge power measured with the opposite sign points at the sign convention
instead, which is a capability matter and not a policy matter. Until such a run exists,
`business_target` stands as the shipped assumption and nothing is marked verified.

## Readings: measured versus commanded

`readings.battery_power_w` is the **measured** battery power in business convention: positive means
the battery discharges, negative means it charges, `0` means it rests. `commanded_power_w` and
`commanded_direction` are what the last command **requested**; the two differ while a command
ramps up or when the inverter limits itself. The measured figures come from the cache only; how
fresh they are is bounded by the periodic read interval (`PERIODIC_INTERVAL_SECONDS`, default 30 s).
A per-metric faster interval is not supported by the polling architecture.

## Admin GUI

The page shows a live flow graphic (PV, grid, battery, house) animated exclusively from measured
values (dot speed proportional to power, direction from the sign, grey and still below 20 W, static
under `prefers-reduced-motion`) and the manual controls. The browser polls
`/admin/api/energy/devices` every 3 s; that endpoint reads the cache only. Gate detail, SoC target
policy, power limits, engineering mode and the hardware verification form (the three capabilities
`write_path_convention`, `battery_power_sign_convention`, `grid_power_sign_convention`) sit in the
collapsed **Advanced / Diagnostics** section. A verification is entered by an operator with the
evidence measured on the device; nothing is verified by default.
