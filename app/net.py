#!/usr/bin/env python3
#
# app/net.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Shared outbound-HTTP safety helpers for the standard-library ``urllib`` clients.

The anti-SSRF redirect guard lives here once so every outbound provider enforces the identical
rule. ``urllib`` follows 30x responses by default, so even a fixed trusted URL could be bounced to
``http://127.0.0.1`` or an internal host. A redirect is allowed only when the target keeps the same
scheme, host and port as the request; anything else (an https->http downgrade, a different host or
port) raises, so a redirect cannot reach an internal target.
"""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler

MAX_RETRY_AFTER_SECONDS = 30


def retry_after_seconds(header: str | None, *, now: datetime | None = None) -> float:
    """Parse a Retry-After delta or HTTP date within the shared bounded delay."""
    if header is None:
        return 1.0
    header = header.strip()
    try:
        seconds = int(header)
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return 1.0
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (
            when - (now if now is not None else datetime.now(UTC))
        ).total_seconds()
    return float(max(0, min(seconds, MAX_RETRY_AFTER_SECONDS)))


class SameOriginRedirectHandler(HTTPRedirectHandler):
    """A redirect handler that refuses to leave the request's origin.

    A provider subclasses this and sets ``provider_label`` so the raised error names it; the
    same-origin comparison and the raise-vs-delegate contract stay in this one audited place.
    """

    provider_label: str = "request"

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            source = urlsplit(req.full_url)
            target = urlsplit(newurl)
            source_origin = (source.scheme, source.hostname, source.port)
            target_origin = (target.scheme, target.hostname, target.port)
        except ValueError as exc:
            raise HTTPError(
                req.full_url,
                code,
                f"{self.provider_label} returned an invalid redirect URL",
                headers,
                fp,
            ) from exc
        if target_origin != source_origin:
            raise HTTPError(
                req.full_url,
                code,
                f"{self.provider_label} redirected to a different origin",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)
