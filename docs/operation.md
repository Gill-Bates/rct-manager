# Operation

## One instance per inverter endpoint

The serialization of device access is guaranteed only inside one process with
one HTTP worker. Several instances against the same inverter endpoint violate
it and produce mixed or stale answers. Run exactly one replica per device
group; the service has no cross-instance lock.

## Exclusive access to port 8899

Make sure this service is the only client of the inverter's port 8899. The
vendor app, a home automation integration or any other RCT client counts as
foreign access. The service does not enforce exclusivity; it flags a suspicion
(`foreign_access_suspected` in the readiness response and the Prometheus gauge
`rct_transport_foreign_access_suspected`) and keeps serving.

## Maintenance state

When the inverter signals its bootloader, sending stops and requests for its
devices get 503 `device_maintenance` until a cool-down has passed and a single
read succeeds.

## Logs and health

- Logs are text on stdout; Compose rotates them (50 MB, 5 files).
- The access log level depends on the request: GUI pages, assets, session polls and successful
  `/metrics` requests are `DEBUG`; mutating admin actions (login, password, settings, tokens),
  `/api/` and `/health` requests are `INFO`; any 4xx is `WARNING` and any 5xx is `ERROR`.
- The generated admin password is never logged. The boxed block printed to stdout after the
  server has started shows it in clear text (a deliberate operator choice, so it can be copied
  straight from the console on first start), and it is also saved to `initial-admin-password`
  next to the admin database (`data/initial-admin-password` by default, mode `0600`) as a
  fallback for runs without a visible console. Change the password at first login, then delete
  the file.
- `GET /health` is token-free and turns 503 once the shutdown has begun. The
  container health check queries it on the container's own address.

## Admin dashboard

- **Edit dashboard** opens the edit mode of the **Overview** page (move and resize widgets, **Add
  widget**, **Reset layout**, **Done**). Changes autosave.
- The layout is stored in `data/rct.db`, separate from the device settings, through
  `GET`/`PUT`/`DELETE /admin/api/dashboard-layout` (admin API, not part of the public contract). The
  server accepts only known widget ids, at most 32 widgets and a 12-column grid.
- The About page checks GitHub for a newer release: results are cached for 1 h, errors for 60 s, and
  a forced refresh runs at most once per 30 s.
- `/admin/static/` answers 404 for any path with a dot-prefixed segment.

## Writes

- Write support is enabled on the GUI **Inverters** page; the `read/write` role and the
  write selection stored in `data/rct.db` still apply. A fresh database starts with an
  empty selection: the shipped catalog only lists what may be enabled, so tick each
  metric you want to allow, including action objects such as `com_service`.
- The shipped allowlist separates "documented in the catalog" from "writable out of the box": a
  labeled action is not automatically a safe default. The destructive `com_service` codes
  `6 erase_parameters_flash` and `11 erase_datalog` are therefore not shipped as writable; the
  codes `0, 5, 9, 10, 12, 13, 14, 15, 16, 18` are. To allow an erase action anyway, copy the
  shipped file, add the code to its `allowed_values` and point `write_allowlist_path` at the copy.
- Numeric limits are wire type limits, not safe operating limits or a promise
  that the firmware accepts a write.
- The **Inverters** page shows a short explanation under a writable parameter where the catalog
  carries one in the entry's `help_text`. Most entries document only their vendor object path, so
  they intentionally stay without an explanation rather than getting a guessed one; an own registry
  file can add `help_text` to any entry. The field is optional, so a registry written without it
  keeps loading.
- `wifi_password` is deliberately excluded: secrets are neither readable nor
  writable, and a registry or allowlist that lists it aborts the start.
- Writing `pas_period` can change periodic polling.
- Repeating an action requires resetting it to 0 first.

## Measured values

- Subnormal `t_float` readings preserve their IEEE 754 value and sign in the decoder.
- Battery power, voltage and current are separate device variables; the power
  need not equal voltage times current.
- Grid feed-in energies are passed through as the device sends them; the
  protocol defines no sign.

## Verification status

Protocol assumptions (CRC escaping, frame layout of commands `0x08`/`0x48`,
byte width of `t_enum`/`t_bool`, string encoding) are not yet verified against
a real inverter; do that before production use.
`net.slave_data` is verified on one device with firmware 2.3.5687: 108 bytes,
all numeric fields little-endian.

## Battery dispatch capabilities

