#!/usr/bin/env python3
#
# tests/test_client_ip.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Source address resolution with repeated forwarding header lines (Requirement 13.8 to 13.11)."""

from ipaddress import ip_network

from starlette.datastructures import Headers

from app.security.client_ip import ClientIpResolver


def test_two_forwarding_header_lines_are_joined_in_order() -> None:
    resolver = ClientIpResolver([ip_network("10.0.0.0/8")], "X-Forwarded-For")
    headers = Headers(raw=[(b"x-forwarded-for", b"203.0.113.9"), (b"x-forwarded-for", b"10.1.1.1")])
    assert resolver.resolve("10.0.0.1", headers) == "203.0.113.9"  # the first line is not lost


def test_fully_trusted_forwarding_chain_yields_the_leftmost_hop() -> None:
    """P3: a chain entirely inside trusted_proxies must not collapse to the immediate peer."""
    resolver = ClientIpResolver([ip_network("10.0.0.0/24")], "X-Forwarded-For")
    headers = Headers(raw=[(b"x-forwarded-for", b"10.0.0.10, 10.0.0.2")])
    assert resolver.resolve("10.0.0.3", headers) == "10.0.0.10"


def test_partially_trusted_forwarding_chain_yields_the_first_untrusted_hop() -> None:
    """Existing behavior must survive the fully-trusted fix: the untrusted hop closest to the origin wins."""
    resolver = ClientIpResolver([ip_network("10.0.0.0/24")], "X-Forwarded-For")
    headers = Headers(raw=[(b"x-forwarded-for", b"203.0.113.9, 10.0.0.2")])
    assert resolver.resolve("10.0.0.3", headers) == "203.0.113.9"
