# Transport Diagnostics

Low-level diagnostics for the TCP connection to the inverter: periodic subscriptions, parser and CRC
behaviour, counters and a packet-capture runbook. Start with
[Troubleshooting](../troubleshooting.md); come here when a symptom points at the transport. Which
protocol assumptions are verified: [Hardware Verification](hardware-verification.md).

## Diagnostic values

Always available (read token):

| Source                                         | Fields                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/v1/readiness`, per device            | `periodic_available` (`true`: periodic reads registered on the live connection; `false`: not registered, for example right after a reconnect; `null`: periodic reads not configured), `periodic_setup_failures` (failed registration rounds in a row, `0` while registered), `foreign_access_suspected`, `consecutive_failures`, `liveness_source`, `last_success_at`. While a device is not ready the 503 problem carries the same `devices` list |
| `GET /metrics` (if the metrics endpoint is on) | `rct_transport_crc_errors_total`, `rct_transport_framing_errors_total`, `rct_transport_bytes_discarded_total`, `rct_transport_unexpected_frames_total`, `rct_transport_foreign_access_suspected`, `rct_device_periodic_registrations`, `rct_device_errors_total`, `rct_device_last_success_timestamp_seconds`                                                                                                                                      |
| `GET /health`                                  | Process check only; says nothing about the inverter                                                                                                                                                                                                                                                                                                                                                                                                |

Only with `ENABLE_VENDOR_DIAGNOSTICS` (internal switch, off by default, read/write token):
`GET /api/v1/vendor/rct/transports` returns `crc_errors`, `framing_errors`, `discarded_bytes`,
`connection_epoch` (increments with every new TCP connection, so the reconnect count is the epoch
minus 1), `periodic_registrations`, `periodic_available`, `periodic_setup_failures` and
`periodic_last_failure` (reason of the latest failed registration round, per device).
`connection_epoch` and `periodic_last_failure` have no Prometheus metric; without the vendor switch
the reason is only in the log line "Setup of periodic reads failed".

## Reading the counters

| Observation                                                           | Interpretation                                                                                                                                                                 |
| --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Rising `crc_errors`                                                   | Frames arrive damaged and are dropped, never cached. Suspect line noise or a serial bridge. A single increase does not explain a read timeout by itself.                       |
| Rising `framing_errors` or `discarded_bytes` without `crc_errors`     | Stray bytes or broken escaping on the line.                                                                                                                                    |
| `connection_epoch` rising while `crc_errors` stays flat               | Connections end by timeout or network, not by damaged frames (see below).                                                                                                      |
| `Duplicate response to object ... after its transaction was answered` | A second frame for an already answered read (a duplicate or another client's request for the same object). Harmless.                                                           |
| `periodic_available=false` with `periodic_setup_failures` growing     | Registration keeps being interrupted; the retry delay doubles from 10 s to 300 s. Values then come from reads and cache and are marked by `stale`, `source` and `age_seconds`. |
| `foreign_access_suspected=true`                                       | Another client uses port 8899; mixed answers are possible.                                                                                                                     |

**Timeout behaviour.** A single unanswered read (5 s) keeps the connection for other objects, but the
same object is re-read only on a new connection, because the protocol has no transaction id and a
late answer would be taken for the retry's. The second unanswered read in a row drops the connection
too. Either reconnect drops all periodic registrations. The log lines for both cases carry the parser
state (`buffered`, age of the last frame, CRC and framing error counts), which tells an answer held
or discarded by the parser from one never sent.

## Unsolicited response frames

Symptom: at DEBUG level the log shows `Unsolicited response burst started ...` for the first frame
for an object that is not polled, then one `burst summary` line per 60 s window (and when the
connection drops) listing object ids with frame counts. Unmatched frames are never cached.

Unexpected frames still count towards the flood limit: more than 50 within 60 s make the receiver
reconnect (`unexpected_flood`, see `rct_transport_unexpected_frames_total`).

!!! note "Working hypothesis, not verified on hardware"
    The inverter keeps `READ PERIODICALLY` subscriptions from an earlier session (diagnostic script,
    other client, unclean shutdown). At startup the service only clears its own demultiplexer state;
    no device-side unsubscribe is known, so none is sent.

Characterization steps on the real device:

1. Which object ids does this service register? Compare the periodic set in the startup log
   (`Periodic reads active: N of M`) with the object ids in the summary.
2. Which unsolicited object ids remain after a normal start with no other client connected?
3. Stop the service cleanly (SIGTERM, which writes `pas.period = 0`), wait 10 s, start it again and
   record 2-3 minutes of DEBUG log. Do the unsolicited ids disappear?
4. Kill the service abruptly (`kill -9`) and restart it. Do they reappear?
5. Repeat step 2 with the vendor app, home automation and every diagnostic script stopped. Do they
   still occur?

Record the firmware version with the result.

## Truncated long value after a split read

Some long inverter responses declare a wrong length, so the parser takes the frame end from the
framing and accepts a CRC-verified end at the current read boundary. If a TCP read ends inside such a
frame and the bytes so far happen to carry a valid CRC (chance 2^-16 per evaluated boundary), the
frame is delivered truncated and the rest discarded. The wire format cannot tell this prefix from a
complete frame, and the CRC is not weakened to hide it (`PREFIX_COLLISION`, a known limitation).

Symptom: a long response with an unexpectedly short payload and a rising `discarded_bytes` in
`/api/v1/vendor/rct/transports`.

## Runbook: next read timeout

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
    - c) a complete, valid answer arrives but is not accepted: counters stay flat and the connection
      is dropped after 5 s anyway, which points to the software;
    - d) TCP reset, FIN or retransmissions: network or device-side connection problem;
      `connection_epoch` rises without a timeout line.
5. Save the raw bytes of the affected response. Check whether it is a long response (command `0x06`
   or `0x46`) whose frame ends in an escaped CRC byte `2D 2D` or `2D 2B` (fixed escape-pair case: it
   used to wait for a following frame), or one whose first bytes already carry a valid CRC and which
   was split across reads (the known `PREFIX_COLLISION` case, see above). The first would now be
   delivered; the second remains a known limitation.
