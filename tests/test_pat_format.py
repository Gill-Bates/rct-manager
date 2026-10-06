#!/usr/bin/env python3
#
# tests/test_pat_format.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import re

from app.security.pat import PAT_PREFIX, generate_pat, pat_well_formed


def test_generated_pats_have_prefix_base62_body_and_valid_checksum() -> None:
    tokens = {generate_pat() for _ in range(200)}
    assert len(tokens) == 200
    for token in tokens:
        assert re.fullmatch(r"pat_[0-9A-Za-z]{46}", token)
        assert pat_well_formed(token)


def test_mistyped_or_truncated_pats_are_rejected() -> None:
    token = generate_pat()
    flipped = token[:10] + ("A" if token[10] != "A" else "B") + token[11:]
    assert not pat_well_formed(flipped)
    assert not pat_well_formed(token[:-1])
    assert not pat_well_formed(PAT_PREFIX + "x" * 46)


def test_tokens_without_the_pat_prefix_are_rejected() -> None:
    assert not pat_well_formed("legacy-token-from-api-tokens")
    assert not pat_well_formed(generate_pat()[len(PAT_PREFIX):])
