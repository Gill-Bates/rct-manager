# Protocol Specification

The gateway implements the RCT Power Serial Communication Protocol (document
version 1.14) over TCP port 8899. The vendor document is the normative source for
frame layout, CRC, escaping, data types and the object registry.

[Download the specification (PDF)](6707-RCT-Power-Serial-Communication-Protocol.pdf){ .md-button }

## Where it is used

| Topic | Code |
| --- | --- |
| Frames, escaping, CRC16-CCITT, value codecs | `app/protocol` |
| Object registry (read objects, data types) | `app/catalog/objects.json`, `app/catalog` |
| Write allowlist | `data/rct.db`, `app/security/allowlist.py` |

See [Architecture](../development/architecture.md) for how these modules fit together. Which protocol
assumptions are verified on hardware: [Hardware Verification](hardware-verification.md). Connection
level analysis: [Transport Diagnostics](transport-diagnostics.md).

## Protocol catalog

`app/catalog/objects.json` ships 894 of the 895 IDs from the supplied protocol PDF v1.14 (without
`wifi_password`). The shipped write-policy seed describes 849 scalar objects; the GUI manages actual
write approvals in `data/rct.db`. Existing friendly names stay stable; additional names replace dots
and brackets with underscores. `net_slave_data` remains a structured diagnostic read. Catalog and
policy seed are packaged application data; the two former project-root JSON files have been removed.
The write reference
[rctpower_writesupport at 4a0d2e9](https://github.com/do-gooder/rctpower_writesupport/blob/4a0d2e9296b8d45abfaa1dfe437e6a7945b2b619/rct.py)
informed the ten supported parameters and the one-byte encoding of the 15 enum objects.

## Value semantics

- Subnormal `t_float` readings preserve their IEEE 754 value and sign in the decoder.
- Battery power, voltage and current are separate device variables; the power need not equal voltage
  times current.
- Grid feed-in energies are passed through as the device sends them; the protocol defines no sign.

!!! note
    The PDF is a third-party document provided for reference. All rights remain
    with the vendor.
