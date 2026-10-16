## [1.2.2] - 2026-xx-xx

- ``New`` After the first forced password change, you must accept a one-time usage and liability disclaimer before the admin interface can be used; once accepted it never asks again and survives later password changes.
- ``New`` The battery health dashboard widgets now show one line per inverter when several are configured, each with the inverter's name and its serial on hover, instead of merging them into a single value.
- ``New`` The Charging plan's Expert diagnostics show a battery calibration history: the usable capacity measured by the BMS over time against the datasheet capacity, kept per inverter serial and read from the local store only. A commissioning date entered by hand starts the curve; deriving it from the inverter energy logs is unverified on hardware and off by default (`battery_commissioning_derivation_enabled`); a replaced battery (new BMS serial) starts a new series.
- ``New`` The dashboard has battery health widgets (usable kWh, remaining % vs datasheet, cycles, last calibration, commissioning date, and a degradation chart), off by default and shown per inverter serial. The calibration history is recorded independently of the Energy Manager.
- ``Fix`` The battery health dashboard tiles list one inverter per row under its display name (serial in the tooltip) instead of one clipped line, and the widgets start directly below the inverter overview instead of far down the page.
- ``Fix`` In the dashboard's "Add widget" dialog, clicking Add or Remove keeps keyboard focus on the same widget's button, so Escape still closes the dialog (it did nothing after the first click since 1.2.0); closing it with Escape no longer discards the unsaved layout edit.
- ``Change`` The usable battery capacity is capped by the BMS ratio of measured to rated Ah (never above the datasheet value); the two Ah registers are refreshed in the background about once a day. A single tower accepts 2 to 6 modules, two towers only 4+4, 5+5 or 6+6.
- ``New`` The Charging plan has a "Battery remaining" tile: the energy currently usable in the battery (verified usable capacity x measured SoC, e.g. 3.42 kWh, 33 % SoC of 10.37 kWh usable). It comes from the same battery snapshot as the plan (new `battery` block in the dynamic-plan response) and shows "—" while the battery state is unavailable.
- ``Change`` The Charging plan uses the full width: four equal KPI tiles (2 x 2 on tablets, stacked on phones), the schedule chart below, the household consumption history underneath without reserving empty space, and a compact header with the Inverter select and Expert switch beside the title.
- ``Change`` The Charging plan reloads on its own (every 5 minutes, when the tab becomes visible again, every 10 s while an input is pending); the Refresh button is gone. The next-charge figures sit in one row and the status cards have no frame.
- ``Change`` A device without configured or stored power limits falls back to 3 kW charge and discharge power for dispatch; Expert mode can store its own values. An inverter that does not answer shows its host instead of "n/a" in the Energy page hardware summary.
- ``Fix`` The verified usable battery capacity now uses the same debounced module topology as the module display, so one flipped complete read of the module serials no longer changes it.
- ``Fix`` The site timezone lookup no longer reuses a cached zone for nearby coordinates (a timezone boundary could yield the wrong zone for up to 24 h), and a place search no longer shows a postal code taken from a postcode-area hit unless the query matched it exactly.
- ``Fix`` A write cancelled during its quiet window now drops the connection, so a late write echo cannot answer the next request; a failed connection setup closes its socket.
- ``Fix`` Write allowlist bounds and step must be finite numbers, and a spot-price day (today, tomorrow, yesterday) is only accepted when it covers a complete local day (DST days included); an empty next-day feed counts as not yet published.
- ``Fix`` Reading the bidding-zone list no longer waits for a running provider refresh, and a damaged stored zone catalog is ignored and reloaded.
- ``Fix`` The Energy page no longer floods the log with errors while write support is off: the expected 503 from the energy device poll is logged at DEBUG, and the page polls every 30 s instead of 3 s until the next success.
- ``Fix`` The Charging plan no longer shows the "Please wait …" spinner for good: a missing or unverifiable battery state shows its explanation at once, a pending input that is still pending after the 30 polls keeps its status card instead of returning to the spinner every 5 minutes, and the text for a missing consumption estimate matches the baseline fallback. Several logger points for one local date in the inverter day history are now logged once.
- ``Fix`` Non-finite floats are rejected when decoding a reply or encoding a write, and a device host is stored in its canonical form (IP address normalized, host name lower-cased).
- ``Fix`` The outline-secondary buttons (e.g. "Edit dashboard") use the theme's secondary text colour in the light theme, so their label reaches the 4.5:1 contrast ratio.
- ``Fix`` The Charging plan no longer announces a "Next grid charge" while the Energy Manager is off for the inverter; it states that no grid charge will be started.
- ``Security`` The usage disclaimer is enforced on the server, not just in the browser: until it is accepted every admin request is refused (including personal access tokens), and opening any page by its direct URL only shows the acceptance prompt.

