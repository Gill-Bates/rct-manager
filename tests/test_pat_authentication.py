#!/usr/bin/env python3
#
# tests/test_pat_authentication.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import hashlib
import sqlite3

import pytest

from app.admin.store import AdminStore
from app.security.pat import PAT_PREFIX, generate_pat


@pytest.fixture
def store(tmp_path) -> AdminStore:
    s = AdminStore(tmp_path / "admin.db", "s" * 48)
    s.initialize()
    yield s
    s.close()


def test_authenticate_token_matches_by_keyed_digest_not_by_comparing_plaintext(tmp_path, store) -> None:
    # The lookup key is an HMAC, so no byte-wise token comparison can leak timing information.
    record, token = store.create_token("ci", "read", None)
    with sqlite3.connect(tmp_path / "admin.db") as db:
        digests = [row[0] for row in db.execute("SELECT digest FROM pats")]
    assert len(digests) == 1
    assert token not in digests[0]
    assert digests[0] != hashlib.sha256(token.encode()).hexdigest()
    assert store.authenticate_token(token).id == record["id"]


def test_authenticate_token_rejects_unknown_malformed_and_unprefixed_tokens(store) -> None:
    _, token = store.create_token("ci", "read", None)
    assert store.authenticate_token(generate_pat()) is None
    assert store.authenticate_token(token[len(PAT_PREFIX):]) is None
    assert store.authenticate_token(token + "x") is None
    assert store.authenticate_token("pat_" + "a" * 600) is None
