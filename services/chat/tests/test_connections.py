import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import WebSocket

from chat_service.contracts.chat import Actor
from chat_service.core.chat_hub import ChatHub, Peer
from chat_service.core.metrics import create_metrics
from chat_service.core.peer import OfferResult
from chat_service.core.sessions import SessionStore
from chat_service.exceptions.database import ChatResourcesUnavailable, DatabaseBusy
from chat_service.schemas.chat import Subscribe, Subscribed, WSError
from chat_service.services.chat import ChatService
from chat_service.services.external import ExternalService
from chat_service.transports.chat_ws import ChatConnection
from chat_service.transports.external_ws import ConnectionSlots, ExternalConnection


def socket() -> Mock:
    value = Mock(spec=WebSocket)
    value.cookies = {}
    return value


def sessions() -> Mock:
    value = Mock(spec=SessionStore)
    value.resolve = AsyncMock(return_value=Actor(uuid4(), "synthetic"))
    return value


@pytest.mark.parametrize("cancel", [False, True])
def test_chat_cleanup_owns_tasks_and_detaches_after_close(cancel):
    async def scenario():
        hub, ws = ChatHub(create_metrics()), socket()
        connection = ChatConnection(sessions(), lambda: Mock(spec=ChatService), hub)
        receiving = asyncio.Event()

        async def receive():
            receiving.set()
            if cancel:
                await asyncio.Future()
            return {"type": "websocket.disconnect"}

        async def close(*, code):
            assert all(task.done() for task in connection.tasks)
            assert connection.peer in hub.peers
            assert code == 1000

        ws.receive.side_effect = receive
        ws.close.side_effect = close
        running = asyncio.create_task(connection.run(cast(WebSocket, ws)))
        try:
            await asyncio.wait_for(receiving.wait(), 1)
            if cancel:
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            else:
                await running
            ws.close.assert_awaited_once_with(code=1000)
            assert not hub.peers
            assert all(task.done() for task in connection.tasks)
            await asyncio.wait_for(connection.peer.wait_closed(), 1)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


def test_partial_worker_creation_failure_does_not_leak_task_or_coroutine(monkeypatch):
    async def scenario():
        hub, ws = ChatHub(create_metrics()), socket()
        connection = ChatConnection(sessions(), lambda: Mock(spec=ChatService), hub)
        create = asyncio.create_task
        calls, rejected = 0, []

        def start(work):
            nonlocal calls
            calls += 1
            if calls == 2:
                rejected.append(work)
                raise RuntimeError("synthetic task creation failure")
            return create(work)

        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "create_task", start)
            await connection.run(cast(WebSocket, ws))
        assert len(connection.tasks) == 1 and connection.tasks[0].done()
        assert rejected[0].cr_frame is None
        ws.close.assert_awaited_once_with(code=1011)
        assert not hub.peers
        await asyncio.wait_for(connection.peer.wait_closed(), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_second", [False, True])
def test_subscription_reads_head_on_both_sides_of_registration(fail_second):
    async def scenario():
        actor, room, order = uuid4(), uuid4(), []
        hub, service = ChatHub(create_metrics()), Mock(spec=ChatService)
        connection = ChatConnection(sessions(), lambda: service, hub)
        connection.peer = Peer(actor_id=actor)
        connection.service = service

        async def head(user, conversation):
            assert (user, conversation) == (actor, room)
            order.append(room in connection.peer.rooms)
            if fail_second and len(order) == 2:
                raise DatabaseBusy()
            return 5

        service.head = AsyncMock(side_effect=head)
        assert await connection.subscribe(actor, room) is OfferResult.ACCEPTED
        assert order == [False, True]
        frame = await connection.peer.next_frame()
        if fail_second:
            assert not connection.peer.rooms
            assert WSError.model_validate_json(frame).code == "DATABASE_BUSY"
        else:
            assert connection.peer.rooms == {room}
            assert Subscribed.model_validate_json(frame).head_seq == "5"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stage", ["auth", "factory", "accept", "subscribe", "send", "receive"]
)
def test_external_failures_release_only_acquired_slot(stage):
    async def scenario():
        store, ws, slots = sessions(), socket(), ConnectionSlots(active=7)
        service = Mock(spec=ExternalService)
        service.head = AsyncMock(return_value=0)
        factory = Mock(return_value=service)
        ws.receive_text.return_value = Subscribe(
            conversation_id=uuid4()
        ).model_dump_json()
        ws.receive.return_value = {"type": "websocket.disconnect"}
        failures = {
            "auth": store.resolve,
            "factory": factory,
            "accept": ws.accept,
            "subscribe": ws.receive_text,
            "send": ws.send_text,
            "receive": ws.receive,
        }
        failures[stage].side_effect = (
            ChatResourcesUnavailable()
            if stage == "factory"
            else RuntimeError("synthetic")
        )
        await ExternalConnection(store, factory, slots).run(cast(WebSocket, ws))
        assert slots.active == 7
        ws.close.assert_awaited_once_with(code=1013 if stage == "factory" else 1008)
        if stage == "auth":
            factory.assert_not_called()

    asyncio.run(scenario())


def test_external_cancellation_releases_slot_and_slots_are_not_global():
    async def scenario():
        first, second = ConnectionSlots(limit=1), ConnectionSlots(limit=1)
        ws, store, entered = socket(), sessions(), asyncio.Event()

        async def authenticate(token):
            entered.set()
            await asyncio.Future()

        store.resolve.side_effect = authenticate
        connection = ExternalConnection(
            store, lambda: Mock(spec=ExternalService), first
        )
        running = asyncio.create_task(connection.run(cast(WebSocket, ws)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert first.active == 1 and second.active == 0
            assert not first.acquire()
            assert second.acquire()
            second.release()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert first.active == second.active == 0
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())
