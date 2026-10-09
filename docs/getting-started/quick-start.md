# Quick Start

## Prerequisites

- Python 3.13 or newer (or Docker, see [Docker](docker.md)).
- Network access to the inverter's TCP port 8899.
- The packaged protocol catalog (installed with the application).

## Install

```sh
python -m pip install -e '.[dev]'
```

Without development tools, `python -m pip install -e .` installs the runtime
dependencies only.

## Configure

Copy `settings.env.example` to `settings.env`:

```sh
cp settings.env.example settings.env
```

The file only holds start parameters (`HMAC_SECRET`, `BIND_ADDRESS`, `BIND_PORT`,
`ALLOW_NON_LOOPBACK_BIND` and optionally `ADMIN_DB_PATH`).
Devices, authentication, API tokens, logging and export are configured in the
administration GUI.
For Docker, supply a stable random `HMAC_SECRET` (`openssl rand -base64 32`)
before starting. Local startup generates the secret in `settings.env` if absent.

The application reads `settings.env` beside `run.py` automatically.
`--env-file` selects another file. Environment variables take precedence.
See [Environment Variables](../configuration/environment.md) for all settings.

## Administration and tokens

Open `http://127.0.0.1:8000/` after starting the server. The first start prints a boxed
`FIRST START - admin login` block with the one-time `admin` password (8 characters, one special
character) in clear text, so it can be copy-pasted straight from the console. It is also saved to
`initial-admin-password` next to the admin database as a fallback for runs without a visible
console:

```sh
cat data/initial-admin-password
```

The first login requires a new password (at least 8 characters) before other administration pages
become accessible; delete the file afterwards.

Until that password is changed, the service is running but idle: the HTTP server
and the administration GUI are reachable, while periodic reads, heartbeat
polling, metric collection and the InfluxDB/QuestDB export stay stopped. The log
shows `Initial admin password not changed yet: ... paused ...` as a warning.
After the password change these jobs start automatically; no restart is needed.
Later starts with an already changed password run normally.

Create a PAT on
**API tokens** and copy it to your API client; its secret is shown only once.
API authentication defaults to enabled even before a PAT exists.
See [Authentication](../configuration/authentication.md).

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
| Overview | Configurable widgets with live values (solar, house consumption, grid, battery, TSDB export) and one card per inverter with its energy-flow graphic, online and operating state, and one card per battery tower; add, edit and remove inverters from the Inverters dialog. Values are shown as whole numbers, with the decimal and thousands separators of the browser locale |
| Inverters | Write access and the writable parameters |
| Energy Manager | Manual battery control (charge, keep idle, discharge, return to automatic) per inverter, see [Energy Manager](../energy-manager.md) |
| TSDB | InfluxDB 2 / QuestDB export |
| Prometheus | Metrics endpoint and the exposed metrics |
| API tokens | Create and revoke personal access tokens |
| Settings | Documentation, authentication, server, network and the admin password |
| About | Version, runtime, project and dependency information. The footer of every page shows the version and the build hash `(abc1234)` (`dev` outside an image) |
