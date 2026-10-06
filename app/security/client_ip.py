#!/usr/bin/env python3
#
# app/security/client_ip.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Source address resolution (Requirement 13.8 to 13.11; ASVS v5.0.0-16.2.1)."""

from collections.abc import Mapping, Sequence
from ipaddress import IPv4Network, IPv6Network, ip_address

type Network = IPv4Network | IPv6Network

# Shared bucket for addresses that cannot be determined: the strictest limit applies to all of them.
UNKNOWN_ADDRESS = "unknown"


def _parse(text: str) -> str | None:
    try:
        return str(ip_address(text.strip().strip("[]")))
    except ValueError:
        return None


def _trusted(address: str, networks: Sequence[Network]) -> bool:
    ip = ip_address(address)
    return any(ip in net for net in networks if net.version == ip.version)


class ClientIpResolver:
    def __init__(self, trusted_proxies: Sequence[Network] = (), forwarded_header: str = "") -> None:
        self._trusted = tuple(trusted_proxies)
        self._header = forwarded_header.lower()

    def _header_value(self, headers: Mapping[str, str]) -> str:
        """All lines of the header in order, comma-joined, so a repeated header cannot hide earlier hops."""
        getlist = getattr(headers, "getlist", None)  # Starlette Headers keep repeated lines
        if getlist is not None:
            return ",".join(getlist(self._header))
        return ",".join(v for k, v in headers.items() if k.lower() == self._header)

    def resolve(self, peer: str | None, headers: Mapping[str, str]) -> str:
        """Peer address by default; the forwarded header only counts when the peer is a trusted proxy."""
        address = _parse(peer) if peer else None
        if address is None:
            return UNKNOWN_ADDRESS
        if not self._header or not _trusted(address, self._trusted):
            return address
        raw = self._header_value(headers)
        if not raw:
            return address
        hops = [_parse(item) for item in raw.split(",")]
        if any(hop is None for hop in hops):
            return UNKNOWN_ADDRESS  # a forged or broken header must not choose the identity
        parsed = [h for h in hops if h is not None]
        for hop in reversed(parsed):
            if not _trusted(hop, self._trusted):
                return hop  # the last address outside the trust list
        return parsed[0] if parsed else address  # a fully trusted chain yields its leftmost, real origin
