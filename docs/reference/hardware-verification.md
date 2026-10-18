# Hardware Verification

Which device behaviours are confirmed on real hardware, which are assumed, and what that means for
the service. This page is the single place for verification status; other pages link here instead of
repeating it.

| Status          | Meaning                                                                              |
| --------------- | ------------------------------------------------------------------------------------ |
| **Verified**    | Confirmed by a measurement on a real inverter. The service relies on it.             |
| **Per-device**  | Verified by an operator per inverter in the admin GUI; blocked until then.           |
| **Observed**    | Seen on hardware and handled defensively, but not a guaranteed contract.             |
| **Assumed**     | A documented working assumption; the service behaves conservatively.                 |
| **Unverified**  | No hardware evidence. The dependent feature is off, blocked or marked as unverified. |
| **Unsupported** | Not decoded or not used.                                                             |

## Verification matrix

### Consumption history

| Feature or assumption                                                                                           | Status      | Evidence and consequence                                                                                              |
| --------------------------------------------------------------------------------------------------------------- | ----------- | --------------------------------------------------------------------------------------------------------------------- |
| `logger_day_eload_log_ts` unit and day mapping                                                                  | Verified    | Wh per local calendar day (A/B measurement below). The daily backfill is always on.                                   |
| Day-point timestamps                                                                                            | Verified    | Local wall-clock fields, labelled UTC.                                                                                |
| `before=` query boundary                                                                                        | Observed    | A/B on the tested inverter (2026-10-18) returned the boundary point only for local wall-clock fields encoded as UTC; do not generalize to other devices without checking. |
| Repeated 23:59:59 logger points                                                                                 | Observed    | The logger may repeat one day's last point 8 to 16 times with values differing by under 1 Wh; the largest value wins. |
| Other logger registers (for example `logger_minutes_eload_log_ts`)                                              | Unverified  | Grid and unit not clarified; not used anywhere.                                                                       |
| Year and month history (`logger_year_eac_log_ts`, `logger_month_eac_log_ts`) for the battery commissioning date | Unverified  | Query contract not confirmed on hardware. Automatic derivation stays disabled; an operator-entered date always works. |
| Event tables (`logger_error_log_time_stamp`)                                                                    | Unsupported | Classified read-only; the wire format is not decoded and neither unit nor timestamp semantics are verified.           |
| Day counter reset instant and time zone                                                                         | Unverified  | Only "non-decreasing between resets" is assumed.                                                                      |
| Total counter reset or overflow behaviour (`energy_e_load_total`)                                               | Unverified  | Resets, deltas above 50 kW, stalled counters and gaps over 24 h are rejected and counted.                             |
| Whether the load counter includes external power                                                                | Unverified  | Not assumed either way.                                                                                               |

### Battery dispatch

