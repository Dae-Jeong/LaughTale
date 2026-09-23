"""명시적으로 실행하는 로컬 합성 DM seed입니다. migration·자동 시작 hook은 없습니다."""

import argparse
import asyncio

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chat_service.core.database import create_primary_engine
from chat_service.core.database_metrics import create_database_metrics
from chat_service.core.metrics import create_metrics
from chat_service.core.sessions import SEED_ROOM, SYNTHETIC_USERS
from chat_service.core.settings import Settings
from chat_service.models.chat import Conversation, Member, User


async def seed_rows(session: AsyncSession) -> None:
    """호출자가 소유하는 트랜잭션에 참여하며 기존 데이터는 덮어쓰지 않습니다."""
    for key, user_id in SYNTHETIC_USERS.items():
        if await session.get(User, user_id) is None:
            session.add(
                User(id=user_id, display_name="User A" if key == "user_a" else "User B")
            )
    if await session.get(Conversation, SEED_ROOM) is None:
        session.add(Conversation(id=SEED_ROOM))
    await session.flush()
    for user_id in SYNTHETIC_USERS.values():
        if await session.get(Member, (SEED_ROOM, user_id)) is None:
            session.add(Member(conversation_id=SEED_ROOM, user_id=user_id))
    await session.flush()


def validate_seed_settings(settings: Settings) -> None:
    url = make_url(settings.db_primary_url)
    if not settings.dev_sessions_enabled or (
        url.drivername,
        url.host,
        url.port,
        url.database,
        url.username,
    ) != ("postgresql+asyncpg", "127.0.0.1", 5440, "laughtale_chat", "chat_writer"):
        raise ValueError("Seed requires the explicitly enabled local lab writer target")


async def seed_local(settings: Settings) -> None:
    validate_seed_settings(settings)
    metrics = create_database_metrics(create_metrics(), 1)
    engine = create_primary_engine(settings, metrics)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            identity = (
                await session.execute(
                    text("SELECT current_database(),current_user,pg_is_in_recovery()")
                )
            ).one()
            if tuple(identity) != ("laughtale_chat", "chat_writer", False):
                raise ValueError("Unexpected seed database identity")
            await seed_rows(session)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Explicitly insert synthetic users and DM after approved migration",
    )
    args = parser.parse_args()
    if not args.apply:
        parser.error("Explicit --apply is required; no data changed")
    try:
        asyncio.run(seed_local(Settings()))
    except Exception:
        raise SystemExit(
            "Seed failed; details suppressed to protect database credentials"
        ) from None
    print("Synthetic users and DM are ready")


if __name__ == "__main__":
    main()
