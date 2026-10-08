## [1.0.1] - 2026-xx-xx

- Energy Manager admin page rebuilt as "Live Flow + Battery Control": animated flow graphic, manual
  charge/hold/discharge/automatic control, Advanced / Diagnostics section (hardware verification, limits).
- `readings.battery_power_w` (measured, discharge-positive) added to the Energy Manager status.
- Removed `PUT /api/v1/devices/{id}/energy/armed`; arming is possible in the admin GUI only.
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
- Switching "Write access" on for the first time approves the four registers Manual battery control needs
  under **Writable parameters** (add-only, once; later off/on toggles never re-approve a register an
  operator cleared). The Energy status carries `required_write_names`.
- Removing a device resets its dispatch capabilities, engineering mode and arming.

<details markdown="1">
<summary>Previous versions...</summary>

## [1.0.0] - 2026-10-06

- Initial Release
