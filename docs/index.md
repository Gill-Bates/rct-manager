# rct-rest-api

Vendor-neutral REST gateway for RCT Power inverters.

The service speaks the RCT serial protocol over TCP (port 8899) to one or more
inverters and exposes measurements, optional write access and a Prometheus
endpoint over HTTP. All requests to one inverter endpoint are serialized inside
the process.

## Where to start

- [Quick Start](getting-started/quick-start.md): run the service from Python and log in to the GUI.
- [Docker](getting-started/docker.md): run the hardened container with Compose.
- [Environment Variables](configuration/environment.md): start parameters and optional seeds; everything else is set in the GUI.
- [API Overview](api/overview.md): paths, authentication and error format.
- [Operation](operation.md): constraints you must respect in production.
- [Protocol Specification](reference/protocol.md): the vendor's RCT serial protocol PDF.

## At a glance

| Topic | Summary |
| --- | --- |
| Language | Python 3.13+, FastAPI, Uvicorn |
| Devices | 1 to 32 inverter endpoints, configured in the GUI |
| Authentication | Personal access tokens (`pat_...`) as bearer tokens, fail-closed by default |
| Administration | Web GUI: Overview, Inverters, TSDB, Prometheus, API tokens, Settings, About |
| Writes | Opt-in (enabled in the GUI) and limited to the approved parameters |
| Errors | RFC 9457 problem details |
| Monitoring | Prometheus text format at `GET /metrics`; optional push export to InfluxDB 2 or QuestDB |
| Transport | Plain HTTP; TLS terminates at a reverse proxy |
| License | [MIT](https://github.com/Gill-Bates/rct-rest-api/blob/main/LICENSE) |