The hardware-specific parts of battery dispatch (the external-control strategy code and the
sign conventions for battery power and grid power) are not configured through the environment.
They are **per-device capabilities**, persisted in the dispatch database and shown and set through
the admin dispatch API (`/admin/api/dispatch/...`, not part of the public, documented contract).

- **Each capability row carries only the evidence that governs it.** The dispatch adapter reads every
  evidence field from exactly one capability, so `GET .../capabilities` reports it on that row only
  and `null` on all others — `null` means "not applicable to this capability", never "unset":
    - `battery_power_sign_convention` → `battery_discharge_positive`
    - `grid_power_sign_convention` → `grid_import_positive`
    - `write_path_convention` → `soc_strategy_external_code`

  A `PUT` that sets an evidence field on a capability that does not govern it is refused with `422`
  instead of storing a value the adapter would ignore. The same rule covers the remaining evidence
  fields: `enum_byte_width`, `bool_byte_width`, `write_frame_layout_verified` and
  `apply_sequence_verified` belong to `write_path_convention`, and
  `export_limit_zero_blocks_export` to `export_limit_convention`. `note` is the exception — every
  capability may carry one.

- **`PUT` replaces the capability record; it is not a partial update.** An omitted evidence field is
  reset to its assumption default rather than kept, so a value that was set earlier is lost unless
  the request sends it again. Read the capability first and resend the fields you want to keep.

- `soc_strategy_external_code` is returned as the raw register value, since the RCT register catalog
  carries no enum label table for it (the meaning is device/firmware-specific, not a project-wide
  constant). It is only interpretable together with `note`, so setting `write_path_convention` to
  `verified` is refused with `400` unless the `PUT` supplies a non-empty `note`; record there what
  the code means and what was observed (hardware model, firmware, behavior).

- **Shipping state is `unverified`.** A fresh database ships every capability as `unverified` with
  assumption defaults the adapter works with (for example `battery_discharge_positive = true`,
  `grid_import_positive = true`). An `unverified` required capability blocks the dispatch mode it
  guards, for that device only.
- **Verification is per device, not per model.** Verifying device A never releases device B, even
  of the identical model and firmware. The only supported transfer is an explicit
  `capabilities:copy-from` call naming the source device and the target model/firmware; it copies
  only capabilities whose evidence matches, and logs each copied row at `WARNING`. A firmware
  update invalidates the capabilities it affects; re-verify after updating.
- **Engineering mode is the only way to dispatch on unverified hardware.** It is a per-device
  switch (`PUT /admin/api/dispatch/devices/{device_id}`), carries a shorter TTL cap than normal
  operation, and still refuses the write-path capability without a strategy code — there is no
  way to form the write at all without one.
- Work through the capability points on the real device using the project's internal hardware
  verification plan before enabling dispatch in production; it is not part of this documentation
  site, because a filled-in sheet holds operator measurements that belong with the operator, not
  on a published page.
- **A removed device loses its dispatch state.** Removing a device (or re-addressing it under the
  same id) resets all its capabilities to `unverified`, switches engineering mode off and disarms the
  Energy Manager, so a device later added under that id starts unverified. The reset runs only
  after the new device graph was built.
- **A capability or device-limit change is refused while an operation it affects is active.**
  Flipping a sign convention, re-verifying the write path, or changing the power limits/
  engineering-mode switch of a device with a running dispatch answers `409
  dispatch_capability_conflict` and names the blocking operation, instead of taking effect live in
  the middle of an apply/control cycle. Stop or let the operation finish first.

A device stuck in `fault_restore_pending` (the restore to the original inverter settings itself
failed) is retried automatically on the next dispatch cycle once eligible, instead of staying
stuck until an operator calls cancel/submit again. This is a minimal, immediate-retry mechanism
with no backoff curve or attempt limit yet; a full backoff schedule is a separate, not yet
implemented work package.

## Protocol catalog

`app/catalog/objects.json` ships 894 of the 895 IDs from the supplied protocol PDF
v1.14 (without `wifi_password`). The shipped write-policy seed describes 893
scalar objects; the GUI manages actual write approvals in `data/rct.db`.
Existing friendly names stay stable; additional names replace dots and brackets with underscores. `net_slave_data`
remains a structured diagnostic read. Catalog and policy seed are packaged
application data; the two former project-root JSON files have been removed. The write reference
[rctpower_writesupport at 4a0d2e9](https://github.com/do-gooder/rctpower_writesupport/blob/4a0d2e9296b8d45abfaa1dfe437e6a7945b2b619/rct.py)
informed the ten supported parameters and the one-byte encoding of the 15 enum
objects.

