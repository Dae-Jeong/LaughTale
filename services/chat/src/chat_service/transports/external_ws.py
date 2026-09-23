import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from chat_service.contracts.chat import Actor
from chat_service.core.sessions import COOKIE_NAME, SessionStore
from chat_service.exceptions.database import ChatResourcesUnavailable
from chat_service.schemas.chat import Subscribe
from chat_service.schemas.external import ExternalHead
from chat_service.services.external import ExternalService


@dataclass
class ConnectionSlots:
    """단일 app 이벤트 루프에서 대기 없이 연결 수를 제한합니다."""

    limit: int = 128
    active: int = 0

    def acquire(self) -> bool:
        if self.active >= self.limit:
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active -= 1


class ExternalConnection:
    def __init__(
        self,
        sessions: SessionStore,
        service: Callable[[], ExternalService],
        slots: ConnectionSlots,
    ) -> None:
        self.sessions = sessions
        self.create_service = service
        self.slots = slots
        self.socket: WebSocket
        self.token: str | None
        self.actor: Actor
        self.service: ExternalService
        self.room: UUID

    async def run(self, socket: WebSocket) -> None:
        self.socket = socket
        if not self.slots.acquire():
            await self.close(1013)
            return
        try:
            self.token = socket.cookies.get(COOKIE_NAME)
            self.actor = await self.sessions.resolve(self.token)
            try:
                self.service = self.create_service()
            except ChatResourcesUnavailable:
                await self.close(1013)
                return
            await socket.accept()
            if await self.subscribe():
                await self.poll()
        except WebSocketDisconnect, OSError:
            return
        except ValidationError, TimeoutError:
            await self.close(1008)
        except Exception:
            await self.close(1008)
        finally:
            self.slots.release()

    async def subscribe(self) -> bool:
        async with asyncio.timeout(10):
            raw = await self.socket.receive_text()
        if len(raw.encode()) > 16384:
            await self.close(1009)
            return False
        self.room = Subscribe.model_validate_json(raw).conversation_id
        return True

    async def poll(self) -> None:
        while True:
            # 구독 하나, queue 없음, tick마다 세션·회원권한을 재확인합니다.
            if (await self.sessions.resolve(self.token)).user_id != self.actor.user_id:
                await self.close(1008)
                return
            head = ExternalHead(
                conversation_id=self.room,
                head_seq=str(await self.service.head(self.actor.user_id, self.room)),
            )
            async with asyncio.timeout(5):
                await self.socket.send_text(head.model_dump_json())
            try:
                async with asyncio.timeout(1):
                    message = await self.socket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    await self.close(1008)
                    return
            except TimeoutError:
                pass

    async def close(self, code: int) -> None:
        await self.socket.close(code=code)
