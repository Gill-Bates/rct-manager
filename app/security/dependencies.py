#!/usr/bin/env python3
#
# app/security/dependencies.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Authorization dependencies: ``require_read``, ``require_write``, ``require_vendor`` (Requirement 12, 30.6).

Deny by default: an unavailable security context or any unexpected state rejects the request.
"""

import asyncio
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.config import TokenRole
from app.errors import AuthenticationError, InsufficientScope, NotFound, WriteDisabled
from app.security.client_ip import ClientIpResolver
from app.security.ratelimit import RateLimiter, caller_key
from app.security.tokens import Principal, TokenStore


@dataclass(slots=True)
class SecurityContext:
    """Built once at startup and stored as ``app.state.security``."""

    tokens: TokenStore
    limiter: RateLimiter
    client_ip: ClientIpResolver
    write_enabled: bool
    vendor_enabled: bool


def get_context(request: Request) -> SecurityContext:
    ctx = getattr(request.app.state, "security", None)
    if not isinstance(ctx, SecurityContext):
        raise AuthenticationError("invalid_token")  # no context means no way to verify anything
    return ctx


def source_address(request: Request, ctx: SecurityContext) -> str:
    peer = request.client.host if request.client else None
    return ctx.client_ip.resolve(peer, request.headers)


Context = Annotated[SecurityContext, Depends(get_context)]


async def require_read(request: Request, ctx: Context) -> Principal:
    """Authenticate, then count the request against the caller's business rate limit."""
    address = source_address(request, ctx)
    ctx.limiter.check_auth_blocked(address)
    try:
        principal = await asyncio.to_thread(ctx.tokens.authenticate, request.headers.get("authorization"))
    except AuthenticationError:
        ctx.limiter.record_auth_failure(address)
        raise
    ctx.limiter.check_request(caller_key(principal.token_id, address))
    request.scope["token_id"] = principal.token_id  # picked up by the request and action logs (Requirement 9.11)
    return principal


async def require_write_enabled(ctx: Context) -> None:
    """Router-level gate for the write routers, which are always registered; the switch is live."""
    if not ctx.write_enabled:
        raise WriteDisabled()


async def require_write(principal: Annotated[Principal, Depends(require_read)], ctx: Context) -> Principal:
    """Write support first, then the token role; allowlist and value checks follow in the handler."""
    if not ctx.write_enabled:
        raise WriteDisabled()
    if principal.role is not TokenRole.READ_WRITE:
        raise InsufficientScope()
    return principal


async def require_vendor(principal: Annotated[Principal, Depends(require_read)], ctx: Context) -> Principal:
    if not ctx.vendor_enabled:
        raise NotFound()
    if principal.role is not TokenRole.READ_WRITE:
        raise InsufficientScope()
    return principal
