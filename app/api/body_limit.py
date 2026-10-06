#!/usr/bin/env python3
#
# app/api/body_limit.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Request body size limit, enforced before FastAPI parses the body (Requirement 25.26)."""

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.problems import ErrorCode, FieldError, build_problem

# The largest legal write is 65527 payload bytes; as a JSON string with \uXXXX escapes that is
# about 400 KB, so 512 KiB passes every legal value.
MAX_BODY_BYTES = 512 * 1024
_DETAIL = "The request body exceeds the maximum size of 512 KiB."


class BodyLimitMiddleware:
    """Sits inside RequestContextMiddleware, so the rejection keeps correlation id and headers."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self._max = max_bytes

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = build_problem(
            Request(scope),
            ErrorCode.INVALID_REQUEST,
            detail=_DETAIL,
            errors=[FieldError(parameter="body", code=ErrorCode.INVALID_REQUEST.value, detail=_DETAIL)],
            headers={"Connection": "close"},  # the unread body must not be parsed as the next request
        )
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = next((v for k, v in scope["headers"] if k == b"content-length"), None)
        if declared is not None and declared.strip().isdigit() and int(declared) > self._max:
            await self._reject(scope, receive, send)  # rejected without reading a single body byte
            return

        received = 0
        rejected = False

        async def counting_receive() -> Message:
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max:
                    # FastAPI maps a failing receive to 400, so the 422 is sent here and the app sees a disconnect.
                    rejected = True
                    await self._reject(scope, receive, send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not rejected:
                await send(message)

        await self.app(scope, counting_receive, guarded_send)
