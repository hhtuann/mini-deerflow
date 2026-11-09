"""Local browser boundary and bounded ASGI request buffering."""

import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BYTES = 16_384
ALLOWED_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)
# The only routes allowed to exceed MAX_REQUEST_BYTES: raw-body document
# uploads whose Content-Length equals the configured per-file quota. The
# pattern deliberately names both upload routes so every other path keeps
# the tight default cap.
UPLOAD_ROUTE_PATTERN = (
    r"^/api/(?:live|offline)/workspaces/[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"/documents(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/versions)?$"
)
_UPLOAD_ROUTE = re.compile(UPLOAD_ROUTE_PATTERN)


class LocalRequestBoundary:
    def __init__(
        self,
        app: ASGIApp,
        *,
        upload_max_bytes: int | None = None,
    ) -> None:
        self.app = app
        if upload_max_bytes is not None and (
            isinstance(upload_max_bytes, bool)
            or not isinstance(upload_max_bytes, int)
            or upload_max_bytes < 1
        ):
            raise ValueError("upload_max_bytes must be a positive integer")
        self._upload_max_bytes = upload_max_bytes

    def _body_limit(self, scope: Scope) -> int:
        if (
            self._upload_max_bytes is not None
            and scope["method"] == "POST"
            and _UPLOAD_ROUTE.match(scope.get("path", ""))
        ):
            return self._upload_max_bytes
        return MAX_REQUEST_BYTES

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._body_limit(scope)
        headers = scope.get("headers", ())
        origins = [value.decode("latin1") for key, value in headers if key == b"origin"]
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"} and (
            len(origins) > 1 or (origins and origins[0] not in ALLOWED_ORIGINS)
        ):
            await JSONResponse({"detail": "Origin is not allowed."}, 403)(
                scope, receive, send
            )
            return
        lengths = [value for key, value in headers if key == b"content-length"]
        try:
            invalid_length = len(lengths) > 1 or (
                lengths and (int(lengths[0]) < 0 or int(lengths[0]) > limit)
            )
        except ValueError:
            invalid_length = True
        if invalid_length:
            await JSONResponse(
                {"detail": "Request body is too large or invalid."}, 413
            )(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                await JSONResponse({"detail": "Request body is too large."}, 413)(
                    scope, receive, send
                )
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        consumed = False

        async def replay() -> Message:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
