# Energy Manager

The Energy Manager turns one business decision ("charge to 80 % now") into a battery dispatch
command. The admin GUI page **Energy Manager** and the REST API share one control path, so both
behave identically.

## How it works

Three independent things decide whether a battery command is possible:

| Switch | Where | What it does |
| --- | --- | --- |
| **Write access** | Inverters page (global) | Allows the service to write to inverters at all. It also opens the generic `PUT /api/v1/devices/{id}/metrics` routes, so it stays a deliberate, separate switch: choosing a mode never turns it on. |
| **Mode** per inverter | Energy Manager page | **Off**, **Manual** or **External**: who may command this inverter. |
| **Expert mode** | Energy Manager page | A display switch only: it shows hardware verification, limits and diagnostics. It changes no behaviour. |

Who may call what, per mode:

| Mode | Admin GUI (signed-in session, CSRF, no token) | REST API with a read/write token (`POST …/energy/command`) |
| --- | --- | --- |
| Off | refused, 409 `energy_manager_off` | refused, 409 `energy_manager_off` |
| Manual | allowed: charge, keep idle, discharge, return to automatic | refused, 409 `energy_manager_not_external` (an operator must switch the inverter to External) |
| External | refused, 409 `energy_manager_external` ("Controlled by an external app") | allowed |

Reading the status (`GET …/energy`) works in every mode, with a session or a token. The mode itself
can only be changed in the admin GUI (`PUT /admin/api/energy/devices/{id}/mode`, session and CSRF; a
token gets 403). Changing the mode to Off, or between Manual and External, first hands the inverter
back to automatic operation, so an operation started under one mode never outlives it.

