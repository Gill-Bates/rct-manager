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


<details markdown="1">
<summary>Previous versions...</summary>

## [1.0.0] - 2026-10-06

- Initial Release