<details markdown="1">
<summary>Previous versions...</summary>

## [1.2.1] - 2026-10-16

- ``New`` Configure an Open-Meteo solar forecast in Energy Manager (location search via Photon, production curve preview) with an optional read-only dashboard widget.
- ``New`` Preview a read-only dynamic charging plan from spot prices, solar forecast, consumption history, battery state, and per-inverter limits. The Dry-Run explains its assumptions and never sends commands to the battery.
- ``New`` The Charging plan tab shows the last 60 days of household consumption as a bar chart with the daily average, and the schedule as a full-width time-series chart (grid charge power and target SoC, per-slot tooltip, screen-reader summary). The schedule lists the newest slot first, explains each reason in plain words, shows end dates of slots running past midnight, and stays open on refresh.
- ``New`` The usable battery capacity is verified from the module count and the RCT Power Battery datasheet (1.92 kWh per module, 90 % usable), e.g. 10.37 kWh for a Power Battery 11.5. An Expert override takes precedence; capacity is never estimated from Ah and voltage and stays "unavailable" without a verified source.
- ``Fix`` The usable battery capacity is now verified on a single-tower system: module serial slots the scan never reads behind an unanswered slot no longer block the calculation, so the charging plan no longer stays on "could not be verified".
- ``Change`` While the Charging plan waits for an input (battery state, prices, solar forecast, consumption model), a rotating "Please wait …" spinner replaces the status text and checklist (still shown in Expert mode) and the plan is re-requested every 10 s, at most 30 times. Settings that need action, such as a disabled spot market, keep their text. A device change clears the consumption chart immediately instead of leaving the previous one.
- ``New`` A "Charging schedule" dashboard widget (off by default, added through Edit dashboard) shows the Dry-Run charging plan of the first inverter as a time-series chart of grid charge power and target SoC, with a per-slot tooltip. It refreshes every 5 minutes like the forecast widgets and shows a short text such as "Spot market disabled" while the plan is unavailable.
- ``Change`` The household consumption section on the Charging plan tab is hidden while there is no data to draw. "Next grid charge" shows Target SoC, Charge power and Total plan grid energy as hero metrics in the Spot Market style.
- ``Fix`` Battery modules are counted correctly (every populated module serial slot is one module, so a Power Battery 11.5 shows 6), and the dashboard battery tower is drawn true to size for up to 6 modules.
- ``Fix`` The runtime charging plan no longer waits for consumption history: it falls back to the configured baseline or a median estimate and reports this in the stable `forecast_basis` and `forecast_confidence` enums (shared with the Dry-Run). The preview says "Spot market is disabled" or "Solar forecast is not configured" when an input is off or incomplete.
- ``Fix`` Spot prices, bidding zones, and solar forecasts refresh in the background and are served from the database, so opening the admin interface or restarting triggers no provider calls. Failed refreshes show a stale-data warning, and an older refresh cannot overwrite concurrent changes. Spot-price previews assign quarter-hour intervals to the correct local day, including daylight-saving transitions.
- ``Fix`` Connection stability: a single unanswered read keeps the connection for other objects, the same object is re-read on a fresh one (a late answer cannot be mistaken for the retry's answer), and a second unanswered read in a row reconnects; a write after an unanswered read starts on a fresh connection; a single-tower inverter no longer drops its connection over second-tower registers; unsolicited responses are logged as one debug "burst started" line plus a per-minute summary (flood limit and reconnect behaviour unchanged); timeout log lines include the parser state and duplicate responses are logged as such.
- ``Fix`` Removing an inverter or changing its address clears its cached readings and the dashboard tower's old module count. Live settings or parameter changes are rejected while another device transition is running.
- ``Fix`` On narrow screens the inverter card header keeps title, status badge, and delete button on one row, and the four energy status tiles stay in one row down to 320 px.
- ``Security`` Dispatch refuses stale control telemetry before the first setpoint and blocks capability, limit, or policy changes while a restore is pending; direct writes to battery control registers can no longer interleave with an active dispatch command.
- ``Security`` The inverter's `logger_*_log_ts` history objects are read-only; a write allowlist listing one is rejected at startup with the metric name. Logging out clears cached inverter telemetry from the browser.

## [1.2.0] - 2026-10-13

- ``New`` Configure a Fraunhofer Energy-Charts bidding zone in Energy Manager and preview quarter-hour spot prices for today and tomorrow; the preview updates when settings change and does not control the battery.
- ``New`` Add optional dashboard widgets for the current spot price, next-day availability, and the day-ahead price curve.
- ``New`` Dashboard layout changes are saved with "Done"; pressing Escape discards the edit session and restores the last database-backed layout, while "Reset layout" applies defaults without a confirmation dialog.
- ``New`` An animated dashboard hint points new users to "Add inverter" until their first inverter is configured, with a reduced-motion fallback.
- ``Fix`` The spot-market preview still shows today's prices when tomorrow's prices are unavailable, along with the reason the next-day data is missing.
- ``Fix`` Inverters connect more reliably at startup, with fewer dropped connections and less log noise, so your devices become fully available sooner after the service starts.
- ``Security`` Spot-market requests use fixed provider-owned HTTPS endpoints and reject redirects to other origins, preventing a saved configuration from directing requests to an arbitrary host.

## [1.1.0] - 2026-10-12

- ``New`` The admin interface now formats times and numbers using your browser's locale instead of a fixed British format.
- ``New`` The time-series database page now has a terminal-style Status & Log console that tails the service's own log live, with timestamps and per-level colours.
- ``New`` The startup log now shows the container's own IP address when the service runs in Docker, so you can see where to reach it.
- ``New`` The API tokens table now shows the masked last four characters of each personal access token, so you can tell your tokens apart.
- ``New`` You can now delete an inverter directly from its dashboard card with a trash icon, after confirming the removal.
- ``New`` An "Inverter added" confirmation now appears after you add a device and close the inverters dialog.
- ``New`` When you switch an inverter's Energy Manager to Off, the service now sets and verifies the inverter's Internal SoC strategy before handing control back, and reports an error if that cannot be confirmed; a newly added inverter shows "Not yet managed" with its observed strategy instead of being labelled Off.
- ``Fix`` The changelog on the About page now renders correctly, with a scrollable history for older releases.
- ``Fix`` Restoring a battery after dispatch now writes back the exact original power value, so the inverter's charge/discharge direction is reproduced even if the power-sign setting changed in between.
- ``Fix`` Changing a device's address or network ID in the live device list no longer carries over the previous hardware's connection state or stale failure history.
- ``Fix`` A device whose last control action was interrupted now recovers on its own instead of staying stuck, and its automatic restore timer runs as expected.
- ``Fix`` Changing the listen address no longer silently ignores a real port conflict with another program, which could leave the service unable to restart.
- ``Fix`` A retention period you set yourself on the time-series database is now always kept and never overwritten by the service.
- ``Fix`` Inverter communication now recovers long device responses whose checksum ends in zero bytes, instead of dropping them, improving reliability on affected hardware.
- ``Security`` The Prometheus metrics endpoint and the metrics push export are now disabled by default on a fresh install; an operator must opt in before either is active.
- ``Security`` Battery dispatch can no longer be released onto unverified hardware by tampering with the local database; the released state is now cross-checked against a signed copy.
- ``Security`` Re-addressing a device now clears the previous hardware's verification evidence, engineering-mode switch, and target policy, so they cannot be inherited by a different device.
- ``Security`` A persisted control record that is missing the data needed to restore the inverter is now rejected instead of silently skipping the restore.

## [1.0.0] - 2026-10-11

- `New` Initial Release

</details>
