"""Small, bounded ASGI request buffering and correlated completion logs."""

import re
import time
from uuid import uuid4

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger(__name__)


class RequestBoundaryMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        supplied = headers.get(b"x-request-id", b"").decode("ascii", "ignore")
        request_id = supplied if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", supplied) else str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.monotonic()
        status_code = 500

        async def send_response(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-store"),
                ]
            await send(message)

        try:
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > self.max_bytes:
                    response = JSONResponse({"detail": "Request body too large"}, status_code=413)
                    await response(scope, receive, send_response)
                    return
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            buffered = bytes(body)
            body.clear()
            consumed = False

            async def bounded_receive() -> Message:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": buffered, "more_body": False}
                return await receive()

            await self.app(scope, bounded_receive, send_response)
        finally:
            logger.info(
                "request_completed",
                method=scope["method"],
                path=scope["path"],
                status=status_code,
                duration_ms=round((time.monotonic() - start) * 1000, 2),
            )
            structlog.contextvars.clear_contextvars()
