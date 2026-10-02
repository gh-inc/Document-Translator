"""File downloads whose range failures use the public error envelope."""

from starlette.responses import FileResponse
from starlette.types import Message, Receive, Scope, Send

from app.api.errors import error_response
from app.core.errors import ErrorCode


class DownloadResponse(FileResponse):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        replaced = False

        async def safe_send(message: Message) -> None:
            nonlocal replaced
            if message["type"] == "http.response.start" and message["status"] in {400, 416}:
                response = error_response(ErrorCode.INVALID_REQUEST, message["status"])
                for name, value in message.get("headers", []):
                    if name.lower() == b"content-range":
                        response.headers["Content-Range"] = value.decode("latin-1")
                replaced = True
                await response(scope, receive, send)
            elif not replaced:
                await send(message)

        await super().__call__(scope, receive, safe_send)
