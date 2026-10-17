# Troubleshooting

Find the symptom, check the likely cause, apply the fix. Low-level connection analysis (counters,
unsolicited frames, packet capture) is on [Transport Diagnostics](reference/transport-diagnostics.md).

| Symptom                                             | Section                                                            |
| --------------------------------------------------- | ------------------------------------------------------------------ |
| Container unhealthy or unreachable                  | [Unhealthy container](#the-container-is-unhealthy-or-unreachable)  |
| Service refuses to start                            | [Start refused](#the-start-is-refused)                             |
| Cannot log in as `admin`                            | [First admin login](#first-admin-login)                            |
| `/docs` answers 404                                 | [`/docs` 404](#docs-answers-404)                                   |
| Write, dispatch or Energy Manager routes answer 404 | [Writes 404](#write-endpoints-answer-404)                          |
| 429 Too Many Requests                               | [429](#429-from-many-clients-behind-a-proxy)                       |
| 503 `device_maintenance`                            | [Maintenance](#503-device_maintenance)                             |
| Mixed, stale or missing values                      | [Stale values](#mixed-or-stale-values)                             |
| Read timeouts, reconnects, unexpected frames        | [Connection drops](#read-timeouts-reconnects-or-unexpected-frames) |
| Charging plan shows no plan                         | [Plan status](#charging-plan-shows-no-plan)                        |

## The container is unhealthy or unreachable

- **Likely cause:** the service listens on loopback inside the container, or `BIND_PORT` differs from
  the container side of the port mapping (also after changing the port in the GUI). A loopback bind in
  a container logs a warning at start.
- **First check:** `docker compose -f docker/compose.yaml logs rct-api` for the bind warning.
- **Fix:** set `BIND_ADDRESS=0.0.0.0` and the GUI option "behind reverse proxy", and keep `BIND_PORT`
  equal to the container port of the mapping. See [Docker](getting-started/docker.md) and
  [Network and TLS](configuration/network.md#listener-and-container-port).

## The start is refused

- **First check:** `python -m app validate` (or `docker compose -f docker/compose.yaml run --rm
  rct-api validate`).
- **Typical causes and fixes:**
    - a GUI device list with more than one device without network id on the same endpoint: give each
      device its own endpoint or network id;
    - an invalid object registry or write allowlist: fix the entry the validation names;
    - missing `HMAC_SECRET` in a read-only container, or a secret that differs from the one that
      encrypted the existing database: set the original secret.

## First admin login

- **Likely cause:** the generated `admin` password is printed once, in the boxed `FIRST START` block
  on stdout, when the admin account is created; restarts neither show nor replace it.
- **First check:** the logs of the first start.
- **Fix:** if missed, there is no file to read it from: reset the admin account. The first login
  requires a password change. Keep database and `HMAC_SECRET` together when restoring a backup.

## `/docs` answers 404

- **Cause:** `/docs` and `/openapi.json` are served only with `DOCS_PUBLIC=true`.
- **Fix:** set `DOCS_PUBLIC=true`.

## Write endpoints answer 404

- **Cause:** write access is off (GUI **Inverters** page); the write, dispatch and Energy Manager
  routes answer 404 `write_disabled`.
- **Fix:** switch write access on. It takes effect at once, without a restart.

## 429 from many clients behind a proxy

- **Likely cause:** all callers share the proxy address, so they share one rate-limit budget.
- **Fix:** set `TRUSTED_PROXIES` and `FORWARDED_HEADER`; see
  [Network and TLS](configuration/network.md).
- **Admin GUI:** it backs off by itself on a 429 and honours `Retry-After`; its requests use the
  separate `ADMIN_SESSION_RATE_LIMIT_*` budget (see [Environment](configuration/environment.md)).

## 503 `device_maintenance`

- **Cause:** the inverter signalled its bootloader.
- **Fix:** none needed. Requests resume after the cool-down once a single read succeeds.

## Mixed or stale values

- **Likely cause:** another client (vendor app, home automation) uses port 8899, or a second instance
  runs against the same inverter.
- **First check:** `foreign_access_suspected` in `GET /api/v1/readiness`.
- **Fix:** stop the other client or instance; see
  [Operation](operation.md#exclusive-access-to-port-8899). Values carry `stale`, `source` and
  `age_seconds`.

## Read timeouts, reconnects or unexpected frames

- **Likely cause:** line noise, a network problem, another client, or periodic subscriptions that the
  inverter kept from an earlier session. The log shows "Connection to ... dropped: no response to
  object ..." or `Unsolicited response burst started`.
- **First check:** `GET /api/v1/readiness` (`periodic_available`, `periodic_setup_failures`,
  `foreign_access_suspected`) and the `rct_transport_*` metrics.
- **Fix:** remove foreign access first. For counters, packet capture and the timeout runbook see
  [Transport Diagnostics](reference/transport-diagnostics.md).

## Charging plan shows no plan

- **Likely cause:** a planner precondition is missing. The status names it:
  `battery_state_unavailable`, `spot_disabled`, `spot_prices_unavailable`, `solar_not_configured` or
  `solar_forecast_unavailable`.
- **First check:** the readiness checklist on the **Charging plan** tab.
- **Fix:** enable and configure the spot market and weather forecast, connect the inverter, or set an
  Expert capacity override. Meaning of each status:
  [Planner status codes](energy/charging-plan.md#planner-status-codes).
