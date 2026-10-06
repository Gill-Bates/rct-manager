#!/usr/bin/env python3
#
# app/security/pat.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Personal access token format and the authenticated-token record."""

import re
import secrets
import string
import zlib
from dataclasses import dataclass

# Personal access tokens follow the prefixed-token convention of GitHub and GitLab:
# "pat_" + 40 base62 random characters (~238 bits) + 6 base62 characters of CRC32 over both.
# The prefix makes leaked tokens detectable by secret scanners, the checksum rejects mistyped
# tokens without a lookup, and the alphabet stays within the RFC 6750 bearer token syntax.
PAT_PREFIX = "pat_"
_BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
_PAT_RANDOM_CHARS = 40
_PAT_CHECKSUM_CHARS = 6
_PAT = re.compile(rf"{PAT_PREFIX}[0-9A-Za-z]{{{_PAT_RANDOM_CHARS + _PAT_CHECKSUM_CHARS}}}")


def _pat_checksum(body: str) -> str:
    value, digits = zlib.crc32(body.encode()), []
    for _ in range(_PAT_CHECKSUM_CHARS):
        value, rest = divmod(value, 62)
        digits.append(_BASE62[rest])
    return "".join(reversed(digits))


def generate_pat() -> str:
    body = PAT_PREFIX + "".join(secrets.choice(_BASE62) for _ in range(_PAT_RANDOM_CHARS))
    return body + _pat_checksum(body)


def pat_well_formed(token: str) -> bool:
    """True only for a "pat_"-prefixed token with the expected shape and a valid checksum."""
    return _PAT.fullmatch(token) is not None and _pat_checksum(token[:-_PAT_CHECKSUM_CHARS]) == token[-_PAT_CHECKSUM_CHARS:]


@dataclass(frozen=True, slots=True)
class FileToken:
    id: str
    role: str
