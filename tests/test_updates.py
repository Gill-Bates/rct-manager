#!/usr/bin/env python3
#
# tests/test_updates.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Release-check helpers: the server-side allowlist for the GitHub release link (SEC-05)."""

import pytest

from app.admin.updates import _release_url

_VALID = "https://github.com/Gill-Bates/rct-manager/releases/tag/v1.1.0"


def test_a_matching_github_release_url_is_kept() -> None:
    assert _release_url(_VALID) == _VALID


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "",
        "javascript:alert(1)",
        # Wrong scheme, wrong host, host-suffix spoof, wrong repository path.
        "http://github.com/Gill-Bates/rct-manager/releases/tag/v1.1.0",
        "https://evil.example/Gill-Bates/rct-manager/releases/tag/v1.1.0",
        "https://github.com.evil.example/Gill-Bates/rct-manager/releases/tag/v1.1.0",
        "https://github.com/other/repo/releases/tag/v1.1.0",
    ],
)
def test_a_url_outside_the_allowlist_is_dropped(value: object) -> None:
    assert _release_url(value) is None
