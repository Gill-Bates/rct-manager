## [1.2.3] - 2026-xx-xx


<details markdown="1">
<summary>Previous versions...</summary>

## [1.2.2] - 2026-10-18

### New

- Optional dashboard widgets now show battery capacity, remaining energy, cycles, calibration and commissioning dates, and degradation history per inverter; unavailable calibration data remains clearly empty.
- Expert diagnostics show battery calibration history, and the Charging plan reports the battery's current remaining energy.
- The default Charging schedule now lists only grid-charging windows; **Show all intervals** reveals the full slot history, with action-specific tooltips and readable reason labels.
- Documentation pages now include a **Print / PDF** control.
- The consumption chart legend lets you show or hide its data series.

### Fix

- Battery topology reports distinguish absent towers from scans still in progress. Because the Ah readings vary with battery state, automatic usable capacity stays unavailable until a stable calibration can be identified; legacy Ah-ratio rows are no longer treated as degradation history.
- Charging plans now respect the SoC trajectory at the first future slot, stop at complete 15-minute slots within the requested horizon, and discard stale plans when their inputs change.
- Consumption forecasts now exclude estimated and thinly covered readings from their learned profile instead of treating them as reliable measurements.
- Daily consumption backfill now sends logger query boundaries using local device wall-clock fields, so the latest completed local day is not skipped.
- When every inverter is Off, spot-market and weather refreshes and plan calculation pause while stored data is kept. Control buttons are disabled; the planning tabs appear only while at least one inverter is in Manual mode.
- A missing battery capacity now shows an immediate explanation while the plan continues checking in the background; genuinely pending price, solar, or consumption inputs show a wait state, while disabled inputs still tell the operator what to enable.
- Plans without configured power limits use a 3 kW charge and discharge fallback; Expert mode can set per-inverter limits.
- Spot prices and bidding zones load more reliably, and prices are used only after a full local day is available. Location search no longer takes a timezone or postal code from a nearby or partial match.
- The **Add widget** dialog now preserves keyboard focus and closes with Escape.

### Security

- The server enforces the one-time usage disclaimer before any admin request, including requests authenticated with personal access tokens.
- Low-level battery dispatch mutations are refused while the Energy Manager owns the inverter, so dispatch cannot bypass the active control mode.
- Admin cookie sessions now have a configurable 0–7 day server-side lifetime (1 day by default); logout and password changes invalidate sessions.
- Admin GUI polling has a separate per-session request budget from API tokens, pauses in hidden tabs, and backs off according to `Retry-After` after a 429 response.

## [1.2.1] - 2026-10-16

### New

- Configure an Open-Meteo solar forecast with location search and a production-curve preview, plus an optional dashboard widget.
- Preview a read-only dynamic charging plan built from spot prices, solar, consumption history, battery state and per-inverter limits; it never sends battery commands.
- The Charging plan combines 60 days of household consumption history with a time-series charging schedule and plain-language slot reasons.
- Battery capacity can be based on module count and the RCT datasheet, with an Expert override; the plan remains unavailable when neither source is qualified.
- An optional Charging schedule dashboard widget shows the first inverter's read-only plan and refreshes every five minutes.

### Fix

- Single-tower capacity no longer stays unavailable because an empty module slot did not answer; module counts use populated serial slots.
- Pending plans retry automatically and distinguish inputs that are still loading from settings that need operator action. The consumption section stays hidden until data is available.
- Plans fall back to a configured baseline or median forecast when consumption history is insufficient and explain disabled or incomplete inputs.
- Spot, solar and bidding-zone data refresh in the background, report stale data, and do not let older refreshes overwrite newer settings; spot intervals are assigned to the correct local day.
- Device changes clear old cached readings, and live settings changes are refused during a device transition.

### Security

- Dispatch refuses stale control telemetry and blocks capability, limit or policy changes while a restore is pending; direct control-register writes cannot interleave with an active dispatch command.
- Logger history objects are read-only, and logging out clears cached inverter telemetry from the browser.

## [1.2.0] - 2026-10-13

### New

- Configure a Fraunhofer Energy-Charts bidding zone and preview quarter-hour spot prices for today and tomorrow without controlling the battery.
- Optional dashboard widgets show the current spot price, next-day availability and the day-ahead price curve.
- Dashboard layout edits can be saved, cancelled with Escape or reset to defaults.

### Fix

- The spot-market preview keeps today's prices visible when tomorrow's prices are unavailable and explains why next-day data is missing.
- Inverters connect more reliably at startup, with fewer dropped connections.

### Security

- Spot-market requests use fixed provider-owned HTTPS endpoints and reject redirects to other origins.

## [1.1.0] - 2026-10-12

### New

- The admin interface formats times and numbers using the browser's locale.
- The time-series database page has a live Status & Log console, and Docker startup logs show the container's own IP address.
- The API tokens table shows the masked last four characters so tokens can be distinguished.
- Inverters can be deleted from their dashboard cards after confirmation, and adding one produces a confirmation message.
- Switching the Energy Manager to Off verifies the inverter's Internal SoC strategy before handing back control; new inverters are labelled "Not yet managed" until their mode is known.

### Fix

- The About page's changelog is scrollable, and a device keeps its original address and hardware state separate after reconfiguration.
- Interrupted dispatch operations recover and restore their timer; a failed listen-port bind is reported instead of being silently ignored.
- User-selected time-series retention is preserved, and long inverter responses with trailing zero checksum bytes are no longer dropped.
- Restoring a battery after dispatch writes back the exact original power value, even if the power-sign setting changed meanwhile.

### Security

- Prometheus scraping and metrics push export are disabled by default on a fresh install.
- Dispatch release state is checked against a signed copy, and re-addressing a device clears its verification evidence and control policy.
- Persisted control records without enough data to restore the inverter are rejected.

## [1.0.0] - 2026-10-11

### New

- Initial release.

</details>