The expert endpoint `POST /api/v1/devices/{id}/battery/dispatch` is independent of the mode: it needs
no mode and is not limited by it. External is therefore **not** the only way to control the battery
through the API; it is the way for an app that should use the business actions (`charge`, `hold`, …)
while the operator keeps the on/off decision. Manual and External share the same prerequisites,
described under [Switching on](#switching-on).

## REST API

Both endpoints need a read/write token and write support, like
[battery dispatch](api/endpoints.md#battery-dispatch). A command is accepted only while the
inverter is in mode **External** (see [How it works](#how-it-works)). The status carries `mode`
(`off`, `manual`, `external`), `accepts_commands_from` (`none`, `admin`, `api`) and the derived
`armed` flag (`true` unless the mode is `off`), kept for compatibility.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/devices/{device_id}/energy` | State, readings, target window, per-action availability |
| POST | `/api/v1/devices/{device_id}/energy/command` | One action: `charge`, `discharge`, `hold`, `auto` |

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"charge","target_soc_percent":80}' \
  http://127.0.0.1:8000/api/v1/devices/main/energy/command
```

| Action | `target_soc_percent` | `max_power_w` | Effect |
| --- | --- | --- | --- |
| `charge` | required | optional | Charges until the target SoC is reached |
| `discharge` | required | optional | Discharges following the house load until the target SoC; cuts discharge immediately on grid export, bypassing deadband and write interval |
| `hold` | omit | omit | Holds the battery at 0 W, grid charging off |
| `auto` | omit | omit | Hands control back to the inverter |

- A body that violates the table is rejected with 422 `invalid_request`.
- `max_power_w` defaults to the limit configured for the inverter and is clamped to it
  (`power_limit_clamped: true`).
- The target must lie inside `target_soc_window` (7 % to 95 %: the product range 7 % to 97 %, narrowed by the fixed
  dispatch SoC bounds 5 % and 95 %); otherwise 422 `value_out_of_range`. It is never clamped.
- Switching "Write access" on the **Inverters** page is live (no restart): off refuses writes, sets every device to mode Off and hands the
  inverters back to automatic operation; on requires choosing a mode again. If a hand-back fails, the response lists
  `write_restore_pending` and the automatic restore retry continues.
- A command lives one hour and is not renewed; the status publishes `until`. A service restart hands
  every running command back to the inverter.
- `auto` replays the captured pre-dispatch registers. If that cannot be confirmed, the answer is 409
  `dispatch_restore_required`; repeating `auto` is the retry.

### Switching on

The Energy Manager is **Off** per inverter until an operator selects **Manual** or **External** in the
admin GUI. The public API cannot change the mode. A command while it is off answers **409
`energy_manager_off`**. Selecting Manual or External needs Write access to be on (otherwise 409
`energy_write_support_required`; the GUI points to the Inverters page), power limits, and the
hardware-verification gate as before.

Selecting Manual or External adds the four dispatch register approvals to the write allowlist if missing (never
removes any), which also opens them to the generic `PUT /api/v1/devices/{id}/metrics`. Selecting Off
hands control back first and removes no approval. If the approvals are revoked on the **Inverters**
page, the actions report `write_not_permitted` and commands answer 409 `energy_action_unavailable`.
A custom write allowlist file must contain `power_mng_soc_strategy`, `power_mng_soc_target_set`,
`power_mng_battery_power_extern` and `power_mng_use_grid_power_enable`.

### Status readings

`readings` carries `battery_soc_percent`, `grid_power_w` (positive = import), `pv_power_w`,
`house_load_w` and `battery_power_w`, each with `age_seconds` and `stale`. They come from the
periodic read cache (periodic interval, fixed at 30 s), never from a device read, and are
`null` with `stale: true` when unavailable. A missing reading never blocks a command.

`battery_power_w` is **measured**: positive = discharging, negative = charging. `commanded_power_w`
and `commanded_direction` are what the last command **requested**.

An action whose hardware capability is unverified reports `available: false` with reason
`hardware_not_verified`; see [Battery dispatch capabilities](operation.md#battery-dispatch-capabilities).

## Admin GUI

The live energy-flow graphic (PV, grid, battery, house) lives on the **dashboard**, animated from the
measured, sign-normalized readings each inverter card already receives; the Energy Manager page is
pure battery control. The dashboard refreshes approximately every 10 s; the Energy Manager page polls every 3 s.

Two orthogonal facts are kept separate on the page:

- **Mode (Off / Manual / External)** — who may command the inverter, a three-state radio group in the
  card header (`PUT …/mode`). Choosing a mode does not by itself charge or discharge the battery. The
  charge, keep idle, discharge and return-to-automatic buttons are enabled in Manual only; in External
  they are disabled and the card says "This inverter is controlled by an external app through the API
  (PAT required)." With Write access off, a hint links to the Inverters page.
- **Current mode** — what the battery is actually doing: Automatic / Charging to X % / Keeping battery
  idle / Discharging to X %, shown as one plain-language sentence (with the measured rate in kW while
  running).

The page is layered by in-page progressive disclosure:

- **Operate** (always shown) — device name and connection, the mode control, the current
  mode sentence, SoC, the actions **Charge battery / Keep battery idle / Discharge battery** with a
  contextual target-SoC slider and a primary button (e.g. "Charge battery to 80 %"), and **Return to
  automatic**. When the battery data is healthy nothing is shown about polling; a stale reading shows
  "Measurements are N seconds old." and a transient failure "Live data unavailable."
- **Setup** (shown only when a prerequisite is missing) — a state-independent readiness checklist
  (Inverter connected / Write access / Power limits / Hardware verification) that opens the editor
  of the first unmet step right below it: a link to the inverter dialog or the **Inverters** page,
  the power-limit form, or — for hardware verification — a **Verify hardware** button (the form
  itself is Expert-only; Basic mode never shows strategy code or byte widths, and Expert mode is
  switched on only by that click or by the switch). "Write access" is met only when
  the four required registers (`power_mng_soc_strategy`, `power_mng_soc_target_set`,
  `power_mng_battery_power_extern`, `power_mng_use_grid_power_enable`) are approved under
  **Writable parameters** on the **Inverters** page; the global write switch alone is not enough.
  The first time **Write access** is switched on, these four registers are approved automatically
  (add-only, applied once). Clearing one later is respected: a later off/on toggle does not approve it
  again, the actions answer `write_not_permitted`, and the checklist step shows it missing. While a
  device is not in mode Off or is dispatching, clearing a required register is refused (HTTP 409), because the
  handback writes it.
  Power limits are shown in **kW**.
- **Expert** (the page-wide **Expert mode** switch next to the heading; off on every page load and
  never stored) — behind a warning: the hardware verification form (re-verify and **Revoke
  verification**), the kW power limits, engineering mode and the SoC target policy. While the Setup
  block shows the power-limit step, that form sits in the Setup block instead; the hardware
  verification form is always in this Expert section.
  Verification is entered by an operator with measured evidence; nothing is verified by default.
- **Diagnostics** (collapsed, inside the Expert section) — the gate table (the only place raw
  `reject_detail` and capability names appear), the capability table, the approved/added write
  names, per-reading age and staleness, and the poll timestamp.

The action **relabels are presentation-only**: the REST action names on the wire are unchanged
(`charge`, `discharge`, `hold`, `auto`); only the GUI strings differ.

If write support (dispatch) is disabled, the Operate poll answers 503 and the page shows "Manual
battery control requires write support to be enabled." instead of the control.

### Hardware verification (admin API)

The verification form uses two session-guarded admin endpoints (CSRF header required, not part of
the public API):

| Method | Path | Effect |
|---|---|---|
| `PUT` | `/admin/api/energy/devices/{device_id}/hardware-verification` | Verifies `write_path_convention`, `battery_power_sign_convention` and `grid_power_sign_convention` in one transaction, all or none. |
| `DELETE` | `/admin/api/energy/devices/{device_id}/hardware-verification` | Revokes the same three; only the status changes, recorded evidence stays. |

The `PUT` body needs `verified_device_model`, `verified_firmware`, a non-empty `note`,
`soc_strategy_external_code` (0-255), `enum_byte_width` and `bool_byte_width` (1-4), and the booleans
`write_frame_layout_verified`, `apply_sequence_verified`, `battery_discharge_positive` and
`grid_import_positive`. Both verification flags must be `true`, otherwise the answer is `400`. A
running operation of the device answers `409 dispatch_capability_conflict`. Both calls return the
admin device status, whose capability rows now also carry the two verification flags.

The `note` documents the write path only. A note already stored on one of the two sign records is
carried over unchanged, because this form has no input field for it.

The hardware verification form covers the write path and both sign conventions only. `export_limit_convention` cannot be set there; it matters only with `limit_export_during_discharge`, which is off by default.

## Hold is unverified

`hold` has not been demonstrated on this unit and ships behind the same per-device capability gate
as every other mode. A verification run must show roughly 0 W battery power within one control
cycle, a setpoint that stays 0 W for the full TTL, a clean `auto` return, and no grid charging.

## Deployment note

The dispatch store adds its Energy Manager tables on first start (schema 3) and the operating mode in schema 4. On the first start a stored `armed=true` becomes mode
`manual` and `armed=false` becomes `off`. An older build rejects the migrated database; restore a backup to go back.
