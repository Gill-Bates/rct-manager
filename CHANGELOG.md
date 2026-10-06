## [1.0.0] - 2026-10-06

- Initial Release

### Added

- The **Inverters** page explains what a writable parameter stands for: where the catalog
  documents a parameter, the explanation appears under the parameter name and is announced with
  the write-access checkbox. The search box matches the explanation as well. A parameter the
  catalog does not document stays without an explanation instead of carrying a guessed one.
- Battery dispatch now gates its hardware-specific write path on a per-device capability record
  instead of a flat environment default. A fresh install ships every capability as `unverified`,
  which blocks the dispatch mode it guards for that device until verified. Verifying one device
  never releases another device of the same model; the only transfer is an explicit
  `capabilities:copy-from` call that requires a matching model and firmware. A new per-device
  engineering mode, with a shorter TTL cap, is the only way to dispatch on unverified hardware. The
  capabilities are managed through a new admin API
  (`/admin/api/dispatch/devices/{device_id}/capabilities/...`), internal/engineering-only and
  deliberately excluded from the public, documented REST contract and the OpenAPI schema; its
  workflow is covered in [Operation](docs/operation.md#battery-dispatch-capabilities).

### Fixed

- A write to a metric the catalog scales took the scale into account only for a fractional body.
  An integer body such as `{"value": 80}` on `battery_soc_target` reached the device unscaled and
  therefore a factor of 100 too high, without an error or a warning. Integer and fractional bodies
  now write the same value.
- Battery dispatch's persisted record is now rejected (`503 dispatch_record_corrupt`, logged and
  skipped per device) instead of silently defaulted to a clean `idle` record when a required field
  is missing, a value is non-finite (`NaN`/`inf`), a persisted datetime is timezone-naive, or the
  state/intent/snapshot/`restore_required` combination is one the state machine cannot itself
  produce. `PowerSetpoint` and `ControlTelemetry` now reject non-finite values at construction, so
  a `NaN` reading can no longer bypass the stale-telemetry check or reach a hardware setpoint.
  `DeviceLimits` and `DispatchConfig` now validate their own finite/positive/consistent domain
  invariants instead of relying only on the HTTP schema.
- A device's control snapshot read with `all_fresh=false` (a real, reachable outcome of the RCT
  adapter) is no longer silently accepted as the basis of a new dispatch, and a persisted snapshot
  missing the `all_fresh` key now defaults to not-fresh instead of fresh. Starting a dispatch on a
  stale snapshot now answers `503 dispatch_snapshot_stale`.
- A capability or device-limit write (sign convention, write-path verification, power limits,
  engineering-mode switch) is now refused with `409 dispatch_capability_conflict` while an
  operation it affects is active on the same device, closing a time-of-check/time-of-use window
  between the former admin-layer check and the actual write, and closing a bypass where setting a
  capability to `verified` was never checked against an active operation at all. The guard now
  runs under the controller's per-device lock instead of in the admin layer.
- A device stuck in `fault_restore_pending` after a failed restore attempt is now retried
  automatically on the next dispatch cycle once eligible, instead of staying stuck until an
  operator calls cancel/submit again. This is a minimal, immediate-retry mechanism without a
  backoff curve or attempt limit.

### Security

- **Breaking:** `DISPATCH_SOC_STRATEGY_EXTERNAL_CODE`, `DISPATCH_BATTERY_DISCHARGE_POSITIVE` and
  `DISPATCH_GRID_IMPORT_POSITIVE` are no longer settings. They are superseded by the per-device
  capability record above; a value set for any of the three is now ignored with a `WARNING`
  naming the variable. Re-enter the hardware-verified values through the admin dispatch API.
- The shipped write allowlist no longer offers the destructive `com_service` actions
  `6 erase_parameters_flash` and `11 erase_datalog`; writing them is rejected even when the
  action is enabled. The codes `0, 5, 9, 10, 12, 13, 14, 15, 16, 18` stay available. To allow an
  erase action deliberately, copy the shipped allowlist, add the code to its `allowed_values` and
  set `write_allowlist_path` to the copy.

<details markdown="1">
<summary>Previous versions...</summary>
