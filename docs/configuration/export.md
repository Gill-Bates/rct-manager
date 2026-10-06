# Push Export

The service can push its metrics to a time-series database. Supported targets:

| Target | Notes |
| --- | --- |
| Prometheus | Existing `GET /metrics` endpoint, toggled on the GUI **Prometheus** page; independent of the push export |
| InfluxDB 2 | Writes to `/api/v2/write` with organization, bucket and token |
| QuestDB OSS 10.x | Writes to `/write` (ILP over HTTP); HTTP Basic Auth only |

Configure the target on the GUI **TSDB** page (secrets are write-only). Without a database type
nothing is exported. A change requires a restart. The variables `DB_TYPE=influxdb_v2` or
`DB_TYPE=questdb` and the `INFLUXDB_*` / `QUESTDB_*` names listed in
[Environment Variables](environment.md) only seed the very first start.

## What is exported

Every `interval` seconds (`METRICS_EXPORT_INTERVAL_SECONDS`) the samples that `/metrics`
shows are written as line protocol. Samples with the same labels form one point: the labels
(`device`, `endpoint`, `state`, `metric`) become tags and each series name becomes a float field.
Histogram buckets are not exported. Export health is part of `/metrics`
(`rct_export_pushes_total`, `rct_export_last_success_timestamp_seconds`).

## Failures

An unreachable or rejecting database never affects the REST API or device polling. A failed
push is dropped (the next one carries current values), the delay doubles up to 300 s, and the
log shows the first failure and then each power of two, plus the recovery.

## QuestDB retention and downsampling

- `QUESTDB_DOWNSAMPLING=off` (the default) creates no rollup and applies `QUESTDB_RETENTION_DAYS`
  to the raw table. `0` keeps all data.
- `QUESTDB_DOWNSAMPLING=manual` creates no rollup and applies only the explicit
  `QUESTDB_RAW_RETENTION_DAYS` to the raw table. This value is required;
  `QUESTDB_RETENTION_DAYS` is ignored in this mode.
- `QUESTDB_DOWNSAMPLING=low` (1 min rollup, 30 days raw), `medium` (1 min, 7 days) or
  `high` (5 min, 1 day) creates a materialized view named from its interval and current column
  signature. `QUESTDB_RETENTION_DAYS` sets the view TTL.
- With a rollup, `QUESTDB_RAW_RETENTION_DAYS` overrides the preset raw days and must not exceed
  the total retention unless the total is unlimited (`0`).
- The view is created after the first data arrived and covers the columns present at that time.
- A TTL that already exists and was not set by this service is never replaced.
  The raw TTL is only applied once the rollup view is refreshed.
