## [1.0.1] - 2026-xx-xx

- Energy Manager admin page: manual charge/hold/discharge/automatic control per inverter, a setup
  checklist that leads to the first missing step, and an **Expert mode** switch for hardware
  verification, power limits, engineering mode, SoC target policy and diagnostics.
- Admin dashboard: every inverter card shows an animated energy-flow graphic (PV, grid, battery, house)
  with state badges, a power card and one card per battery tower with its module count.
- `readings.battery_power_w` (measured, discharge-positive) added to the Energy Manager status.
- Energy Manager operating mode per inverter: `off`, `manual` or `external`, replacing the boolean "armed".
  Manual is operated from the admin GUI (session, no token); External lets a third-party app control the
  battery through `POST /api/v1/devices/{id}/energy/command` with a read/write token. Commands from the
  wrong surface answer 409 (`energy_manager_not_external` for a token in Manual, `energy_manager_external` for
  the GUI in External); `energy_manager_disarmed` is renamed `energy_manager_off`. Choosing a mode does not
  turn on "Write access". The status gains `mode` and `accepts_commands_from`; `armed` stays as the derived
  flag `mode != "off"`, and the action reason `not_armed` is now `mode_off`.
- Removed `PUT /api/v1/devices/{id}/energy/armed` and `PUT /admin/api/energy/devices/{id}/armed`; the mode is set
  in the admin GUI only, through the new session-only `PUT /admin/api/energy/devices/{id}/mode`
  (`{"mode": "off|manual|external"}`). The admin status fields `armed_at`/`armed_by` are now
  `mode_changed_at`/`mode_changed_by`. The expert endpoint `/battery/dispatch` is unchanged and needs no mode.
- Dispatch database schema 4: a stored `armed=true` becomes mode `manual`, `armed=false` becomes `off`
  (one-time migration; no downgrade).
- Upgrade note: the server no longer applies Uvicorn's own forwarded-header handling; `TRUSTED_PROXIES` is the only
  proxy trust list. Behind a TLS-terminating reverse proxy, set `TRUSTED_PROXIES` to the proxy network, otherwise
  administration logins and changes fail with 403 (a startup warning names this case).
- Administration API: token management, `devices`, `enable_write_support` and authentication/trust settings, and
  widening the write allowlist, need a signed-in session; a PAT gets 403 for a real change of these.

- Admin dashboard: configurable GridStack widget layout (edit mode, add widget, reset, autosave),
  stored through `GET`/`PUT`/`DELETE /admin/api/dashboard-layout`.
- Administration API: a PAT also gets 403 for a real change of the export target and credentials
  (`db_type`, InfluxDB/QuestDB host, port, TLS, credentials), `bind_port`, `log_level` and the
  QuestDB retention days.
- Oversized request bodies answer 413 (`invalid_request`) instead of 422.
- `/admin/static/` answers 404 for dot-prefixed path segments.
- Update check: forced refresh at most every 30 s, errors cached 60 s, results 1 h.
- Energy Manager setup: "Write access" requires the four dispatch registers to be approved on the
  Inverters page; `discharge` cuts immediately on grid export.
- QuestDB export: provisioning failures no longer mark the export as failed; DDL runs only when the
  column set changes.
- "Write access" (`enable_write_support`) now takes effect live: no restart. Switching it off refuses writes at
  once, sets every Energy Manager mode to Off and hands every inverter back to automatic operation; switching it on builds
  the battery dispatch if it was not started at boot.
- Switching "Write access" on for the first time approves the four registers Manual battery control needs
  under **Writable parameters** (add-only, once; later off/on toggles never re-approve a register an
  operator cleared). The Energy status carries `required_write_names`.
- Removing a device resets its dispatch capabilities, engineering mode and operating mode.
- Inverter list in the dashboard dialog: edits stay a draft until **Apply changes**; re-addressing or
  removing an inverter asks for confirmation, and a rejected list is rolled back.
- Admin GUI: risky settings (listen address, proxy trust, plaintext export credentials) ask for
  confirmation; a "Connection lost" dialog appears while the server is unreachable, and the dashboard
  keeps showing its last data with a banner.
- A failed automatic battery restore is retried with an exponential backoff (1 s doubling up to 30 s).

<details markdown="1">
<summary>Previous versions...</summary>

## [1.0.0] - 2026-10-06

- Initial Release
