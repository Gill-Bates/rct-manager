#!/usr/bin/env python3
#
# app/hostnames.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared host-string normalization.

One canonical rule for stripping the ``[...]`` wrapper that URL syntax puts around an IPv6 literal,
so config parsing, the admin device editor and the client-IP parser all agree on one bare socket
host. Each caller keeps its own follow-on validation (empty/whitespace rejection, stray-bracket
rejection, ``ip_address`` parsing); only the strip itself lives here.
"""


def strip_ipv6_brackets(host: str) -> str:
    """Return ``host`` without the surrounding ``[...]`` of an IPv6 literal, else unchanged."""
    host = host.strip()
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host
