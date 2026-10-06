#!/usr/bin/env python3
#
# app/security/tokens.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Token management (Requirement 12; ASVS v5.0.0-11.3.1, v5.0.0-2.2.1)."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config import TokenRole
from app.errors import AuthenticationError
from app.security.pat import pat_well_formed

if TYPE_CHECKING:
    from app.admin.store import AdminStore


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller; ``token_id`` is None only for ANONYMOUS."""

    token_id: str | None
    role: TokenRole


# Only reachable when the admin disables authentication; writes still need write support and the allowlist.
ANONYMOUS = Principal(None, TokenRole.READ_WRITE)


class TokenStore:
    def __init__(self, *, auth_required: bool = True, admin_store: "AdminStore | None" = None) -> None:
        self._auth_required = auth_required
        self._admin_store = admin_store

    def set_auth_required(self, required: bool) -> None:
        self._auth_required = required

    def authenticate(self, authorization: str | None) -> Principal:
        """Return the caller or raise AuthenticationError (missing_token / invalid_token)."""
        if authorization is None or not authorization.strip():
            if self._auth_required:
                raise AuthenticationError("missing_token")
            return ANONYMOUS
        scheme, _, token = authorization.strip().partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token or not pat_well_formed(token):
            raise AuthenticationError("invalid_token")
        entry = self._admin_store.authenticate_token(token) if self._admin_store is not None else None
        if entry is None:
            raise AuthenticationError("invalid_token")
        return Principal(entry.id, TokenRole(entry.role))

    def authenticate_required(self, authorization: str | None) -> Principal:
        """Same checks as ``authenticate``, but a token stays mandatory even without global auth.

        For callers whose own setting demands a token, so disabling auth cannot void it.
        """
        if authorization is None or not authorization.strip():
            raise AuthenticationError("missing_token")
        return self.authenticate(authorization)
