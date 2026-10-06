<p align="center">
  <img src=".github/img/rct-logo-black.svg#gh-light-mode-only" width="300">
  <img src=".github/img/rct-logo-white.svg#gh-dark-mode-only" width="300">
</p>

<h2 align="center">Your RCT Power inverter, one REST call away.</h2>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/Python-3.13%2B-3776AB?logo=python&logoColor=white" alt="Python 3.13+">
  <img src="https://img.shields.io/badge/Platform-linux%2Famd64%20%7C%20linux%2Farm64-lightgrey?logo=linux&logoColor=white" alt="Platform">
</p>

<p align="center">
  <a href="https://gill-bates.github.io/rct-manager/"><img src="https://img.shields.io/badge/Documentation-2ea44f?style=for-the-badge&logo=readthedocs&logoColor=white" alt="Documentation"></a>
  <a href="https://gill-bates.github.io/rct-manager/getting-started/quick-start/"><img src="https://img.shields.io/badge/Quick%20Start-0a7bbb?style=for-the-badge&logo=docker&logoColor=white" alt="Quick Start"></a>
</p>

---

## What it is

A vendor-neutral REST gateway for RCT Power inverters. It speaks the RCT serial
protocol over TCP to one or more inverters and turns it into clean JSON, a
Prometheus endpoint and optional, guarded write access. No vendor app, no
protocol knowledge needed.

---

## ✨ Features

| Category | Highlights |
|---|---|
| **REST API** | Read any metric from the full protocol catalog (894 of 895 IDs), partial success on multi-metric requests, RFC 9457 error responses |
| **Administration GUI** | Overview, Inverters, TSDB, Prometheus, API tokens, Settings and About pages with autosave; first-login password change |
| **Prometheus** | Ready-made `/metrics` endpoint for Telegraf, Grafana and friends; a scrape never touches the inverter |
| **Safe by default** | API token authentication, separate admin sessions, `read` and `read/write` roles, encrypted SQLite administration storage |
| **Guarded writes** | Off by default, allowlist-based, with readback confirmation |
| **Multi-device** | Several inverters behind one API, requests per endpoint serialized so the device never sees a conflict |
| **Hardened container** | Read-only root filesystem, all capabilities dropped, non-root user, built-in health check |
| **Vendor-neutral contract** | Another device adapter can fill the same fields without changing paths or error keys |

---

## 🚀 Getting Started

```sh
cp settings.env.example settings.env   # set HMAC_SECRET (openssl rand -base64 32) for Docker
docker compose -f docker/compose.yaml up -d
```

Without Docker, use Python 3.13+:

```sh
python -m pip install -e '.[dev]'
python -m app validate
python -m app
```

Open `http://127.0.0.1:8000/`. The first start prints a boxed `FIRST START - admin login` block
with the one-time `admin` password in clear text (also saved to `data/initial-admin-password` as
a fallback); the first login requires a password change
([details](docs/configuration/authentication.md)). Until then, periodic reads, heartbeat and
metric export stay paused; they start automatically afterwards. Manage inverters, TSDB export, Prometheus metrics,
PATs and settings in the GUI. Changes autosave with a toast.
Settings are stored in `data/rct.db` using authenticated encryption with HMAC.
The stable `HMAC_SECRET` stays in `settings.env`; local startup generates it
when absent. For Docker, provide it before starting. Back up the database and
secret together. See [Configuration](docs/getting-started/configuration.md).

Installation, configuration, authentication and the full API reference are in the
**[Documentation](https://gill-bates.github.io/rct-manager/)**. Container details
are in [`docker/README_docker.md`](docker/README_docker.md).

---

> [!IMPORTANT]
> **Disclaimer:** This is an independent open-source project. It is not affiliated
> with, endorsed by or connected to RCT Power GmbH, Line-Eid-Str. 1, D-78467
> Konstanz. "RCT Power" and related names are trademarks of their respective owner
> and are used only to describe compatibility.

---

## License

Released under the [MIT License](LICENSE). Third-party components retain their
respective licenses.

<p align="center">
  <a href="https://www.buymeacoffee.com/tnsteinerx">
    <img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20beer&emoji=%F0%9F%8D%BA&slug=tnsteinerx&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" alt="Buy Me A Coffee">
  </a>
</p>
