# Quick Start

## Prerequisites

- Python 3.13 or newer (or Docker, see [Docker](docker.md)).
- Network access to the inverter's TCP port 8899.
- The packaged protocol catalog (installed with the application).

## Install

```sh
python -m pip install -e '.[dev]'
```

`python -m pip install -e .` installs the runtime dependencies only.

## Configure

Copy `settings.env.example` to `settings.env`:

```sh
cp settings.env.example settings.env
```

The file holds only start parameters (`HMAC_SECRET`, `BIND_ADDRESS`, `BIND_PORT`,
`ALLOW_NON_LOOPBACK_BIND`, optionally `ADMIN_DB_PATH`); devices, authentication, API tokens, logging
and export are set in the admin GUI. Docker needs a stable random `HMAC_SECRET`
(`openssl rand -base64 32`) before starting; a local start generates it in `settings.env` if absent.

`settings.env` at the project root is read automatically; `--env-file` selects another file and
environment variables take precedence. See [Environment Variables](../configuration/environment.md).

## Administration and tokens

Open `http://127.0.0.1:8000/` after starting the server. The first start prints a boxed
`FIRST START` block with the one-time `admin` password (see
[Authentication](../configuration/authentication.md#administration-login)). The first login requires
a new password (at least 8 characters) before other pages become accessible.

Until it is changed, the service is idle: HTTP server and GUI are reachable, while periodic reads,
heartbeat polling, metric collection and the InfluxDB/QuestDB export stay stopped (warning
`Initial admin password not changed yet: ... paused ...`). They start automatically after the
change, without a restart.

Create a PAT on **API tokens** and copy it to your API client; the secret is shown only once. API
authentication is enabled by default even before a PAT exists.

## Validate and start

```sh
python -m app validate     # configuration, object registry and write allowlist
python -m app              # start the server (serve is the default mode)
```

Check the service:

```sh
curl http://127.0.0.1:8000/health
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8000/api/v1/devices
```

## Administration menu

| Page | Purpose |
| --- | --- |
| Overview | Configurable widgets with live values (solar, house consumption, grid, battery, TSDB export), optional spot-price, day-ahead, solar-forecast and charging-schedule widgets, one card per inverter (energy-flow graphic, online and operating state) and one per battery tower; inverters are added, edited and removed in the Inverters dialog. Values are whole numbers in the browser locale's format; a missing value (not configured, unreachable, no reading yet, export off) reads `n/a` |
| Inverters | Write access and the writable parameters |
| Energy Manager | Manual battery control (charge, keep idle, discharge) per inverter; Off returns control to the inverter's Internal strategy. Read-only day-ahead price (Spot Market) and Open-Meteo solar-forecast (Weather) previews; see [Energy Manager](../energy-manager.md) |
| TSDB | InfluxDB 2 / QuestDB export |
| Prometheus | Metrics endpoint and the exposed metrics |
| API tokens | Create and revoke personal access tokens |
| Settings | Documentation, authentication, server, network and the admin password |
| About | Version, runtime, project and dependency information. The footer of every page shows the version and the build hash `(abc1234)` (`dev` outside an image) |
