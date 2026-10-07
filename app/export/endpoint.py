#!/usr/bin/env python3
#
# app/export/endpoint.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Host name / URL resolution for the push export (no dependency on app.config)."""

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int
    tls: bool

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{'https' if self.tls else 'http'}://{host}:{self.port}"

    @property
    def is_loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS


def _is_ipv6(host: str) -> bool:
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return False
    return True


def resolve_endpoint(hostname: str, port: int | None, tls: bool, default_port: int, name: str) -> Endpoint:
    """Accept a host name or an http(s) URL; scheme and URL port win over the separate settings."""
    host = hostname.strip()
    if "://" in host:
        parsed = urlsplit(host)
        if parsed.path.strip("/") or parsed.query or parsed.fragment:
            raise ValueError(f"{name} must not contain a path, query or fragment")
        try:
            url_port = parsed.port
        except ValueError:
            raise ValueError(f"{name} contains an invalid port") from None
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(f"{name} must not contain credentials")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"{name} must be a host name or an http(s) URL")
        tls = parsed.scheme == "https"
        port = url_port if url_port is not None else (port if port is not None else (443 if tls else 80))
        return Endpoint(parsed.hostname, port, tls)  # an explicit scheme always wins over the port heuristic
    elif not host or any(c.isspace() or c in "/?#@" for c in host):
        raise ValueError(f"{name} must be a host name or an http(s) URL")
    elif ":" in host and not _is_ipv6(host):
        raise ValueError(f"{name} must not contain a port; use the port setting or an http(s) URL")
    else:
        port = port if port is not None else default_port
    return Endpoint(host, port, tls or port == 443)  # port 443 implies TLS, e.g. behind a reverse proxy
