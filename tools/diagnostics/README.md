# Hardware diagnostics

## rct_battery_probe.py

Characterizes `battery_soc_target` (0x8B9FF008) and the battery capacity candidates on a real
inverter. Results are observations, not a capacity release.

The default mode sends only normal `READ` frames and observes passively. It registers no
`READ_PERIODICALLY`, never writes, and starts no `PeriodicManager`. A guard around
`TransportEndpoint.execute` aborts on any other command. No secrets and no serial numbers are
printed (module slots appear as indices only; a throw-away HMAC secret only builds the app objects).

### Usage

Stop the rct-manager app first (one socket per device), then run from the repository root:

```bash
python tools/diagnostics/rct_battery_probe.py --device home=192.168.1.50:8899 \
    --confirm-app-stopped --json-out probe.json --csv-out probe.csv
```

The run takes about 6-7 minutes (default `battery_soc_target` READs at T+5/30/60/300 s; each
unanswered object costs one response timeout and a reconnect).

It reads, side by side and without interpretation: `power_mng_n_batteries`,
`power_mng_amp_hours_measured`, `battery_ah_capacity`, `battery_placeholder_0_ah_capacity`;
`battery_soc`/`battery_voltage` (tower 2 only when the topology detects it); the topology from
`module_sn` slots; `household_load_power`; model and firmware.

| Option | Effect |
| --- | --- |
| `--device id=host:port[@network_id]` | Device (required, exactly one) |
| `--confirm-app-stopped` | Required confirmation that no app instance holds the connection |
| `--offsets 5,30,60,300` | Seconds after connect for the `battery_soc_target` READs |
| `--response-timeout 5` | Per-read timeout in seconds |
| `--with-energy` | Also read `battery_stored_energy`/`battery_used_energy` (meaning taken from the external rctclient registry, not verified in this project) |
| `--skip-topology`, `--skip-household` | Skip those reads |
| `--exposed-names a,b` | Override the exposed metric names used to compute the (informational) periodic set |
| `--json-out`, `--csv-out` | Write the full result / one row per read |

`classification_hint` in the default mode only reports what plain READs show (`read_ok`,
`read_unstable`, `read_unstable_early`, `read_unstable_mixed`). The cases A-D need a periodic
comparison and exist only in the invasive mode.

### Invasive test

**INVASIVE. Not part of a normal run.** It registers `READ_PERIODICALLY` on the device, which
changes its subscription state and can recreate the stale periodic subscriptions that this tool is
meant to investigate. The cleanup semantics are unclear: the registration is only withdrawn locally;
the device keeps it until the connection ends. Use it in a fresh device/app restart window.

```bash
python tools/diagnostics/rct_battery_probe.py --device home=192.168.1.50:8899 \
    --confirm-app-stopped --invasive-periodic-test \
    --i-understand-this-registers-periodic-reads
```

- `--invasive-periodic-test` registers `battery_soc_target` and `household_load_power` after the READ
  timeline. Without `--i-understand-this-registers-periodic-reads` the tool aborts.
- `--invasive-periodic-first` (requires `--invasive-periodic-test`) makes the first
  `battery_soc_target` access a registration instead of a READ.
- Only the registration answer is judged. No `pas.period` WRITE is ever sent, so later periodic
  pushes are not observed.
- Output and JSON carry a warning banner and mode `INVASIVE ...`; the guard then allows
  `READ_PERIODICALLY` in addition to `READ`.

### Dependency on private app code

The tool imports private helpers (`_build`, `_effective_periodic_names`, `_MODULE_INVENTORY_NAMES`
from `app.api.app_factory`; `_battery_module_slot_states`, `_battery_tower_present`,
`_raw_battery_module_report` from `app.gateway.battery_inventory`) and uses `RctGateway._device`. A refactoring of
these can break it; `tests/test_rct_battery_probe.py` runs it against a fake inverter and catches that.
