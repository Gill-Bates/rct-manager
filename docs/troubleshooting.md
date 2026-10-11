# Troubleshooting

## The container is unhealthy or unreachable

The service listens on loopback. Inside a container set `BIND_ADDRESS=0.0.0.0`
and the GUI option "behind reverse proxy", and keep `BIND_PORT` equal to the container
port of the mapping. The start logs a warning for a loopback bind in a
container.

## The start is refused

Run `python -m app validate` (or `docker compose -f docker/compose.yaml run --rm rct-api validate`).
Typical causes:

- a device list saved in the GUI with more than one device without network id on
  the same endpoint;
- an invalid object registry or write allowlist.
- missing `HMAC_SECRET` in a read-only container, or a secret that differs
  from the one used to encrypt the existing database.

## First admin login

The generated `admin` password is never logged. The first start prints it in clear text in the
boxed `FIRST START - admin login` block on stdout and also saves it to `initial-admin-password`
next to the admin database (`data/initial-admin-password` by default, mode `0600`) as a fallback
for runs without a visible console. Log in, change the password, then delete the file — an absent
file is the intended end state. So a missing file means either the password has already been
changed, or this is not a first start: the administrator exists already and no new password was
generated. A restart does not replace the password. The first login requires a password change.
Keep the database and `HMAC_SECRET` together when restoring a backup.

## `/docs` answers 404

Both `/docs` and `/openapi.json` need `DOCS_PUBLIC=true`.

## Write endpoints answer 404

While write access is off (GUI **Inverters** page), the write, dispatch and Energy Manager routes
answer 404 `write_disabled`. Switching it on takes effect at once, without a restart.

## 429 from many clients behind a proxy

Set `TRUSTED_PROXIES` and `FORWARDED_HEADER`; otherwise all callers share the
proxy address. See [Network and TLS](configuration/network.md).

## 503 `device_maintenance`

The inverter signalled its bootloader. Requests resume after the cool-down once
a single read succeeds.

## Mixed or stale values

Check `foreign_access_suspected` in `GET /api/v1/readiness`: another client
(vendor app, home automation) may be using port 8899, or a second instance runs
against the same inverter. See [Operation](operation.md).

## Truncated long value after a split read (known protocol ambiguity)

Some long responses of the inverter declare a wrong length, so the parser takes the frame end from
the framing and accepts a CRC-verified end at the current read boundary. If a TCP read ends exactly
inside such a frame and the bytes received so far happen to carry a valid CRC (chance 2^-16 per
evaluated boundary), the frame is delivered truncated and the rest is discarded. The wire format has
no further framing information to tell this prefix from a complete frame, and the CRC is not
weakened to hide it. A suspect value shows up as a long response with an unexpectedly short payload
together with a rising `discarded_bytes` counter in `/api/v1/vendor/rct/transports`.

## Diagnostic values after a deployment

Always available (read token):

- `GET /api/v1/readiness`, per device: `periodic_available` (`true`: periodic reads are registered on
  the live connection; `false`: not registered, for example right after a reconnect; `null`: periodic
  reads not configured), `periodic_setup_failures` (failed registration rounds in a row, `0` while
  registered), `foreign_access_suspected`, `consecutive_failures`, `liveness_source`, `last_success_at`.
  While a device is not ready the 503 problem carries the same `devices` list.
- `GET /metrics` (Prometheus, if the metrics endpoint is on): `rct_transport_crc_errors_total`,
  `rct_transport_framing_errors_total`, `rct_transport_bytes_discarded_total`,
  `rct_transport_unexpected_frames_total`, `rct_transport_foreign_access_suspected`,
  `rct_device_periodic_registrations`, `rct_device_errors_total`,
  `rct_device_last_success_timestamp_seconds`.
- `GET /health` is a process check only and says nothing about the inverter.

Only with `ENABLE_VENDOR_DIAGNOSTICS` (internal switch, off by default, read/write token),
`GET /api/v1/vendor/rct/transports`: `crc_errors`, `framing_errors`, `discarded_bytes`,
`connection_epoch` (increments with every new TCP connection, so the reconnect count is the epoch
minus 1), `periodic_registrations`, `periodic_available`, `periodic_setup_failures` and
`periodic_last_failure` (reason of the latest failed registration round, per device). `connection_epoch`
and `periodic_last_failure` have no Prometheus metric; without the vendor switch the reason is only in
the log line "Setup of periodic reads failed".

How to read them:

- Rising `crc_errors`: frames arrive damaged and are dropped, never cached. Suspect line noise or a
  serial bridge. A single increase does not explain a read timeout by itself.
- Rising `framing_errors` or `discarded_bytes` without `crc_errors`: stray bytes or broken escaping on
  the line.
- `connection_epoch` rising while `crc_errors` stays flat: connections end by timeout or network, not
  by damaged frames. Each unanswered read after 5 s drops the connection and with it all periodic
  registrations.
- `periodic_available=false` with `periodic_setup_failures` growing: registration keeps being
  interrupted; the retry delay doubles from 10 s to 300 s. Values then come from reads and cache and
  are marked by `stale`, `source` and `age_seconds`.
- `foreign_access_suspected=true`: another client uses port 8899; mixed answers are possible.

## Runbook: next read timeout on the inverter connection

Keep the polling load unchanged; do not add reads to reproduce it.

1. Note the time of the log line "Connection to ... dropped: no response to object 0x... within 5.0 s"
   and the object id.
2. Capture `GET /api/v1/readiness`, `GET /metrics` (and the vendor transports, if enabled) as close
   before and after that time as possible; compare `crc_errors`, `framing_errors`, `discarded_bytes`,
   `connection_epoch`, `periodic_setup_failures`.
3. In parallel keep a packet capture of port 8899 running on the host (for example
   `tcpdump -i any -s 0 -w incident.pcap tcp port 8899`) and save it with the log excerpt.
4. In the capture, tell apart:
    - a) no answer: the request goes out, no data from the inverter follows;
    - b) a TCP answer arrives but is damaged or incomplete (bad CRC, truncated): expect `crc_errors`
      or `discarded_bytes` to rise;
    - c) a complete, valid answer arrives but is not accepted: counters stay flat and the connection is
      dropped after 5 s anyway, which points to the software;
    - d) TCP reset, FIN or retransmissions: network or device-side connection problem; `connection_epoch`
      rises without a timeout line.
5. Save the raw bytes of the affected response. Check whether it is a long response (command 0x06 or
   0x46) whose frame ends in an escaped CRC byte `2D 2D` or `2D 2B` (fixed escape-pair case: used to
   wait for a following frame), or one whose first bytes already carry a valid CRC and which was
   split across reads (known `PREFIX_COLLISION` case, see above). The first would now be delivered;
   the second remains a known limitation.
