"""Local browser boundary and bounded ASGI request buffering."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BYTES = 16_384
ALLOWED_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


class LocalRequestBoundary:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
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
                lengths and (int(lengths[0]) < 0 or int(lengths[0]) > MAX_REQUEST_BYTES)
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
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
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
