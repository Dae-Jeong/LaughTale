import asyncio
import time
from collections.abc import Callable, Coroutine
from uuid import UUID

import anyio
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from chat_service.core.chat_hub import (
    MAX_FRAME_BYTES,
    MAX_SUBSCRIPTIONS,
    SEND_TIMEOUT_SECONDS,
    SESSION_LEASE_SECONDS,
    ChatHub,
    Peer,
)
from chat_service.core.peer import CloseReason, OfferResult
from chat_service.core.sessions import COOKIE_NAME, SessionStore
from chat_service.exceptions.chat import ConversationNotFoundError, UnauthenticatedError
from chat_service.exceptions.database import (
    ChatResourcesUnavailable,
    DatabaseBusy,
    DatabasePoolTimeout,
)
from chat_service.schemas.chat import (
    Subscribed,
    Unsubscribe,
    WSError,
    client_frame_adapter,
)
from chat_service.schemas.responses import ErrorCode
from chat_service.services.chat import ChatService


def close_code_for(reason: CloseReason) -> int:
    return 1008 if reason is CloseReason.AUTH_REVOKED else 1013


class ChatConnection:
    def __init__(
        self, sessions: SessionStore, service: Callable[[], ChatService], hub: ChatHub
    ) -> None:
        self.sessions = sessions
        self.create_service = service
        self.hub = hub
        self.tasks: list[asyncio.Task[object]] = []
        self.socket: WebSocket
        self.peer: Peer
        self.service: ChatService

    async def run(self, socket: WebSocket) -> None:
        self.socket = socket
        token = socket.cookies.get(COOKIE_NAME)
        checked_at = time.monotonic()
        try:
            actor = await self.sessions.resolve(token)
        except UnauthenticatedError:
            await socket.close(code=1008)
            return
        try:
            self.service = self.create_service()
        except ChatResourcesUnavailable:
            await socket.close(code=1013)
            return
        self.peer = Peer(
            actor_id=actor.user_id,
            token=token,
            session_valid_until=checked_at + SESSION_LEASE_SECONDS,
        )
        if time.monotonic() >= self.peer.session_valid_until:
            await socket.close(code=1013)
            return
        if not self.hub.join(self.peer):
            await socket.close(code=1013)
            return
        code = 1000
        try:
            await socket.accept()
            self.start(self.send())
            self.start(self.receive())
            self.start(self.peer.wait_close_requested())
            done, _ = await asyncio.wait(
                self.tasks, return_when=asyncio.FIRST_COMPLETED
            )
            if self.peer.is_closing:
                code = close_code_for(
                    self.peer.close_reason or CloseReason.DELIVERY_FAILED
                )
            for task in done:
                task.result()
        except UnauthenticatedError:
            code = 1008
        except TimeoutError, WebSocketDisconnect:
            code = 1013
        except Exception:
            code = 1011
        finally:
            await self.close(code)

    def start(self, work: Coroutine[object, object, object]) -> None:
        try:
            task = asyncio.create_task(work)
        except BaseException:
            work.close()
            raise
        self.tasks.append(task)

    async def send(self) -> None:
        while True:
            encoded = await self.peer.next_frame()
            if self.peer.is_closing:
                return
            if time.monotonic() >= self.peer.session_valid_until:
                checked_at = time.monotonic()
                async with asyncio.timeout(1):
                    actor = await self.sessions.resolve(self.peer.token)
                if actor.user_id != self.peer.actor_id:
                    raise UnauthenticatedError()
                self.peer.session_valid_until = checked_at + SESSION_LEASE_SECONDS
                if time.monotonic() >= self.peer.session_valid_until:
                    self.peer.request_close(CloseReason.SESSION_EXPIRED)
            # Primary await 중 권한이 폐기됐으면 이미 꺼낸 본문도 보내지 않습니다.
            if self.peer.is_closing:
                return
            async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                await self.socket.send_text(encoded)

    async def receive(self) -> None:
        while True:
            incoming = await self.socket.receive()
            if incoming["type"] == "websocket.disconnect":
                return
            actor = await self.sessions.resolve(self.peer.token)
            raw = incoming.get("text")
            if not isinstance(raw, str) or len(raw.encode()) > MAX_FRAME_BYTES:
                if (
                    self.peer.offer(WSError(code=ErrorCode.INVALID_INPUT))
                    is OfferResult.REJECTED
                ):
                    return
                continue
            try:
                frame = client_frame_adapter.validate_json(raw)
            except ValidationError:
                if (
                    self.peer.offer(WSError(code=ErrorCode.INVALID_INPUT))
                    is OfferResult.REJECTED
                ):
                    return
                continue
            room = frame.conversation_id
            if isinstance(frame, Unsubscribe):
                await self.hub.unsubscribe(self.peer, room)
            elif await self.subscribe(actor.user_id, room) is OfferResult.REJECTED:
                return

    async def subscribe(self, actor: UUID, room: UUID) -> OfferResult:
        if room not in self.peer.rooms and len(self.peer.rooms) >= MAX_SUBSCRIPTIONS:
            return self.peer.offer(WSError(code=ErrorCode.HTTP_ERROR))
        try:
            await self.service.head(actor, room)
            await self.hub.subscribe(self.peer, room)
            # 등록 사이 이벤트는 history와 병합하므로 등록 후 head를 다시 읽습니다.
            head = await self.service.head(actor, room)
        except ConversationNotFoundError:
            error = WSError(code=ErrorCode.CONVERSATION_NOT_FOUND)
        except DatabaseBusy, DatabasePoolTimeout:
            error = WSError(code=ErrorCode.DATABASE_BUSY)
        except Exception:
            error = WSError(code=ErrorCode.INTERNAL_ERROR)
        else:
            return self.peer.offer(Subscribed(conversation_id=room, head_seq=str(head)))
        await self.hub.unsubscribe(self.peer, room)
        return self.peer.offer(error)

    async def close(self, code: int) -> None:
        for task in self.tasks:
            task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*self.tasks, return_exceptions=True)
            try:
                async with asyncio.timeout(1):
                    await self.socket.close(code=code)
            except RuntimeError, WebSocketDisconnect, TimeoutError:
                pass
            finally:
                self.peer.mark_closed()
                await self.hub.detach(self.peer)
