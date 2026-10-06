# Environment Variables

Environment variables and `settings.env` provide bootstrap defaults. Administration
settings saved through the GUI are persisted in `data/rct.db`. `HMAC_SECRET`
always stays outside the database. Timeouts, retries, cache, limits and intervals
remain fixed in code.

A set variable that is an internal setting but not one of these operator names is
logged once at start (never its value). Completely unknown variables are
ignored.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HMAC_SECRET` | generated locally | Stable secret, at least 32 characters (`openssl rand -base64 32`); provide before Docker startup. The old name `ADMIN_SECRET` still works but logs a deprecation warning |
| `ADMIN_DB_PATH` | `<project>/data/rct.db` | Location of the encrypted admin database |
| `DISPATCH_DB_PATH` | `<project>/data/rct-dispatch.db` | Location of the encrypted, crash-durable dispatch state database |
| `DISPATCH_MAX_CHARGE_POWER_W` | unset | Verified maximum grid-charge power; required for dispatch |
| `DISPATCH_MAX_DISCHARGE_POWER_W` | unset | Verified maximum load-following discharge power; required for dispatch |
| `BIND_ADDRESS` | `127.0.0.1` | Listen address |
| `BIND_PORT` | `8000` | Listen port, 1024 to 65535 |
| `ALLOW_NON_LOOPBACK_BIND` | `false` | Explicit consent for a non-loopback listener; does not provide TLS or secure cookies. Compose sets it while publishing only on host loopback |
| `DOCS_PUBLIC` | `false` | `true`: serve `/docs` and `/openapi.json` without a token; `false`: both return 404 on every bind address |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `TRUSTED_PROXIES` | empty | Networks (CIDR, comma separated, at most 32) of reverse proxies whose forwarding header is trusted |
| `FORWARDED_HEADER` | empty | Single-address header set by the proxy, for example `X-Forwarded-For`; requires `TRUSTED_PROXIES`. `Forwarded` (RFC 7239) is rejected |
| `ENABLE_METRICS_ENDPOINT` | `true` | `false`: the route `GET /metrics` is not mounted and answers 404 |
| `DB_TYPE` and `INFLUXDB_*` / `QUESTDB_*` | unset | Optional push export, see [Push export](export.md) |
| `METRICS_EXPORT_INTERVAL_SECONDS` | `30` | Export interval in seconds |

The dispatch values are deliberately not guessed. The strategy code and both sign conventions used
to live as the three environment variables `DISPATCH_SOC_STRATEGY_EXTERNAL_CODE`,
`DISPATCH_BATTERY_DISCHARGE_POSITIVE` and `DISPATCH_GRID_IMPORT_POSITIVE`; **this is a
non-backward-compatible configuration change**: all three are now fields of a persisted,
per-device capability record, set through the admin dispatch API after hardware verification, not
through the environment. A set value for any of the three is ignored with a `WARNING` naming the
variable (never its value). See [Operation](../operation.md#battery-dispatch-capabilities) for the
capability workflow and the engineering-mode escape hatch.
The dispatch database must be backed up together with `HMAC_SECRET`, just like `data/rct.db`.

## GUI-only settings

Authentication, API tokens, devices, write support and reverse-proxy mode are set in the admin GUI. The variables `AUTH_REQUIRED`, `API_TOKENS`, `API_TOKENS_FILE`, `DEVICES`, `BEHIND_REVERSE_PROXY` and `ENABLE_WRITE_SUPPORT` are ignored with a startup warning.

## Validate a configuration

```sh
python -m app validate
```

The command checks the settings, the object registry and the write allowlist
and exits with a non-zero status on an invalid configuration.
