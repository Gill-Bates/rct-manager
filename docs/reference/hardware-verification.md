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
| `before=` query boundary                                                                                        | Verified    | Must be encoded as local device wall-clock time on the wire. A true UTC boundary drops the day point.                 |
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
| Capacity ratio `power_mng_amp_hours_measured` / `battery_ah_capacity`                                   | Assumed     | Used only as a ratio against the rated module baseline. A comparison with the RCT app's usable kWh is still due. |
| Per-tower versus device-wide meaning of `battery_placeholder_0_ah_capacity` and other battery registers | Unverified  | Unsupported tower combinations stay unavailable.                                                                 |
| `battery_soh` scale and `power_mng_battery_type` codes                                                  | Unsupported | Not used.                                                                                                        |

### Protocol

| Feature or assumption                                                                                  | Status     | Evidence and consequence                                                                                    |
| ------------------------------------------------------------------------------------------------------ | ---------- | ----------------------------------------------------------------------------------------------------------- |
| `net.slave_data` decoding                                                                              | Verified   | One device, firmware 2.3.5687: 108 bytes, all numeric fields little-endian.                                 |
| CRC escaping, frame layout of commands `0x08`/`0x48`, byte width of `t_enum`/`t_bool`, string encoding | Unverified | Documented as not yet verified against a real inverter; verify before production use.                       |
| Origin of unsolicited response frames                                                                  | Unverified | Working hypothesis only; see [Transport Diagnostics](transport-diagnostics.md#unsolicited-response-frames). |

## A/B measurement: day-history query boundary

Boundary 2026-10-16, `Europe/Berlin`, read-only (`tools/probe_consumption_history.py`):

| Run | `before=` value | Meaning                            | 2026-10-15 day point |
| --- | --------------- | ---------------------------------- | -------------------- |
| A   | `1792101600`    | True UTC instant of local midnight | missing              |
| B   | `1792108800`    | Local wall-clock encoded as UTC    | present              |

The 2026-10-15 value, 18018.7 Wh, matches the operator's export. The device therefore compares the
query boundary against local wall-clock fields; the service encodes it that way. Consequence and
usage: [Consumption](../energy/consumption.md#logger-semantics-hardware-verified).

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
