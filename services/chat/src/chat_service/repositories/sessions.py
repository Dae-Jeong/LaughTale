from datetime import timedelta

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.contracts.chat import Actor
from chat_service.models.chat import User
from chat_service.models.sessions import SharedSession


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def issue(
        self,
        actor: Actor,
        token_hash: str,
        previous_hash: str | None,
        ttl_seconds: int,
        capacity: int,
    ) -> None:
        # 전체 세션 발급 예산은 여러 API가 공유합니다. 메시지 admission과 별도 lock입니다.
        await self.session.execute(text("SELECT pg_advisory_xact_lock(181832, 2)"))
        await self.session.execute(
            delete(SharedSession).where(
                or_(
                    SharedSession.expires_at <= func.clock_timestamp(),
                    SharedSession.revoked_at.is_not(None),
                )
            )
        )
        if previous_hash:
            await self.revoke(previous_hash)
            await self.session.execute(
                delete(SharedSession).where(SharedSession.token_hash == previous_hash)
            )
        hashes = (
            await self.session.scalars(
                select(SharedSession.token_hash).order_by(
                    SharedSession.created_at, SharedSession.token_hash
                )
            )
        ).all()
        discard = max(0, len(hashes) - capacity + 1)
        if discard:
            await self.session.execute(
                delete(SharedSession).where(
                    SharedSession.token_hash.in_(hashes[:discard])
                )
            )
        now = (await self.session.execute(select(func.clock_timestamp()))).scalar_one()
        self.session.add(
            SharedSession(
                token_hash=token_hash,
                actor_id=actor.user_id,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
        )
        await self.session.flush()

    async def resolve_many(self, hashes: tuple[str, ...]) -> dict[str, Actor]:
        if not hashes:
            return {}
        rows = await self.session.execute(
            select(SharedSession.token_hash, User.id, User.display_name)
            .join(User, User.id == SharedSession.actor_id)
            .where(
                SharedSession.token_hash.in_(hashes),
                SharedSession.revoked_at.is_(None),
                SharedSession.expires_at > func.clock_timestamp(),
            )
        )
        return {
            token_hash: Actor(user_id, display_name)
            for token_hash, user_id, display_name in rows
        }

    async def revoke(self, token_hash: str) -> None:
        await self.session.execute(
            update(SharedSession)
            .where(
                SharedSession.token_hash == token_hash,
                SharedSession.revoked_at.is_(None),
            )
            .values(revoked_at=func.clock_timestamp())
        )
