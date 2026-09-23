"""단일 프로세스 로컬 실험용 세션입니다. 운영 인증으로 사용하지 않습니다."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from secrets import token_urlsafe
from typing import Protocol
from uuid import UUID

from chat_service.contracts.chat import Actor
from chat_service.core.contracts import Clock
from chat_service.exceptions.chat import UnauthenticatedError

COOKIE_NAME = "chat_session"
SYNTHETIC_USERS = {
    "user_a": UUID("00000000-0000-4000-8000-000000000001"),
    "user_b": UUID("00000000-0000-4000-8000-000000000002"),
}
SEED_ROOM = UUID("00000000-0000-4000-8000-000000000010")


@dataclass(frozen=True, slots=True)
class Session:
    actor: Actor
    expires_at: datetime


@dataclass
class LocalSessions:
    clock: Clock
    ttl_seconds: int = 28800
    capacity: int = 256
    sessions: dict[str, Session] = field(default_factory=dict, repr=False)

    def issue(self, actor: Actor, previous: str | None = None) -> str:
        now = self.clock()
        self.sessions = {
            key: value for key, value in self.sessions.items() if value.expires_at > now
        }
        if previous:
            self.sessions.pop(previous, None)
        if len(self.sessions) >= self.capacity:
            # 가장 오래된 세션부터 폐기하며 서버 메모리를 무한히 늘리지 않습니다.
            del self.sessions[next(iter(self.sessions))]
        token = token_urlsafe(32)
        self.sessions[token] = Session(actor, now + timedelta(seconds=self.ttl_seconds))
        return token

    def resolve(self, token: str | None) -> Actor:
        session = self.sessions.get(token or "")
        if session is None or session.expires_at <= self.clock():
            self.sessions.pop(token or "", None)
            raise UnauthenticatedError()
        return session.actor

    def revoke(self, token: str | None) -> None:
        self.sessions.pop(token or "", None)


class SessionStore(Protocol):
    async def issue(self, actor: Actor, previous: str | None = None) -> str: ...
    async def resolve(self, token: str | None) -> Actor: ...
    async def revoke(self, token: str | None) -> None: ...
    async def resolve_many(self, tokens: tuple[str, ...]) -> dict[str, Actor]: ...


@dataclass
class LocalSessionStore:
    """기존 동기 로컬 저장소를 비동기 호출 경계에 연결합니다."""

    local: LocalSessions

    async def issue(self, actor: Actor, previous: str | None = None) -> str:
        return self.local.issue(actor, previous)

    async def resolve(self, token: str | None) -> Actor:
        return self.local.resolve(token)

    async def revoke(self, token: str | None) -> None:
        self.local.revoke(token)

    async def resolve_many(self, tokens: tuple[str, ...]) -> dict[str, Actor]:
        if len(tokens) > 256:
            raise ValueError("Session batch limit")
        actors = {}
        for token in tokens:
            try:
                actors[token] = self.local.resolve(token)
            except UnauthenticatedError:
                continue
        return actors
