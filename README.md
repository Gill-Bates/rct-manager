<p align="center">
  <img src=".github/img/rct-logo-black.svg#gh-light-mode-only" width="300">
  <img src=".github/img/rct-logo-white.svg#gh-dark-mode-only" width="300">
</p>

<h2 align="center">Your RCT Power inverter is just one REST call away.</h2>

<p align="center">
  <b>Take charge of your home battery.</b> The Energy Manager lets you charge, hold or
  discharge it with day-ahead spot prices and the solar forecast in view, all from one admin GUI.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/Python-3.13%2B-3776AB?logo=python&logoColor=white" alt="Python 3.13+">
  <img src="https://img.shields.io/badge/Platform-linux%2Famd64%20%7C%20linux%2Farm64-lightgrey?logo=linux&logoColor=white" alt="Platform">
  <a href="https://hub.docker.com/r/giiibates/rct-manager"><img src="https://img.shields.io/docker/pulls/giiibates/rct-manager?logo=docker&label=pulls" alt="Docker Pulls"></a>
</p>

<p align="center">
  <a href="https://gill-bates.github.io/rct-manager/"><img src="https://img.shields.io/badge/Documentation-2ea44f?style=for-the-badge&logo=readthedocs&logoColor=white" alt="Documentation"></a>
  <a href="https://gill-bates.github.io/rct-manager/getting-started/quick-start/"><img src="https://img.shields.io/badge/Quick%20Start-0a7bbb?style=for-the-badge&logo=docker&logoColor=white" alt="Quick Start"></a>
  <a href="https://gill-bates.github.io/rct-manager/energy-manager/"><img src="https://img.shields.io/badge/Energy%20Manager-f2a900?style=for-the-badge" alt="Energy Manager"></a>
</p>

---

## What is RCT Manager?

Do you have an RCT inverter and want to do more than the manufacturer’s app
allows? RCT Manager connects to one or more inverters via TCP and puts their
readings and controls behind a REST API, an admin GUI and a live dashboard. You
build your own integrations without touching the proprietary RCT protocol, and
no vendor app is required.

<p align="center">
  <img src=".github/img/screenshot_01.png" width="800" alt="RCT Manager dashboard">
  <br>
  <img src=".github/img/screenshot_02.png" width="800" alt="Energy Manager with the day-ahead spot-price preview">
</p>

---

## ⚡ Energy Manager

**Your battery, your rules.** Charge from the grid when power is cheap, keep the
battery idle, or let it supply the house down to a target state of charge, with
one click in the admin GUI or from your own app through the API. The day-ahead
spot prices (Energy-Charts) and the solar production forecast (Open-Meteo) live
on the same Energy Manager page and on the dashboard, so you see at a glance when
charging is worth it.

Safe by default: battery control stays off until you switch on write access and
verify each inverter's hardware, and nothing happens without your decision.

👉 [Learn more about the Energy Manager](https://gill-bates.github.io/rct-manager/energy-manager/)

> [!NOTE]
> **Preview:** a dynamic charging planner turns prices, the solar forecast and
> your consumption history into a proposed charging plan, shown in the Energy
> Manager's *Charging plan* tab. It is a dry-run preview only and never controls
> the battery.

---

## ✨ More highlights

- **REST API** for the full protocol catalogue, with JSON responses and clear
  error messages. [API overview](https://gill-bates.github.io/rct-manager/api/overview/)
- **Admin GUI and dashboard** for inverters, Energy Manager, export, API tokens
  and settings, all in one place.
- **Your own analytics** through an opt-in Prometheus `/metrics` endpoint or push
  export to InfluxDB 2 or QuestDB, with a ready-made
  [Grafana dashboard](grafana/dashboard_questdb.json).
  [Push export](https://gill-bates.github.io/rct-manager/configuration/export/)
- **Guarded access** with API tokens, separate admin sessions and writes that are
  off by default. [Authentication](https://gill-bates.github.io/rct-manager/configuration/authentication/)
- **Multiple inverters** behind one API, with coordinated device access.
- **Hardened container** for `linux/amd64` and `linux/arm64`: read-only root file
  system, non-root process, health check.

---

## 🚀 Quick start

```sh
cp settings.env.example settings.env   # set HMAC_SECRET, e.g. openssl rand -base64 32
RCT_API_TAG=latest docker compose -f docker/compose.yaml up -d
```

Open `http://127.0.0.1:8000/` and log in with the one-time `admin` password from
the startup log. The [Quick Start guide](https://gill-bates.github.io/rct-manager/getting-started/quick-start/)
covers everything else, including running without Docker.

> [!WARNING]
> Reaching the service from beyond localhost? Terminate TLS in a reverse proxy in
> front of it and switch on "behind reverse proxy" in the admin GUI.
> [Network and TLS](https://gill-bates.github.io/rct-manager/configuration/network/)

---

## 📚 Documentation

Everything in detail lives at **<https://gill-bates.github.io/rct-manager/>**:
[Docker](https://gill-bates.github.io/rct-manager/getting-started/docker/) ·
[Configuration](https://gill-bates.github.io/rct-manager/getting-started/configuration/) ·
[Energy Manager](https://gill-bates.github.io/rct-manager/energy-manager/) ·
[API](https://gill-bates.github.io/rct-manager/api/overview/) ·
[Operation](https://gill-bates.github.io/rct-manager/operation/) ·
[Troubleshooting](https://gill-bates.github.io/rct-manager/troubleshooting/) ·
[Changelog](CHANGELOG.md)

If RCT Manager works well for you, please leave a
[star on GitHub](https://github.com/Gill-Bates/rct-manager). If something is
missing or does not work as expected, open an
[issue](https://github.com/Gill-Bates/rct-manager/issues/new/choose).

---

> [!IMPORTANT]
> **Disclaimer:** This is an independent open-source project. It is not affiliated
> with, endorsed by or connected to RCT Power GmbH, Line-Eid-Str. 1, D-78467
> Konstanz. "RCT Power" and related names are trademarks of their respective owners
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
