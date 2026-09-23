import asyncio
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from sqlalchemy import select, update
from tests.integration.test_chat_service import A, B, service_database

from chat_service.bootstrap.app import create_app
from chat_service.contracts.chat import Actor
from chat_service.core.settings import Settings
from chat_service.core.shared_sessions import SharedSessions, session_hash
from chat_service.exceptions.chat import UnauthenticatedError
from chat_service.models.sessions import SharedSession

pytestmark = pytest.mark.postgres


def test_api_a_issue_api_b_resolve_rotation(postgres_url: str) -> None:
    async def scenario():
        async with service_database(postgres_url) as service:
            settings = Settings(
                _env_file=None,
                dev_sessions_enabled=True,
                session_backend="postgres",
                network_profile="isolated-lab",
                app_environment="isolated-lab",
                server_host="0.0.0.0",
                db_primary_url=postgres_url,
            )

            async def prepare(app, stack):
                app.state.primary_session_factory = service.factory
                app.state.database_metrics = service.metrics

            app_a = create_app(settings, prepare=prepare)
            app_b = create_app(settings, prepare=prepare)
            async with (
                app_a.router.lifespan_context(app_a),
                app_b.router.lifespan_context(app_b),
            ):
                async with (
                    httpx2.AsyncClient(
                        transport=httpx2.ASGITransport(
                            app_a, client=("127.0.0.1", 1234)
                        ),
                        base_url="http://127.0.0.1:18082",
                    ) as api_a,
                    httpx2.AsyncClient(
                        transport=httpx2.ASGITransport(
                            app_b, client=("127.0.0.1", 1234)
                        ),
                        base_url="http://127.0.0.1:18082",
                    ) as api_b,
                ):
                    origin = {"Origin": "http://127.0.0.1:18083"}
                    issued = await api_a.post(
                        "/v1/dev/session", json={"user": "user_a"}, headers=origin
                    )
                    assert issued.status_code == 200
                    cookie = issued.headers["set-cookie"].split(";", 1)[0]
                    resolved = await api_b.get(
                        "/v1/session", headers={"Cookie": cookie}
                    )
                    assert resolved.status_code == 200 and resolved.json()["data"][
                        "user_id"
                    ] == str(A)
                    replaced = await api_b.post(
                        "/v1/dev/session",
                        json={"user": "user_b"},
                        headers=origin | {"Cookie": cookie},
                    )
                    assert replaced.status_code == 200
                    assert (
                        await api_a.get("/v1/session", headers={"Cookie": cookie})
                    ).status_code == 401

    asyncio.run(scenario())


def test_two_instances_share_issue_revoke_expiry_and_hash_only(
    postgres_url: str,
) -> None:
    async def scenario():
        async with service_database(postgres_url) as service:
            issuer = SharedSessions(service.factory)
            resolver = SharedSessions(service.factory)
            actor = await service.actor(A)
            token = await issuer.issue(actor)
            assert await resolver.resolve(token) == actor
            async with service.factory() as session:
                rows = (await session.scalars(select(SharedSession))).all()
                assert len(rows) == 1
                assert rows[0].token_hash == session_hash(token)
                assert rows[0].token_hash != token and len(rows[0].token_hash) == 64
                assert (
                    28798
                    < (rows[0].expires_at - rows[0].created_at).total_seconds()
                    < 28802
                )
            await resolver.revoke(token)
            with pytest.raises(UnauthenticatedError):
                await issuer.resolve(token)
            expired = await issuer.issue(actor)
            async with service.factory() as session, session.begin():
                await session.execute(
                    update(SharedSession)
                    .where(SharedSession.token_hash == session_hash(expired))
                    .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
            with pytest.raises(UnauthenticatedError):
                await resolver.resolve(expired)
            current = await resolver.issue(actor)
            assert await issuer.resolve_many((token, expired, current, "bad")) == {
                current: actor
            }
            async with service.factory() as session:
                assert len((await session.scalars(select(SharedSession))).all()) == 1

    asyncio.run(scenario())


def test_cross_instance_rotation_and_concurrent_capacity(postgres_url: str) -> None:
    async def scenario():
        async with service_database(postgres_url) as service:
            issuer = SharedSessions(service.factory, capacity=2)
            other = SharedSessions(service.factory, capacity=2)
            token_a = await issuer.issue(await service.actor(A))
            token_b = await other.issue(await service.actor(B), token_a)
            with pytest.raises(UnauthenticatedError):
                await issuer.resolve(token_a)
            assert (await issuer.resolve(token_b)).user_id == B
            await asyncio.gather(
                *(store.issue(Actor(A, "synthetic")) for store in [issuer, other] * 4)
            )
            async with service.factory() as session:
                rows = (await session.scalars(select(SharedSession))).all()
                assert len(rows) == 2
            with pytest.raises(UnauthenticatedError):
                await issuer.resolve(token_b)

    asyncio.run(scenario())
