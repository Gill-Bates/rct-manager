# Errors

Errors use RFC 9457 problem details with the media type
`application/problem+json`. The error key is stable; the text may change.
Validation problems carry an `errors` list with `parameter`, `code` and
`detail` per field.

| Key | Status | Meaning |
| --- | --- | --- |
| `missing_token`, `invalid_token` | 401 | Authorization header missing or token invalid |
| `insufficient_scope`, `write_not_allowed` | 403 | Role too low, or metric not approved for writing |
| `not_found`, `unknown_device`, `unknown_metric`, `write_disabled`, `docs_not_available` | 404 | Unknown path, device or metric; write support off; docs not released |
| `method_not_allowed` | 405 | Method not allowed for the path |
| `invalid_request` | 413 | Request body too large |
| `dispatch_not_found` | 404 | No battery dispatch operation is active |
| `metric_is_action`, `fresh_not_available_for_periodic_metric` | 409 | Wrong endpoint for an action; fresh read not possible |
| `dispatch_mode_unavailable`, `dispatch_limits_missing`, `dispatch_restore_required`, `dispatch_operation_conflict`, `dispatch_unverified`, `dispatch_capability_conflict` | 409 | Battery dispatch refused: mode not offered, power limits missing, previous state must be restored first, expected operation no longer active, hardware not verified, capability change during an active operation |
| `energy_manager_disarmed`, `energy_write_support_required`, `energy_action_unavailable` | 409 | Energy Manager switched off for the inverter, write access needed, action not available right now |
| `invalid_request`, `invalid_parameter`, `batch_too_large`, `fresh_batch_too_large`, `value_out_of_range`, `value_type_mismatch`, `value_not_finite`, `value_step_mismatch` | 422 | Request body or parameters invalid (`unknown_metric` is 422 for a name from `names`) |
| `rate_limited`, `device_budget_exhausted` | 429 | Request rate, failed-authentication limit or device work budget exceeded |
| `internal_error` | 500 | Unexpected server error |
| `device_unreachable`, `device_timeout`, `protocol_error`, `device_unavailable`, `write_outcome_unknown`, `action_outcome_unknown` | 502 | Device connection, answer or outcome problem |
| `queue_full`, `device_maintenance`, `not_ready` | 503 | Device queue full, device in maintenance, service or device not ready |
| `dispatch_snapshot_stale`, `dispatch_record_corrupt`, `dispatch_store_unavailable` | 503 | Dispatch snapshot not fresh enough to start; persisted dispatch state unreadable; durable store unavailable |
| `queue_timeout` | 504 | Request waited too long in the device queue |

The authoritative mapping is `app/api/problems.py`.
