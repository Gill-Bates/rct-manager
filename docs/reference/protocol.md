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
| Write allowlist | `data/rct.db`, `app/allowlist.py` |

See [Architecture](../development/architecture.md) for how these modules fit together.

!!! note
    The PDF is a third-party document provided for reference. All rights remain
    with the vendor.
