# API Overview

The REST API is versioned under `/api/v1`. Responses are JSON; errors follow
RFC 9457 problem details (`application/problem+json`, see [Errors](errors.md)).
Every response, including `/health`, `/metrics`, `/docs` and error responses,
carries `Cache-Control: no-store`.

## Authentication

Send `Authorization: Bearer <token>` on every request except `GET /health`.
See [Authentication](../configuration/authentication.md) for roles and the
personal access tokens created in the GUI.

## Interactive documentation

With `DOCS_PUBLIC=true` the service serves Swagger UI at `/docs` and the
OpenAPI document at `/openapi.json` without a token. Both count against the
request rate limit.

## Vendor-neutral contract

Paths, error keys and status codes do not depend on the inverter vendor. A
different device adapter must fill the same fields; the adapter port is
`app/gateway/base.py` (see [Architecture](../development/architecture.md)).
Vendor-specific data (object ids, protocol types, frame counters, slave
discovery) lives only under `/api/v1/vendor/rct/`.

| Resource | Fields |
| --- | --- |
| Metric descriptor | `name`, `unit`, `value_type` (`boolean`, `integer`, `number`, `string`, `enum`, `object`), `writable`, `preselected` |
| Device | `device_id`, `display_name`, `role` (`master`, `slave`, `standalone`), `ready` |
| Metric value | `name`, `value`, `unit`, `timestamp` (UTC), `age_seconds`, `stale`, `source` (`device`, `cache`); optional `stale_reason`, `freshness`, `enum_value`, `enum_label` |
| Write result | `device_id`, `name`, `written_value`, `readback_value`, `confirmed`, `send_unconfirmed`, `timestamp` |
| Action result | `device_id`, `name`, `requested_value`, `readback_value`, `action_confirmed` (always `false`), `action_note`, `timestamp` |
| Readiness | `device_id`, `state`, `last_success_at`, `last_heartbeat_at`, `consecutive_failures`, `queue_length`, `foreign_access_suspected`, `liveness_source`, `transactions`, `failures`, `cache_hits`, `cache_misses` |
