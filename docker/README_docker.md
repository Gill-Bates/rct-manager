<p align="center">
  <img src="https://raw.githubusercontent.com/Gill-Bates/rct-manager/main/.github/img/rct-logo-black.svg" width="300" alt="RCT Manager">
</p>

<h2 align="center">Your RCT Power inverter is just one REST call away.</h2>

<p align="center">
  <b>Take charge of your home battery.</b> The Energy Manager lets you charge, hold or
  discharge it with day-ahead spot prices and the solar forecast in view, all from one admin GUI.
</p>

<p align="center">
  <a href="https://github.com/Gill-Bates/rct-manager/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/Python-3.13%2B-3776AB?logo=python&logoColor=white" alt="Python 3.13+">
  <img src="https://img.shields.io/badge/Platform-linux%2Famd64%20%7C%20linux%2Farm64-lightgrey?logo=linux&logoColor=white" alt="Platform">
  <a href="https://hub.docker.com/r/giiibates/rct-manager"><img src="https://img.shields.io/docker/pulls/giiibates/rct-manager?logo=docker&label=pulls" alt="Docker Pulls"></a>
</p>

<p align="center">
  <a href="https://gill-bates.github.io/rct-manager/"><img src="https://img.shields.io/badge/Documentation-2ea44f?style=for-the-badge&logo=readthedocs&logoColor=white" alt="Documentation"></a>
  <a href="https://gill-bates.github.io/rct-manager/getting-started/docker/"><img src="https://img.shields.io/badge/Docker%20Guide-0a7bbb?style=for-the-badge&logo=docker&logoColor=white" alt="Docker Guide"></a>
</p>

---

## What is RCT Manager?

Do you have an RCT inverter and want to do more than the manufacturer’s app
allows? This image connects to one or more inverters via TCP and puts their
readings and controls behind a REST API, an admin GUI and a live dashboard. No
vendor app and no knowledge of the proprietary RCT protocol required.

<p align="center">
  <img src="https://raw.githubusercontent.com/Gill-Bates/rct-manager/main/.github/img/screenshot_01.png" width="800" alt="RCT Manager dashboard">
</p>

---

## ⚡ Energy Manager

**Your battery, your rules.** Charge from the grid when power is cheap, keep the
battery idle, or let it supply the house down to a target state of charge, with
one click in the admin GUI or from your own app through the API. Day-ahead spot
prices (Energy-Charts) and the solar production forecast (Open-Meteo) live on the
same Energy Manager page and on the dashboard.

Safe by default: battery control stays off until you switch on write access and
verify each inverter's hardware.

👉 [Learn more about the Energy Manager](https://gill-bates.github.io/rct-manager/energy-manager/)

Read-only preview: the dynamic charging plan proposes a schedule from prices, forecast and
consumption history in the Energy Manager's *Charging plan* tab. It is a dry run that sends no
inverter command and never controls the battery.

---

## ✨ Why this image

- **Multi-arch:** native `linux/amd64` and `linux/arm64` images under one tag.
- **Hardened:** read-only root file system, dropped capabilities, non-root server
  process and a built-in health check.
- **All in one service:** REST API, admin GUI, dashboard, API tokens, an opt-in
  Prometheus `/metrics` endpoint and push export to InfluxDB 2 or QuestDB.
- **Persistent and encrypted:** settings live in one data volume, protected by
  your `HMAC_SECRET`.

---

## 🚀 Quick start

From a checkout of the [repository](https://github.com/Gill-Bates/rct-manager):

```sh
cp settings.env.example settings.env   # set HMAC_SECRET, e.g. openssl rand -base64 32
RCT_API_TAG=latest docker compose -f docker/compose.yaml up -d
```

Open `http://127.0.0.1:8000/` and log in with the one-time `admin` password from
the container log. Ports, volumes, reverse proxy and upgrades are covered in the
[Docker guide](https://gill-bates.github.io/rct-manager/getting-started/docker/)
and [Network and TLS](https://gill-bates.github.io/rct-manager/configuration/network/).

---

## 📚 Documentation

Everything in detail lives at **<https://gill-bates.github.io/rct-manager/>**:
[Quick Start](https://gill-bates.github.io/rct-manager/getting-started/quick-start/) ·
[Environment Variables](https://gill-bates.github.io/rct-manager/configuration/environment/) ·
[Energy Manager](https://gill-bates.github.io/rct-manager/energy-manager/) ·
[API](https://gill-bates.github.io/rct-manager/api/overview/) ·
[Troubleshooting](https://gill-bates.github.io/rct-manager/troubleshooting/) ·
[Changelog](https://github.com/Gill-Bates/rct-manager/blob/main/CHANGELOG.md)

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

Released under the [MIT License](https://github.com/Gill-Bates/rct-manager/blob/main/LICENSE).
Third-party components retain their respective licenses.

<p align="center">
  <a href="https://www.buymeacoffee.com/tnsteinerx">
    <img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20beer&emoji=%F0%9F%8D%BA&slug=tnsteinerx&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff" alt="Buy Me A Coffee">
  </a>
</p>
