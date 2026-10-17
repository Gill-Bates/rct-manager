# Configuration

Copy `settings.env.example` to `settings.env`. It holds only the start parameters `HMAC_SECRET`,
`BIND_ADDRESS`, `BIND_PORT`, `ALLOW_NON_LOOPBACK_BIND` and optionally `ADMIN_DB_PATH`. All other
settings are managed in the admin GUI and persisted in `data/rct.db`; the
[optional environment variables](../configuration/environment.md) only seed the very first start.
Internal tuning (timeouts, retries, cache, limits, intervals) is fixed in code.

Changes autosave and show a toast and take effect on their own; a changed listen address or port
is switched in the background and the page reconnects. Only the inverter list and the TSDB export
fields need **Apply changes** (see [Devices](#devices)); the TSDB **Enable export** switch applies
immediately. Leaving a page with unapplied changes discards them without a warning.

## Secret and database

`HMAC_SECRET` encrypts the admin database `data/rct.db` (default location; `ADMIN_DB_PATH` overrides
it). A local start generates the secret; Docker requires it up front. Back up database and secret
together. Details: [Environment Variables](../configuration/environment.md#admin-database-and-secret),
backup and WAL rules: [Operation](../operation.md#backup-and-restore).

The first start prints the generated `admin` password, see
[Authentication](../configuration/authentication.md#administration-login).

## Device and Network

### Devices
Add inverters on the **Overview** dashboard with **Add inverter** in the Inverters card. The modal editor stores devices encrypted in `data/rct.db`.

- Edits stay a draft until **Apply changes**, which rebuilds the connections without a restart; **Discard** drops them. A list the server rejects is rolled back.
- Re-addressing or removing an inverter resets its verification evidence, engineering mode and Energy Manager mode (back to Off) and asks for confirmation.
- A device is an IP address or host name plus a port. The service assigns the id (`main`, then `inverter-2`, `inverter-3`, ...) and keeps it while the device exists, so API paths and exports stay stable. Editing the host keeps the id; duplicate addresses are rejected.
- Display name precedence: saved name, inverter-reported name, device id.
- A `DEVICES` environment variable is ignored (INFO note at startup).

### Listener, reverse proxy and trusted proxies
Defaults: `BIND_ADDRESS=127.0.0.1`, `ALLOW_NON_LOOPBACK_BIND=false`, `BIND_PORT=8000`. A non-loopback
address requires `ALLOW_NON_LOOPBACK_BIND=true`, which enables neither TLS nor secure cookies: put a
TLS-terminating reverse proxy in front and enable the GUI option "behind reverse proxy". Behind a
proxy set `TRUSTED_PROXIES` and `FORWARDED_HEADER`. In a container the GUI port moves only the
internal listener. Details: [Network and TLS](../configuration/network.md).

## Authentication and Tokens

### Authentication and tokens
GUI only: **Require authentication** (default on) and the personal access tokens (`pat_...`) live in the admin database. `AUTH_REQUIRED`, `API_TOKENS` and `API_TOKENS_FILE` are ignored with a warning. Changing settings always needs an admin session or a `read/write` token, regardless of the switch. The scrape token of [`GET /metrics`](../api/prometheus.md) is independent of it too: turning authentication off does not make a token-free scrape from an untrusted peer acceptable.

## API Behavior

### DOCS_PUBLIC
Default: `false`

Serve `/docs` (Swagger UI) and `/openapi.json` without requiring a token. `false` disables both, including on loopback.

### Write support
GUI only (default off); the **Inverters** page controls write access and the approved write targets (none are approved on a fresh install). `ENABLE_WRITE_SUPPORT` is ignored with a warning.

### ENABLE_METRICS_ENDPOINT
Default: `true`

Serve the Prometheus endpoint `GET /metrics`. Set to `false` to answer 404.

## Push export (optional)

Pushes the `/metrics` values to InfluxDB 2 or QuestDB OSS at a fixed interval. Configure it on the GUI **TSDB** page; a saved change restarts the export at once and does not affect the Prometheus endpoint. See [Push export](../configuration/export.md). `DB_TYPE`, `INFLUXDB_*` and `QUESTDB_*` only seed the first start ([Environment Variables](../configuration/environment.md)).

## Logging and Debugging

### LOG_LEVEL
Default: `INFO`

One of: `DEBUG`, `INFO`, `WARNING`, `ERROR`

### TZ
Default: `Etc/UTC`

Timezone for container logs and internal timestamps (IANA timezone name).
