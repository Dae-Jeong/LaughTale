"""Opaque token은 반환/조회 인자에만 존재하며 Primary에는 해시만 저장합니다."""

import asyncio
import hashlib
import re
from contextlib import nullcontext
from secrets import token_urlsafe

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chat_service.contracts.chat import Actor
from chat_service.contracts.database import AuthPhase
from chat_service.core.database_metrics import DatabaseMetrics
from chat_service.exceptions.chat import UnauthenticatedError
from chat_service.repositories.sessions import SessionRepository


def session_hash(token: str | None) -> str | None:
    if token is None or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class SharedSessions:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        ttl_seconds: int = 28800,
        capacity: int = 256,
        *,
        metrics: DatabaseMetrics | None = None,
    ) -> None:
        if not 1 <= capacity <= 256 or not 1 <= ttl_seconds <= 28800:
            raise ValueError("Shared session lab bounds")
        self.factory = factory
        self.ttl_seconds = ttl_seconds
        self.capacity = capacity
        self.metrics = metrics

    async def issue(self, actor: Actor, previous: str | None = None) -> str:
        token = token_urlsafe(32)
        token_hash = session_hash(token)
        assert token_hash is not None
        async with asyncio.timeout(5), self.factory() as session, session.begin():
            await SessionRepository(session).issue(
                actor,
                token_hash,
                session_hash(previous),
                self.ttl_seconds,
                self.capacity,
            )
        return token

    async def resolve(self, token: str | None) -> Actor:
        actors = await self.resolve_many((token,) if token is not None else ())
        actor = actors.get(token or "")
        if actor is None:
            raise UnauthenticatedError()
        return actor

    async def resolve_many(self, tokens: tuple[str, ...]) -> dict[str, Actor]:
        if len(tokens) > 256:
            raise ValueError("Session batch limit")
        hashes = {
            token: digest
            for token in tokens
            if (digest := session_hash(token)) is not None
        }
        if not hashes:
            return {}
        async with asyncio.timeout(5), self.factory() as session, session.begin():
            if self.metrics is not None:
                with self.metrics.auth_phase(AuthPhase.POOL):
                    await session.connection()
            with (
                self.metrics.auth_phase(AuthPhase.QUERY)
                if self.metrics is not None
                else nullcontext()
            ):
                actors = await SessionRepository(session).resolve_many(
                    tuple(hashes.values())
                )
        return {
            token: actors[digest]
            for token, digest in hashes.items()
            if digest in actors
        }

    async def revoke(self, token: str | None) -> None:
        digest = session_hash(token)
        if digest is None:
            return
        async with asyncio.timeout(5), self.factory() as session, session.begin():
            await SessionRepository(session).revoke(digest)
