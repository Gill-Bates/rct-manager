#!/usr/bin/env python3
#
# tests/test_updates.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Release-check helpers: the server-side allowlist for the GitHub release link (SEC-05)."""

import io
import json

import pytest

from app.admin import updates
from app.admin.updates import _fetch, _release_url, _version_key, _version_parts

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


@pytest.mark.parametrize("tag", ["garbage", "1.2.3-invalid-junk", "v", ""])
def test_fetch_rejects_an_unparseable_release_tag(tag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tag GitHub returns that does not parse must not be stored as latest_version (SEC/correctness)."""

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(request, timeout):
        del request, timeout
        payload = {"tag_name": tag, "html_url": _VALID, "published_at": "2026-10-01T12:00:00Z"}
        return _Response(json.dumps(payload).encode())

    monkeypatch.setattr(updates, "urlopen", _fake_urlopen)
    result = _fetch()
    assert result["latest_version"] is None
    assert result["update_available"] is False
    assert result["error"] is not None


def test_a_junk_tag_does_not_parse_as_a_version() -> None:
    assert _version_parts("1.2.3-invalid-junk") == (0,)  # not read as (1, 2, 3)
    assert _version_parts("garbage") == (0,)


@pytest.mark.parametrize(
    ("left", "right"),
    [("1.2.3.0", "1.2.3"), ("1.2.3", "1.2.3.0"), ("1.2.0.0", "1.2"), ("1.2.3.0.0", "1.2.3")],
)
def test_trailing_zero_segments_are_equivalent(left: str, right: str) -> None:
    # 1.2.3.0 and 1.2.3 are the same release: neither must be read as an update over the other.
    assert _version_parts(left) == _version_parts(right)
    assert _version_key(left) == _version_key(right)


def test_a_trailing_zero_release_is_not_reported_as_an_update() -> None:
    assert _version_key("1.2.3.0") == _version_key("1.2.3")
    assert not (_version_key("1.2.3.0") > _version_key("1.2.3"))


def test_a_higher_patch_is_still_newer() -> None:
    assert _version_key("1.2.4") > _version_key("1.2.3")


def test_a_pre_release_still_sorts_below_its_final_release() -> None:
    assert _version_key("2.0.0rc1") < _version_key("2.0.0")
