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

## Administration storage

`HMAC_SECRET` is a stable random secret of at least 32 characters (`openssl rand -base64 32`; the
deprecated name `ADMIN_SECRET` is still read). A local start generates it and appends it to
`settings.env` when absent; Docker requires it up front (read-only root file system). Keep it in
`settings.env`, never in version control.

The database stores settings and credentials with Fernet authenticated encryption (AES with
HMAC-SHA256). Passwords use a salted hash; PAT secrets are shown once and only their digests are
kept. Back up `data/rct.db` together with `HMAC_SECRET`; changing the secret prevents decryption.
The GUI never displays the secret. `ADMIN_DB_PATH` overrides the location (default
`<project>/data/rct.db`).

The first start prints the generated `admin` password, see
[Authentication](../configuration/authentication.md#administration-login).

The database runs in SQLite WAL mode (`synchronous=NORMAL`, 5 s busy timeout), so concurrent
autosaves, sessions and token bookkeeping do not block each other. While the service runs,
`rct.db-wal` and `rct.db-shm` may exist next to `rct.db`; they get the same 0600 permissions and
are folded back into `rct.db` on a clean shutdown. Rules:

- Back up while the service is stopped (copy all existing `rct.db*` files together) or online with
  `sqlite3 data/rct.db ".backup backup.db"`; never copy only `rct.db` of a running service.
- Keep the directory on a local file system. WAL does not work on NFS or SMB shares.
- The directory must be writable (in Docker the `rct-data` volume), because SQLite creates the
  `-wal` and `-shm` files there.

## Device and Network

### Devices
Add inverters on the **Overview** dashboard with **Add inverter** in the Inverters card. The modal editor stores devices encrypted in `data/rct.db`.

- Edits stay a draft until **Apply changes**, which rebuilds the connections without a restart; **Discard** drops them. A list the server rejects is rolled back.
- Re-addressing or removing an inverter resets its verification evidence, engineering mode and Energy Manager mode (back to Off) and asks for confirmation.
- A device is an IP address or host name plus a port. The service assigns the id (`main`, then `inverter-2`, `inverter-3`, ...) and keeps it while the device exists, so API paths and exports stay stable. Editing the host keeps the id; duplicate addresses are rejected.
- Display name precedence: saved name, inverter-reported name, device id.
- A `DEVICES` environment variable is ignored (INFO note at startup).

### BIND_ADDRESS, ALLOW_NON_LOOPBACK_BIND, BIND_PORT
Defaults: `127.0.0.1`, `false`, `8000`.

`BIND_ADDRESS` is the REST API listen address (not the inverter address). A non-loopback address
such as `0.0.0.0` requires `ALLOW_NON_LOOPBACK_BIND=true`; the flag allows the bind but enables
neither TLS nor secure cookies, so put a TLS-terminating reverse proxy in front when the service is
reachable beyond localhost. Docker Compose sets the flag because the container listens on
`0.0.0.0` while the host publishes the port on `127.0.0.1`.

The port can also be changed in the GUI; the service re-binds in the background, and the value
saved in the GUI takes precedence over `BIND_PORT` after the first start. In a container this
moves only the internal listener: the published port mapping and the health check's `BIND_PORT`
stay unchanged, so the container reports unhealthy and the published port stops reaching the
service until environment, compose value and mapping match the new port. The GUI warns before
applying the change.

### Behind reverse proxy
GUI only. Confirms a TLS-terminating proxy in front of the service and sets the secure flag on session cookies. It does not trust any forwarded header; that is `TRUSTED_PROXIES` below. A `BEHIND_REVERSE_PROXY` variable is ignored with a warning. A consented non-loopback bind logs a warning until this option is enabled in the GUI.

### TRUSTED_PROXIES and FORWARDED_HEADER
Default: empty. See [Network and TLS](../configuration/network.md#caller-address).

`TRUSTED_PROXIES`: comma-separated reverse-proxy networks (CIDR, at most 32), the only proxy trust
list. Used to extract the client IP and to accept `X-Forwarded-Proto` for the admin interface.
Without it all callers share the proxy's address; behind an unlisted TLS proxy admin logins and
changes fail with 403. `FORWARDED_HEADER`: single header name set by the proxy (e.g.
`X-Forwarded-For`), required together with `TRUSTED_PROXIES`; `Forwarded` (RFC 7239) is rejected.

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