| Feature or assumption                                                   | Status      | Evidence and consequence                                                                                                                                    |
| ----------------------------------------------------------------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Battery dispatch write path (`write_path_convention`)                   | Per-device  | Needs the SoC strategy code, enum and bool byte widths, frame layout and apply sequence. Dispatch stays blocked until verified, except in engineering mode. |
| Battery and grid power sign conventions                                 | Per-device  | Verified together with the write path; defaults are assumptions.                                                                                            |
| Hold behaviour (`hold`)                                                 | Unverified  | Not demonstrated on the unit. The same capability gate applies. See [Battery Control](../energy/battery-control.md#hold-status).                            |
| Export limit convention                                                 | Per-device  | Not part of the verification form; matters only with `limit_export_during_discharge` (off by default).                                                      |
| Live charge limit register                                              | Unsupported | The producer always reports no live limit; the static device limit governs.                                                                                 |
| Reference of `power_mng_battery_power_extern` (battery side or AC side) | Assumed     | Plan assumes battery-side, `power_reference_verified: false`.                                                                                               |
| Charge efficiency (90 %) and preferred power range                      | Assumed     | Planning assumptions, not device properties.                                                                                                                |

### Battery capacity

| Feature or assumption                                                                                   | Status      | Evidence and consequence                                                                                         |
| ------------------------------------------------------------------------------------------------------- | ----------- | ---------------------------------------------------------------------------------------------------------------- |
| Capacity ratio `power_mng_amp_hours_measured` / `battery_ah_capacity`                                   | Unsupported | Hardware reads varied from 25.0/23.8531 Ah at 79–87 % SoC to 16.9251/8.65005 Ah near 2.5 % SoC. No automatic current capacity or calibration point is released. |
| Ah register readability on RCT DC 10.0, firmware 2.3.5689                                              | Observed    | Both registers returned values during the 2026-10-17 characterization run. This does not validate their usable-Wh semantics. |
| Per-tower versus device-wide meaning of `battery_placeholder_0_ah_capacity` and other battery registers | Unverified  | Unsupported tower combinations stay unavailable.                                                                 |
| `battery_soh` scale and `power_mng_battery_type` codes                                                  | Unsupported | Not used.                                                                                                        |

### Protocol

| Feature or assumption                                                                                  | Status     | Evidence and consequence                                                                                    |
| ------------------------------------------------------------------------------------------------------ | ---------- | ----------------------------------------------------------------------------------------------------------- |
| `net.slave_data` decoding                                                                              | Verified   | One device, firmware 2.3.5687: 108 bytes, all numeric fields little-endian.                                 |
| CRC escaping, frame layout of commands `0x08`/`0x48`, byte width of `t_enum`/`t_bool`, string encoding | Unverified | Documented as not yet verified against a real inverter; verify before production use.                       |
| Origin of unsolicited response frames                                                                  | Unverified | Working hypothesis only; see [Transport Diagnostics](transport-diagnostics.md#unsolicited-response-frames). |

## Device-specific characterization: 2026-10-17

Device: RCT DC 10.0 Steiner, firmware 2.3.5689. These observations apply to this device and run.

| Observation | Status | Evidence and limit |
| ----------- | ------ | ------------------ |
| Primary tower topology | Observed | Three inventory passes found six populated module serial slots (0–5); slot 6 was empty. |
| Second tower | Observed | Classified absent during this run. |
| `power_mng_n_batteries` | Observed | Read as `1`, consistent with the observed single tower. |
| Ah registers | Observed | `power_mng_amp_hours_measured` read as 23.8530769348 Ah and `battery_ah_capacity` as 25.0 Ah. Both reads succeeded; later readings at low SoC differed, so their ratio is not an established usable-capacity health factor. |
| Normal reads | Observed | `battery_soc_target` and `household_load_power` succeeded at approximately T+5, T+30, T+60 and T+300 seconds: eight successful reads without a reconnect. No instability was reproduced in this five-minute run. |
| `READ_PERIODICALLY` registration | Not exercised in these runs | The probe ran read-only (`invasive = false`, `registration_probes` empty, "No periodic registration was made"), so no registration was attempted; this run neither confirms nor denies a registration failure. |
| Periodic push delivery and subscription persistence | Unverified | The probe did not write `pas.period`, observe subsequent push cycles or test a reconnect. |

The 23.8530769348/25.0 Ah ratio would make the six-module rated baseline approximately 9.89 kWh
if used as a health factor. That calculation alone is not an independent charge/discharge capacity
measurement or proof of the registers' physical meaning; the service does not release that ratio as
automatic current capacity.

## Day-history query-boundary A/B measurement

Boundary 2026-10-16, `Europe/Berlin`, read-only (`tools/probe_consumption_history.py`):

| Run | `before=` value | Meaning                            | 2026-10-15 day point |
| --- | --------------- | ---------------------------------- | -------------------- |
| A   | `1792101600`    | True UTC instant of local midnight | missing              |
| B   | `1792108800`    | Local wall-clock encoded as UTC    | present              |

The boundary point was absent in A and present in B on the repeat run of 2026-10-18; its value,
18018.6797 Wh, matches the operator's export. This confirms the local-wall-clock query encoding on
the tested inverter only. The production history provider already encodes local clock fields as a
UTC-labelled query instant. See
[Consumption](../energy/consumption.md#logger-semantics-hardware-observations) for current usage.

## Characterization run: 2026-10-17

Device-specific evidence from a single read-first run. It characterizes one unit and one firmware;
it does not generalize to every RCT DC 10.0 or every firmware.

- **Device:** RCT DC 10.0 Steiner
- **Firmware:** 2.3.5689
- **Mode:** read-only (`tools/diagnostics/rct_battery_probe.py`)

### Battery topology (this device)

| Observation                                                       | Status                      |
| ----------------------------------------------------------------- | --------------------------- |
| Primary tower present with six populated module serial slots (0–5) | Observed on this device     |
| Primary tower slot 6 empty; three inventory passes                | Observed on this device     |
| Placeholder tower absent (no populated module serials)            | Observed on this device     |
| `power_mng_n_batteries = 1`                                       | Consistent with the observed topology |

The placeholder tower reports `present = false` with `module_count = null` and
`module_count_status = pending`; `present` is the authoritative signal here. `pending` on an absent
tower means "no module serials seen", not "a scan is still running"; the regular dashboard filters
absent towers out, so it never shows a scan state for one.

### Capacity registers (this device)

`power_mng_amp_hours_measured ≈ 23.853 Ah` and `battery_ah_capacity = 25.0 Ah` were both read
successfully. One read of `power_mng_amp_hours_measured` later in the same run timed out; the
cache freshness and retry handling absorb such a single timeout. With six modules the derived usable
energy is `6 × 1.92 kWh × 0.9 × min(1, 23.853 / 25) ≈ 9.89 kWh`, which matches the value the service
shows. This confirms the registers are readable and the formula is applied correctly on this device;
it is not an independent capacity calibration.

### Normal reads (this device)

Over a five-minute window, `battery_soc_target` and `household_load_power` were read at roughly
T+5 s, T+30 s, T+60 s and T+300 s — eight reads, all successful, with no reconnect. This run
reproduced no normal-read instability; it does not prove normal reads are universally stable. The
probe classifies the run as "no problem reproduced in this run", and one run is not a decision.

### `READ_PERIODICALLY`: registration versus push delivery

Registration and actual push delivery are distinct behaviours and must not be conflated.

The probe artifacts for this characterization run (`rct_battery_probe.py`) were taken in the default
read-only mode: `periodic_status.registration_probes` is empty and the run records
"No periodic registration was made (default mode)" with `invasive = false`. The configured default
set it reports (`soc_target`, `household_load_power`) is the intended periodic object set, not an
executed registration. These runs therefore establish only stable normal reads (above); they do not
exercise `READ_PERIODICALLY` registration, and periodic behaviour is explicitly "not tested in this
mode".

| Behaviour                                                                 | Status                         |
| ------------------------------------------------------------------------- | ------------------------------ |
| `READ_PERIODICALLY` registration for the two probed objects               | Not exercised in these runs    |
| Actual periodic pushes after setting `pas.period`                         | Unverified                     |
| Persistence of a subscription across reconnect or session                 | Unverified                     |

Actual periodic push delivery was never exercised: it needs a `pas.period` write, which the
read-only tool does not send. The working hypothesis about persisting subscriptions stays open; see
[Transport Diagnostics](transport-diagnostics.md#unsolicited-response-frames).

## Verifying a device

- **Dispatch capabilities:** per device in the admin GUI (Expert mode), with measured evidence and a
  note; see [Battery Control](../energy/battery-control.md#hardware-verification-form-admin-api).
  Verifying one device never releases another.
- **Logger history:** run the read-only probe with the application stopped:
  `python tools/probe_consumption_history.py --host <DEVICE_HOST> --port 8899 --pages 8`. It sends
  only logger history queries (a write frame on the wire that stores nothing) and never writes the
  device address into its output.
- **Production use:** the service is the only intended client of port 8899; see
  [Operation](../operation.md#exclusive-access-to-port-8899).
