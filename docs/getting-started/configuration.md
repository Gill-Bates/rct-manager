# Configuration

Copy `settings.env.example` to `settings.env`. It only holds the start parameters
`HMAC_SECRET`, `BIND_ADDRESS`, `BIND_PORT`, `ALLOW_NON_LOOPBACK_BIND` and optionally `ADMIN_DB_PATH`.
Other settings are managed in the administration GUI and persisted in `data/rct.db`; the
[optional environment variables](../configuration/environment.md) only seed the very first start
(for example in automated deployments).
Changes autosave and show a toast. Settings requiring a server restart are marked in the GUI.
Internal tuning (timeouts, retries, cache, limits and intervals) remains fixed in code.

## Administration storage

`HMAC_SECRET` is a stable random secret of at least 32 characters (`openssl rand -base64 32`; the deprecated name `ADMIN_SECRET` is still read). Local server startup
generates it and appends it to `settings.env` when absent. Docker requires it to be set
before starting because its root filesystem is read-only. Generate a secret with:

```sh
openssl rand -base64 32
```

Keep the result in `settings.env`, never in version control. The database stores settings
and credential records using Fernet authenticated encryption (AES with HMAC-SHA256).
Passwords use a salted hash; PAT secrets are shown once and only their digests are retained.
Back up `data/rct.db` together with `HMAC_SECRET`; changing the secret prevents decryption.
The GUI never displays the secret.

`ADMIN_DB_PATH` overrides the database location (default `<project>/data/rct.db`).

The very first start prints the generated `admin` password in clear text in the boxed
`FIRST START - admin login` block on stdout, so it can be copied straight from the console. It is
also saved to `initial-admin-password` in the same directory, with mode `0600`, as a fallback for
runs without a visible console. The password never appears in the log. Change it at first login
and delete the file; keep the directory out of version control and out of backups that are shared
more widely than the database itself.

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
Configure inverter endpoints from the **Overview** dashboard with the plus button in the Inverters card; the editor opens in a modal and stores the devices encrypted in `data/rct.db`. A device consists of an IP address or host name and a port; the service assigns the device id (`main` for the first one, then `inverter-2`, `inverter-3`, ...) and keeps it while the device exists, so API paths and exports stay stable. A saved display name takes precedence; otherwise, the dashboard uses the inverter-reported name, then the device id. Editing the host keeps the id; duplicate addresses are rejected. Devices saved earlier keep their ids and custom names. A `DEVICES` environment variable is ignored; the service notes it at startup on INFO level.

### BIND_ADDRESS
Default: `127.0.0.1`

REST API listen address. Not the inverter connection address. A non-loopback
address such as `0.0.0.0` requires `ALLOW_NON_LOOPBACK_BIND=true`. This flag
allows the bind; it does not enable TLS or secure cookies. Place a TLS-terminating
reverse proxy in front of the service when it is reachable beyond localhost.

### ALLOW_NON_LOOPBACK_BIND
Default: `false`

Set to `true` only when a non-loopback listener is required and access to it is
restricted. Docker Compose sets it because the container listens on `0.0.0.0`
while the host publishes the port on `127.0.0.1`.

### BIND_PORT
Default: `8080`

REST API listen port.

### Behind reverse proxy
GUI only. Confirms a TLS-terminating proxy in front of the service (secure cookies, client address handling). A `BEHIND_REVERSE_PROXY` variable is ignored with a warning. A consented non-loopback bind logs a warning until this option is enabled in the GUI.

### TRUSTED_PROXIES
Default: empty

Comma-separated list of reverse-proxy networks (CIDR notation, at most 32). Used to extract the real client IP from the header set by the proxy. Without this, all callers share the proxy's address.

### FORWARDED_HEADER
Default: empty

Single HTTP header name set by the reverse proxy (e.g., `X-Forwarded-For`). Must not be `Forwarded` (RFC 7239 is rejected). Required together with `TRUSTED_PROXIES`.

## Authentication and Tokens

### Authentication and tokens
GUI only: **Require authentication** (default on) and the personal access tokens (`pat_...`) live in the admin database. `AUTH_REQUIRED`, `API_TOKENS` and `API_TOKENS_FILE` are ignored with a warning. Changing settings always needs an admin session or a `read/write` token, regardless of the authentication switch. The scrape token of [`GET /metrics`](../api/prometheus.md) is independent of the switch as well: turning authentication off does not make an untrusted peer's token-free scrape acceptable.

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

Pushes the metrics of `/metrics` to InfluxDB 2 or QuestDB OSS at a fixed interval. Configure it on
the GUI **TSDB** page; a change takes effect after a restart. It does not affect the Prometheus
endpoint. Details: [Push export](../configuration/export.md). The `DB_TYPE`, `INFLUXDB_*` and
`QUESTDB_*` variables only seed the very first start and are listed in
[Environment Variables](../configuration/environment.md).

## Logging and Debugging

### LOG_LEVEL
Default: `INFO`

One of: `DEBUG`, `INFO`, `WARNING`, `ERROR`

### TZ
Default: `Etc/UTC`

Timezone for container logs and internal timestamps (IANA timezone name).
