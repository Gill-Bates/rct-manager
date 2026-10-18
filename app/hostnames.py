#!/usr/bin/env python3
#
# app/hostnames.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared host-string normalization.

One canonical rule for stripping surrounding whitespace and the ``[...]`` wrapper that URL syntax
puts around an IPv6 literal, so config parsing, the admin device editor and the client-IP parser
all agree on one bare socket host. The surrounding-whitespace strip is part of this contract: the
forwarded-header parser relies on it to accept the optional whitespace a header allows around each
comma-separated hop. Each caller keeps its own follow-on validation (emptiness, internal-whitespace
rejection, stray-bracket rejection, ``ip_address`` parsing).
"""


def strip_ipv6_brackets(host: str) -> str:
    """Return ``host`` trimmed of surrounding whitespace and of the ``[...]`` of an IPv6 literal.

    Surrounding whitespace is removed deliberately; any remaining internal whitespace is left for
    the caller to reject.
    """
    host = host.strip()
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host
