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


<details markdown="1">
<summary>Previous versions...</summary>

## [1.0.0] - 2026-10-11

- `New` Initial Release

</details>
