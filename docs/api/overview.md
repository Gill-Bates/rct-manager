# API Overview

The REST API is versioned under `/api/v1`. Responses are JSON; errors follow RFC 9457 problem
details (`application/problem+json`, see [Errors](errors.md)). Every response, including `/health`,
`/metrics`, `/docs` and errors, carries `Cache-Control: no-store`.

## Authentication

Send `Authorization: Bearer <token>` on every request except `GET /health`; see
[Authentication](../configuration/authentication.md).

## Interactive documentation

With `DOCS_PUBLIC=true` Swagger UI (`/docs`) and the OpenAPI document (`/openapi.json`) are served
without a token; both count against the rate limit.

## Vendor-neutral contract

Paths, error keys and status codes do not depend on the vendor; another adapter fills the same
fields (port: `app/gateway/base.py`, see [Architecture](../development/architecture.md)).
Vendor-specific data (object ids, protocol types, frame counters, slave discovery) lives only under
`/api/v1/vendor/rct/`.

| Resource | Fields |
| --- | --- |
| Metric descriptor | `name`, `unit`, `value_type` (`boolean`, `integer`, `number`, `string`, `enum`, `object`), `writable`, `preselected` |
| Device | `device_id`, `display_name`, `role` (`master`, `slave`, `standalone`), `ready` |
| Metric value | `name`, `value`, `unit`, `timestamp` (UTC), `age_seconds`, `stale`, `source` (`device`, `cache`); optional `stale_reason`, `freshness`, `enum_value`, `enum_label` |
| Write result | `device_id`, `name`, `written_value`, `readback_value`, `confirmed`, `send_unconfirmed`, `timestamp` |
| Action result | `device_id`, `name`, `requested_value`, `readback_value`, `action_confirmed` (always `false`), `action_note`, `timestamp` |
| Readiness | `device_id`, `state`, `last_success_at`, `last_heartbeat_at`, `consecutive_failures`, `queue_length`, `foreign_access_suspected`, `liveness_source`, `transactions`, `failures`, `cache_hits`, `cache_misses`, `periodic_available` (`null`: periodic reads not configured), `periodic_setup_failures` |
