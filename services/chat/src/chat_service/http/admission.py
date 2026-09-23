"""Opt-in per-process message admission experiment; no queue or global limit."""

import re
from http import HTTPStatus

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from chat_service.http.errors import problem_response
from chat_service.schemas.responses import ErrorCode

MESSAGE_PATH = re.compile(r"/v1/internal-conversations/[^/]+/messages")


class MessageAdmission:
    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit
        self.active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        eligible = (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and MESSAGE_PATH.fullmatch(scope["path"]) is not None
        )
        if not eligible:
            await self.app(scope, receive, send)
            return
        # No await between checking and incrementing: one ASGI event loop owns this.
        if self.active >= self.limit:
            response = problem_response(
                Request(scope),
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                code=ErrorCode.HTTP_ERROR,
                headers={"Retry-After": "1", "X-Lab-Admission": "rejected"},
            )
            await response(scope, receive, send)
            return
        self.active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.active -= 1
