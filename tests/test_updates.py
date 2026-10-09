#!/usr/bin/env python3
#
# tests/test_updates.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Release-check helpers: the server-side allowlist for the GitHub release link (SEC-05)."""

import pytest

from app.admin.updates import _release_url, _version_key

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


@pytest.mark.parametrize(
    ("older", "newer"),
    [("2.0.0rc1", "2.0.0"), ("2.0.0-rc.1", "2.0.0"), ("2.0.0a2", "2.0.0b1"), ("2.0.0rc1", "2.0.0rc2"),
     ("1.9.9", "2.0.0rc1"), ("2.0.0.dev1", "2.0.0a1")],
)
def test_a_pre_release_sorts_below_its_final_release(older: str, newer: str) -> None:
    assert _version_key(older) < _version_key(newer)


def test_equal_versions_are_not_an_update() -> None:
    assert _version_key("v1.1.0") == _version_key("1.1.0")
