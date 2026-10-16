---
hide:
  - navigation
---

<p align="center">
  <img src="assets/rct-logo-black.svg" width="400" alt="rct-manager Logo" class="rct-logo-light">
  <img src="assets/rct-logo-white.svg" width="400" alt="rct-manager Logo" class="rct-logo-dark">
</p>

<p align="center">
  <strong>Vendor-neutral REST gateway for RCT Power inverters, with Energy Manager and Prometheus metrics</strong>
</p>

<p align="center">
  <a href="https://github.com/Gill-Bates/rct-manager/releases"><img src="https://img.shields.io/github/v/release/Gill-Bates/rct-manager?logo=github&logoColor=white" alt="GitHub Release"></a>
  <a href="https://hub.docker.com/r/giiibates/rct-manager"><img src="https://img.shields.io/docker/pulls/giiibates/rct-manager?logo=docker&logoColor=white" alt="Docker Pulls"></a>
  <a href="https://github.com/Gill-Bates/rct-manager/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
</p>

---

## What is rct-manager?

rct-manager speaks the RCT serial protocol over TCP (port 8899) to one or more inverters and
exposes measurements, optional write access and a Prometheus endpoint over HTTP. All requests to
one inverter endpoint are serialized inside the process.

## Key Features

<div class="grid cards" markdown>

-   :material-api:{ .lg .middle } **REST API**

    ---

    Measurements and opt-in, allow-listed writes over HTTP with personal access tokens, fail-closed by default, and RFC 9457 problem details.

    [:octicons-arrow-right-24: API Overview](api/overview.md)

-   :material-battery-charging:{ .lg .middle } **Energy Manager**

    ---

    Business actions for the battery (charge, hold, discharge), operating modes per inverter, and a read-only dynamic charging plan preview.

    [:octicons-arrow-right-24: Energy Manager](energy-manager.md)

-   :material-currency-eur:{ .lg .middle } **Spot Market & Solar Forecast**

    ---

    Quarter-hour day-ahead prices from Energy-Charts and an Open-Meteo solar forecast, served from the database and fetched once per day.

    [:octicons-arrow-right-24: Spot Market](energy-manager.md#spot-market)

-   :material-chart-line:{ .lg .middle } **Monitoring & Export**

    ---

    Prometheus text format at `GET /metrics` and optional push export to InfluxDB 2 or QuestDB.

    [:octicons-arrow-right-24: Prometheus Metrics](api/prometheus.md)

-   :material-shield-lock:{ .lg .middle } **Secure by Default**

    ---

    Hardened container, encrypted admin and dispatch databases, loopback bind, and TLS terminated at your reverse proxy.

    [:octicons-arrow-right-24: Authentication](configuration/authentication.md)

-   :material-view-dashboard:{ .lg .middle } **Admin GUI**

    ---

    Overview, inverters, Energy Manager, time-series database, API tokens and settings in one responsive web interface.

    [:octicons-arrow-right-24: Quick Start](getting-started/quick-start.md)

</div>

## Quick Start

Get rct-manager up and running in minutes:

```sh
git clone https://github.com/Gill-Bates/rct-manager.git
cd rct-manager
python -m pip install -e .
cp settings.env.example settings.env
python -m app  # or run the container, see Docker
```

!!! success "First start"
    Open `http://127.0.0.1:8000/` and sign in as `admin`. The first start prints a boxed
    `FIRST START` block in the log with the one-time password.

    !!! warning
        Write access, dispatch and the Energy Manager are off until you enable and verify them in the GUI.

[:material-rocket-launch: Quick Start](getting-started/quick-start.md){ .md-button .md-button--primary }
[:simple-docker: Run with Docker](getting-started/docker.md){ .md-button }

## Screenshots

=== "Energy Manager"
    ![Energy Manager spot-market configuration and day-ahead price preview](assets/screenshot_02.png)

## At a glance

| Topic | Summary |
| --- | --- |
| Language | Python 3.13+, FastAPI, Uvicorn |
| Devices | 1 to 32 inverter endpoints, configured in the GUI |
| Authentication | Personal access tokens (`pat_...`) as bearer tokens, fail-closed by default |
| Administration | Web GUI: Overview, Inverters, Energy Manager, TSDB, Prometheus, API tokens, Settings, About |
| Writes | Opt-in (enabled in the GUI) and limited to the approved parameters |
| Errors | RFC 9457 problem details |
| Monitoring | Prometheus text format at `GET /metrics`; optional push export to InfluxDB 2 or QuestDB |
| Transport | Plain HTTP; TLS terminates at a reverse proxy |
| License | [MIT](https://github.com/Gill-Bates/rct-manager/blob/main/LICENSE) |

## Where to go next

- [Environment Variables](configuration/environment.md): start parameters and optional seeds; everything else is set in the GUI.
- [Operation](operation.md): constraints you must respect in production.
- [Protocol Specification](reference/protocol.md): the vendor's RCT serial protocol PDF.
- [Troubleshooting](troubleshooting.md): common problems and fixes.
