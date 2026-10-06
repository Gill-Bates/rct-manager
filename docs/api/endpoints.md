# Endpoints

| Method | Path | Token | Purpose |
| --- | --- | --- | --- |
| GET | `/health` | no | Liveness; 503 once the shutdown has begun |
| GET | `/metrics` | yes by default | Prometheus text format, never touches a device |
| GET | `/docs`, `/openapi.json` | no | Swagger UI and OpenAPI document; only with documentation enabled (`DOCS_PUBLIC`, GUI **Settings**), otherwise 404 |
| GET | `/api/v1/readiness` | yes | Readiness per device |
| GET | `/api/v1/metrics` | yes | Metric list from the object registry |
| GET | `/api/v1/devices` | yes | Configured devices |
| GET | `/api/v1/devices/{device_id}/metrics` | yes | Several metrics, partial success |
| GET | `/api/v1/devices/{device_id}/metrics/{metric_name}` | yes | One metric |
| PUT | `/api/v1/devices/{device_id}/metrics/{metric_name}` | read/write | Write a metric (write access required) |
| POST | `/api/v1/devices/{device_id}/actions/{action_name}` | read/write | Trigger an action variable (write access required) |
| POST | `/api/v1/devices/{device_id}/battery/dispatch` | read/write | Start or atomically replace grid charging or load-following discharge |
| GET | `/api/v1/devices/{device_id}/battery/dispatch` | read/write | Read the current dispatch and restore state |
| DELETE | `/api/v1/devices/{device_id}/battery/dispatch` | read/write | Stop dispatch and restore the previous inverter settings |
| GET | `/api/v1/vendor/rct/objects`, `/transports`, `/devices/{device_id}/slaves` | read/write | RCT diagnostics |

`/metrics` (Prometheus) and `/api/v1/metrics` (metric list) are different on
purpose.

!!! note "Vendor diagnostics are off"
    The vendor routes answer 404 because the internal switch
    `ENABLE_VENDOR_DIAGNOSTICS` defaults to off and is not an operator
    setting. Likewise the write routes only exist with write access enabled in the GUI
    (404 `write_disabled` otherwise).

## Reading values

```sh
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8080/api/v1/devices/main/metrics?names=battery_soc,inverter_state"
```

- Without `names` the request returns all preselected metrics and is exempt
  from the batch limit; explicit `names` are limited to 32.
- A partial success answers 200 with an `errors` list; a batch without a single
  value fails with 502 `device_unavailable`.
- `fresh=true` forces a device read instead of using the cache. It requires an
  explicit `names` list (at most 8 entries); without `names` the request is
  rejected with 422 `invalid_request`. Periodically delivered metrics are read from the device
  as well (the internal `observe` mode); the 409 `fresh_not_available_for_periodic_metric` only
  exists in the internal `reject` mode, which is not an operator setting.
- `stale` and `stale_reason` mark a cached value that could not be refreshed.

## Writing values

```sh
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"value": 80}' \
  http://127.0.0.1:8080/api/v1/devices/main/metrics/<metric_name>
```

- Needs write access enabled in the GUI, a `read/write` token and a metric approved on the
  **Inverters** page (stored in `data/rct.db`).
- The body is `{"value": <scalar>}`; unknown fields are rejected.
- The value is always named in the unit the metric reports on read. For a metric the catalog
  scales (`battery_soc_target` is given in percent, the device holds a ratio) the gateway converts
  it back before sending, so `{"value": 80}` and `{"value": 80.0}` write the same value.
- The result reports `readback_value` and `confirmed`. If the value was sent
  but could not be confirmed, the request fails with 502
  `write_outcome_unknown`.
- Metrics that are actions answer 409 `metric_is_action`; use
  `POST /api/v1/devices/{device_id}/actions/{action_name}` with the same body.
  The protocol has no execution feedback, so `action_confirmed` is always
  `false` and the response carries an `action_note`.

## Readiness

`GET /api/v1/readiness` answers 200 with `ready: true` when every device is in a
ready state, and 503 `not_ready` (with a `devices` member) otherwise.

## Battery dispatch

Battery dispatch is available only when write support is enabled, all four `power_mng_*`
registers are approved on the **Inverters** page, and the device's required capabilities for the
requested mode are verified (or its engineering mode is on) — see
[Battery dispatch capabilities](../operation.md#battery-dispatch-capabilities). A device with an
unverified required capability answers `409 dispatch_unverified`, naming the missing
capabilities. Starting a dispatch when the device's control snapshot could not be read freshly
enough answers `503 dispatch_snapshot_stale`; retry. A persisted dispatch record that cannot be
read back (corrupt or incomplete) answers `503 dispatch_record_corrupt` for that device only —
other devices keep operating normally.

Grid charging:

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"charge_from_grid","target_soc_percent":80,"max_power_w":3000,"valid_until":"2026-10-05T23:00:00+02:00"}' \
  http://127.0.0.1:8080/api/v1/devices/main/battery/dispatch
```

Load-following discharge:

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"discharge_to_load","target_soc_percent":20,"max_power_w":5000,"valid_until":"2026-10-05T23:00:00+02:00"}' \
  http://127.0.0.1:8080/api/v1/devices/main/battery/dispatch
```

- `max_power_w` is an upper bound and is clamped to the configured device limit.
- `valid_until` is mandatory and must include a timezone offset. The service also caps it at six hours.
- A second `POST` replaces the active operation. Set `expected_operation_id` for compare-and-swap semantics.
- `DELETE` first requests 0 W and then restores the captured pre-dispatch register values.
- `FAULT_RESTORE_PENDING` means restoration could not be confirmed. New dispatches remain blocked until restore succeeds.
- `export_to_grid` is reserved but currently returns `409 dispatch_mode_unavailable`.
