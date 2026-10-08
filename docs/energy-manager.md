# Energy Manager

The Energy Manager turns one business decision ("charge to 80 % now") into a battery dispatch
command. The admin GUI page **Energy Manager** and the REST API share one control path, so both
behave identically.

## REST API

Both endpoints need a read/write token and write support, like
[battery dispatch](api/endpoints.md#battery-dispatch).

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
- The target must lie inside `target_soc_window` (7 % to 95 % with the defaults, narrowed by
  `DISPATCH_MIN_SOC` / `DISPATCH_MAX_SOC`); otherwise 422 `value_out_of_range`. It is never clamped.
- Switching "Write access" in Settings is live (no restart): off refuses writes, disarms every device and hands the
  inverters back to automatic operation; on requires arming again. If a hand-back fails, the response lists
  `write_restore_pending` and the automatic restore retry continues.
- A command lives one hour and is not renewed; the status publishes `until`. A service restart hands
  every running command back to the inverter.
- `auto` replays the captured pre-dispatch registers. If that cannot be confirmed, the answer is 409
  `dispatch_restore_required`; repeating `auto` is the retry.

### Switching on

The Energy Manager is off per inverter until an operator switches it on in the admin GUI. The public
API cannot arm it. A command while it is off answers **409 `energy_manager_disarmed`**.

Switching on adds the four dispatch register approvals to the write allowlist if missing (never
removes any), which also opens them to the generic `PUT /api/v1/devices/{id}/metrics`. Switching off
hands control back first and removes no approval. If the approvals are revoked on the **Inverters**
page, the actions report `write_not_permitted` and commands answer 409 `energy_action_unavailable`.
A custom `WRITE_ALLOWLIST_PATH` must contain `power_mng_soc_strategy`, `power_mng_soc_target_set`,
`power_mng_battery_power_extern` and `power_mng_use_grid_power_enable`.

### Status readings

`readings` carries `battery_soc_percent`, `grid_power_w` (positive = import), `pv_power_w`,
`house_load_w` and `battery_power_w`, each with `age_seconds` and `stale`. They come from the
periodic read cache (`PERIODIC_INTERVAL_SECONDS`, default 30 s), never from a device read, and are
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

- **Manual control (Enabled / Disabled)** — whether manual commands are *allowed*. Enabling does not
  by itself charge or discharge the battery; it is the arm toggle (`PUT …/armed`).
- **Current mode** — what the battery is actually doing: Automatic / Charging to X % / Keeping battery
  idle / Discharging to X %, shown as one plain-language sentence (with the measured rate in kW while
  running).

The page is layered by in-page progressive disclosure:

- **Operate** (always shown) — device name and connection, the Manual-control toggle, the current
  mode sentence, SoC, the actions **Charge battery / Keep battery idle / Discharge battery** with a
  contextual target-SoC slider and a primary button (e.g. "Charge battery to 80 %"), and **Return to
  automatic**. When the battery data is healthy nothing is shown about polling; a stale reading shows
  "Measurements are N seconds old." and a transient failure "Live data unavailable."
- **Setup** (shown only when a prerequisite is missing) — a state-independent readiness checklist
  (Inverter connected / Write access / Power limits configured / Hardware control verified)
  with a single call-to-action that routes to the first unmet step. "Write access" is met only when
  the four required registers (`power_mng_soc_strategy`, `power_mng_soc_target_set`,
  `power_mng_battery_power_extern`, `power_mng_use_grid_power_enable`) are approved under
  **Writable parameters** on the **Inverters** page; the global write switch alone is not enough.
  The first time **Write access** is switched on, these four registers are approved automatically
  (add-only, applied once). Clearing one later is respected: a later off/on toggle does not approve it
  again, the actions answer `write_not_permitted`, and the checklist step shows it missing. While a
  device is armed or dispatching, clearing a required register is refused (HTTP 409), because the
  handback writes it.
  Power limits are shown in **kW**.
- **Expert** (collapsed) — the hardware verification form, engineering mode and the kW power limits
  behind a warning, and the SoC target policy. Verification is entered by an operator with measured
  evidence; nothing is verified by default.
- **Diagnostics** (collapsed) — the gate table (the only place raw `reject_detail` and capability
  register names appear), the capability table, the approved/added write names, per-reading age and
  staleness, and the poll timestamp.

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

The dispatch store adds its Energy Manager tables on first start (schema 3). An older build rejects
that database; restore a backup to go back.
