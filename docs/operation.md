# Operation

Running the service in production: what must hold, what to watch, how to back up. Device-specific
verification status is on [Hardware Verification](reference/hardware-verification.md); battery control
is in [Battery Control](energy/battery-control.md).

## Production limits

| Constraint                         | Detail                                                                                                                                                                                                                              |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| One instance per inverter endpoint | Device access is serialized only inside one process with one HTTP worker. Several instances against the same endpoint produce mixed or stale answers and there is no cross-instance lock: run exactly one replica per device group. |
| Exclusive access to port 8899      | See below.                                                                                                                                                                                                                          |
| Plain HTTP                         | TLS terminates at a reverse proxy; see [Network and TLS](configuration/network.md).                                                                                                                                                 |
| Protocol assumptions               | Some are not yet verified against a real inverter; check [Hardware Verification](reference/hardware-verification.md) before production use.                                                                                         |
| Writes                             | Off by default; enabled and approved per parameter in the GUI.                                                                                                                                                                      |

### Exclusive access to port 8899

This service must be the only client of the inverter's port 8899; the vendor app, home automation or
any other RCT client counts as foreign access. Exclusivity is not enforced: a suspicion is flagged
(`foreign_access_suspected` in the readiness response, Prometheus gauge
`rct_transport_foreign_access_suspected`) and the service keeps serving.

### Maintenance state

When the inverter signals its bootloader, sending stops and requests for its devices get 503
`device_maintenance` until a cool-down has passed and a single read succeeds.

## Runtime and restart behaviour

- Most settings autosave in the GUI and take effect without a restart. A changed listen address or
  port is switched in the background; the inverter list and the TSDB export fields need **Apply
  changes**, which rebuilds the connections without a restart.
- Until the initial admin password is changed, the service is idle: periodic reads, heartbeat
  polling, metric collection and the push export stay stopped, and start without a restart after the
  change.
- A service restart hands every running Energy Manager command back to the inverter.
- Write access can be switched off live; it sets every device to mode Off and hands the inverters
  back.
- After adding or removing battery modules, restart the service so the module count is re-read.
- A removed or re-addressed device loses its dispatch state (capabilities `unverified`, engineering
  mode off, Energy Manager mode Off).
- Compose gives the container a one-minute stop grace period; the server runs as PID 1 and receives
  SIGTERM directly. See [Docker](getting-started/docker.md#startup-and-volume-ownership).

## Logs and health

- Logs are text on stdout; Compose rotates them (50 MB, 5 files).
- Access log level: GUI pages, assets, session polls and successful `/metrics` requests are `DEBUG`;
  mutating admin actions (login, password, settings, tokens), `/api/` and `/health` are `INFO`; any
  4xx is `WARNING`, any 5xx `ERROR`. Exception: the Energy page's device poll, whose expected 503
  while write support is off is `DEBUG`.
- The generated admin password appears in clear text in a boxed block on first start only (see
  [Authentication](configuration/authentication.md#administration-login)). Change it at first login.
- `GET /health` is token-free and turns 503 once shutdown has begun; the container health check
  queries it on the container's own address. It is a process check and says nothing about the
  inverter; use `GET /api/v1/readiness` for that.

## Backup and restore

State lives in `data/rct.db` (settings, credentials, tokens, consumption and price history) and, for
battery dispatch, `data/rct-dispatch.db`. Settings and credentials are stored encrypted;
changing `HMAC_SECRET` prevents decryption. Back up database and secret together and
restore them together (see [Environment Variables](configuration/environment.md#admin-database-and-secret)).

The admin database runs in SQLite WAL mode (`synchronous=NORMAL`, 5 s busy timeout), so concurrent
autosaves, sessions and token bookkeeping do not block each other. While the service runs, `rct.db-wal`
and `rct.db-shm` may exist next to `rct.db`; they get the same 0600 permissions and are folded back
into `rct.db` on a clean shutdown.

- Back up while the service is stopped (copy all existing `rct.db*` files together) or online with
  `sqlite3 data/rct.db ".backup backup.db"`. Never copy only `rct.db` of a running service.
- Keep the directory on a local file system. WAL does not work on NFS or SMB shares.
- The directory must be writable (in Docker the `rct-data` volume), because SQLite creates the `-wal`
  and `-shm` files there.
- Keep volume and secret across upgrades.
- An older build rejects a migrated dispatch database; restore a backup to go back
  ([Battery Control](energy/battery-control.md#upgrade-note)).

## Admin dashboard

- **Edit dashboard** opens edit mode on the **Overview** page (move and resize widgets, **Add
  widget**, **Reset layout**, **Done**); changes autosave. On a phone widgets are stacked and the
  layout cannot be edited (the saved layout stays untouched).
- Each inverter card shows the energy-flow graphic (from cached readings, also with write support
  off), a power card and one card per battery tower. A tower's module count comes from its populated
  module serial slots: "Detecting modules..." until all slots were read, "Module layout unclear"
  instead of a drawing for a pattern no documented tower has (for example seven slots, where a Power
  Battery 11.5 has six modules). A tower with N modules is drawn as top cap, N-1 middle parts and
  bottom part.
- The Energy Manager page shows a compact grid, house and battery energy-flow view beside the controls
  when space allows, from measured, sign-normalized readings.
- If the server is unreachable, the dashboard keeps the last data (up to one hour old, dimmed, with a
  banner); a button-less "Connection lost" dialog probes the server and reloads the page once it
  answers.
- The layout is stored in `data/rct.db`, separate from device settings, via `GET`/`PUT`/`DELETE
  /admin/api/dashboard-layout` (admin API, not public contract). The server accepts only known widget
  ids, at most 32 widgets and a 12-column grid.
- The About page checks GitHub for a newer release: results are cached for 1 h, errors for 60 s, and a
  forced refresh runs at most once per 30 s.
- `/admin/static/` answers 404 for any path with a dot-prefixed segment.

## Writes

- Write support is enabled on the GUI **Inverters** page; the `read/write` role and the write
  selection in `data/rct.db` still apply. A fresh database starts with an empty selection (the shipped
  catalog only lists what may be enabled): tick each metric to allow, including action objects such as
  `com_service`.
- "Documented in the catalog" is not "writable out of the box". The destructive `com_service` codes
  `6 erase_parameters_flash` and `11 erase_datalog` are not shipped as writable; codes `0, 5, 9, 10,
  12, 13, 14, 15, 16, 18` are. To allow an erase action, copy the shipped file, add the code to its
  `allowed_values` and use it as the write allowlist (`write_allowlist_path` is not an environment
  setting).
- Numeric limits are wire type limits, not safe operating limits or a promise that the firmware
  accepts a write.
- The **Inverters** page shows a short explanation under a writable parameter where the catalog entry
  has a `help_text`. Most entries document only their vendor object path and intentionally have none
  rather than a guessed one; an own registry file can add `help_text` to any entry (the field is
  optional).
- `wifi_password` is deliberately excluded: secrets are neither readable nor writable, and a registry
  or allowlist that lists it aborts the start.
- Writing `pas_period` can change periodic polling.
- Repeating an action requires resetting it to 0 first.
- Battery dispatch has its own gates: [Battery Control](energy/battery-control.md#safety-gates).
