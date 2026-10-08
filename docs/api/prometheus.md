# Prometheus Metrics

`GET /metrics` serves the Prometheus text format. A scrape never reads the
device; it exports cached values. With the metrics endpoint switched off (GUI **Prometheus** page or
`ENABLE_METRICS_ENDPOINT=false` as a first-start seed) `/metrics` answers 404.

By default a scrape needs `Authorization: Bearer <token>` with a `read` token.
The **Prometheus** page lets an administrator change this requirement and enter
trusted scrape peer IP addresses or CIDR networks. A trusted peer bypasses the
metrics token check; this uses the direct peer address, not a forwarded header.
Disabling the token requirement allows any peer that can reach `/metrics` to
scrape it. Keep the requirement enabled unless access is controlled by another
trusted layer.

The page also controls the scrape rate limit (1 to 10000 requests per window)
and window length (1 to 3600 seconds). Authentication, trusted peer, rate
limit, and endpoint on/off changes all apply immediately on save, without a
server restart. The limit still applies to trusted peers and token-free scrapes.

## What is exported

- Device values appear when selected on the administration **Prometheus** page
  and a non-expired value is cached. The packaged catalog supplies the initial
  preselected set.
  Text values are never exported.
- Registry units drive the metric name suffix (`_watts`, `_watt_hours`,
  `_volts`, `_amperes`, `_hertz`, `_celsius`, `_seconds`, ...). Changing a unit
  renames the exported metric.
- `# HELP` texts come from the registry `description`. A registry
  `prometheus_name` overrides the generated name.
- Enum values with registry labels (for example `inverter_state`) become one
  series per state with a `state` label (1 = current, 0 = others):

```text
rct_inverter_state{device="main",state="power_check"} 1
rct_inverter_state{device="main",state="feed_in"} 0
```

## Freshness

The numeric values selected on the **Prometheus** page (initially the preselected set) are
registered as periodic reads after every (re)connect. Two further groups are added even when they
are not exposed on `/metrics`: the energy-flow figures (battery SoC, grid, PV, house load and
battery power), which feed the dashboard graphic and the Energy Manager, and the values of the
dashboard cards (operating state, temperatures, battery status, cycles, SoC target, next
calibration date and the battery module serials). The energy-flow figures always get a slot: when
the selection is full, exposed metrics from its end are left out of the periodic reads (logged as a
warning). The card values only fill the room that is left. A value counts as fresh for at most three times the periodic
interval after its last update; a value that cannot be refreshed ages visibly
(`rct_device_metric_age_seconds`) and leaves `/metrics` after the grace period.
At most 64 values per device are possible; more refuse the start
(`too_many_periodic_metrics`).

!!! warning
    The periodic interval is device-global. Registering periodic reads sets it
    for the whole inverter and also affects other clients on the same device;
    it is reset on shutdown.

## Scraping with Telegraf

A collector such as Telegraf (`inputs.prometheus`) can write the series to a
database. With `metric_version = 1` a time-series database such as QuestDB
gets one table per Prometheus metric. Histogram bucket bounds such as `0.05`
or `+Inf` are not valid column names there; keep only `sum` and `count` of
`rct_device_request_duration_seconds`.
