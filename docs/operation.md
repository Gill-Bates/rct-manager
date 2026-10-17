# Operation

## One instance per inverter endpoint

Device access is serialized only inside one process with one HTTP worker. Several instances
against the same endpoint produce mixed or stale answers and there is no cross-instance lock: run
exactly one replica per device group.

## Exclusive access to port 8899

This service must be the only client of the inverter's port 8899; the vendor app, home automation
or any other RCT client counts as foreign access. Exclusivity is not enforced: a suspicion is
flagged (`foreign_access_suspected` in the readiness response, Prometheus gauge
`rct_transport_foreign_access_suspected`) and the service keeps serving.

## Maintenance state

When the inverter signals its bootloader, sending stops and requests for its devices get 503
`device_maintenance` until a cool-down has passed and a single read succeeds.

## Logs and health

- Logs are text on stdout; Compose rotates them (50 MB, 5 files).
- Access log level: GUI pages, assets, session polls and successful `/metrics` requests are
  `DEBUG`; mutating admin actions (login, password, settings, tokens), `/api/` and `/health` are
  `INFO`; any 4xx is `WARNING`, any 5xx `ERROR`. Exception: the Energy page's device poll, whose
  expected 503 while write support is off is `DEBUG`.
- The generated admin password appears in clear text in a boxed block on first start only (see
  [Authentication](configuration/authentication.md#administration-login)). Change it at first login.
- `GET /health` is token-free and turns 503 once shutdown has begun; the container health check
  queries it on the container's own address.

## Admin dashboard

- **Edit dashboard** opens edit mode on the **Overview** page (move and resize widgets, **Add
  widget**, **Reset layout**, **Done**); changes autosave. On a phone widgets are stacked and the
  layout cannot be edited (the saved layout stays untouched).
- Each inverter card shows the energy-flow graphic (from cached readings, also with write support
  off), a power card and one card per battery tower. A tower's module count comes from its
  populated module serial slots: "Detecting modules…" until all slots were read, "Module layout
  unclear" instead of a drawing for a pattern no documented tower has (e.g. seven slots, where a
  Power Battery 11.5 has six modules). A tower with N modules is drawn as top cap, N-1 middle parts
  and bottom part.
- The Energy Manager page shows a compact grid, house and battery energy-flow view beside the
  controls when space allows, from measured, sign-normalized readings.
- If the server is unreachable, the dashboard keeps the last data (up to one hour old, dimmed, with
  a banner); a button-less "Connection lost" dialog probes the server and reloads the page once it
  answers.
- The layout is stored in `data/rct.db`, separate from device settings, via
  `GET`/`PUT`/`DELETE /admin/api/dashboard-layout` (admin API, not public contract). The server
  accepts only known widget ids, at most 32 widgets and a 12-column grid.
- The About page checks GitHub for a newer release: results are cached for 1 h, errors for 60 s, and
  a forced refresh runs at most once per 30 s.
- `/admin/static/` answers 404 for any path with a dot-prefixed segment.

## Writes

- Write support is enabled on the GUI **Inverters** page; the `read/write` role and the write
  selection in `data/rct.db` still apply. A fresh database starts with an empty selection (the
  shipped catalog only lists what may be enabled): tick each metric to allow, including action
  objects such as `com_service`.
- "Documented in the catalog" is not "writable out of the box". The destructive `com_service` codes
  `6 erase_parameters_flash` and `11 erase_datalog` are not shipped as writable; codes
  `0, 5, 9, 10, 12, 13, 14, 15, 16, 18` are. To allow an erase action, copy the shipped file, add
  the code to its `allowed_values` and use it as the write allowlist (`write_allowlist_path` is not
  an environment setting).
- Numeric limits are wire type limits, not safe operating limits or a promise
  that the firmware accepts a write.
- The **Inverters** page shows a short explanation under a writable parameter where the catalog
  entry has a `help_text`. Most entries document only their vendor object path and intentionally
  have none rather than a guessed one; an own registry file can add `help_text` to any entry (the
  field is optional).
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
They are **per-device capabilities**, persisted in the dispatch database and set through the admin
dispatch API (`/admin/api/dispatch/...`, not part of the public contract).

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
- Verify the capability points on the real device with the project's internal hardware
  verification plan before enabling dispatch in production (not part of this site: a filled-in
  sheet holds operator measurements).
- **A removed device loses its dispatch state.** Removing a device (or re-addressing it under the
  same id) resets all its capabilities to `unverified`, switches engineering mode off and sets the
  Energy Manager mode to Off, so a device later added under that id starts unverified. The reset runs only
  after the new device graph was built.
- **A capability or device-limit change is refused while an operation it affects is active.**
  Flipping a sign convention, re-verifying the write path, or changing the power limits/
  engineering-mode switch of a device with a running dispatch answers `409
  dispatch_capability_conflict` and names the blocking operation, instead of taking effect live in
  the middle of an apply/control cycle. Stop or let the operation finish first.

A device stuck in `fault_restore_pending` (the restore to the original inverter settings itself
failed) is retried automatically by the dispatch cycle, instead of staying stuck until an operator
calls cancel/submit again. The delay between attempts doubles from 1 s up to 30 s; there is no
attempt limit. The Energy Manager admin status carries the failed attempts (`restore_attempts`) and
the next retry time (`next_restore_at`).

## Charge power limits

Grid charging is bounded by a safety clamp that can only ever **lower** the charge power, never
raise it. The effective charge power is `min(requested power, configured static device limit,
verified live limit)`. The static device limit is the verified maximum grid-charge power configured
for the device; the inverter and its BMS keep their own internal protection regardless.

The live-limit term is the **Live Charge Envelope** seam. On this build it is **wired but
unconditionally inert**: the RCT producer always reports "no live limit", so the configured static
device limit alone governs. A live limit will only ever constrain dispatch once a real per-device
hardware-verification contract exists for the register it would come from; until then no value is
derived or invented, and a missing, stale or unverified limit always falls back to the static bound.
A live limit, once a verified producer is attached, is additionally ignored when its measurement is
older than a conservative freshness bound, so a stale ceiling can never keep constraining dispatch.

## Protocol catalog

`app/catalog/objects.json` ships 894 of the 895 IDs from the supplied protocol PDF
v1.14 (without `wifi_password`). The shipped write-policy seed describes 849
scalar objects; the GUI manages actual write approvals in `data/rct.db`.
Existing friendly names stay stable; additional names replace dots and brackets with underscores. `net_slave_data`
remains a structured diagnostic read. Catalog and policy seed are packaged
application data; the two former project-root JSON files have been removed. The write reference
[rctpower_writesupport at 4a0d2e9](https://github.com/do-gooder/rctpower_writesupport/blob/4a0d2e9296b8d45abfaa1dfe437e6a7945b2b619/rct.py)
informed the ten supported parameters and the one-byte encoding of the 15 enum
objects.

