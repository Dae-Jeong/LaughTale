import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from chat_service.adapters.local_delivery import LocalDelivery
from chat_service.contracts.chat import ChatMessage, Stored
from chat_service.core.chat_hub import ChatHub
from chat_service.core.peer import CloseReason
from chat_service.domain.chat import MessagePayload
from chat_service.exceptions.database import DatabaseBusy
from chat_service.schemas.chat import MessageCreated, MessageData, StoredMessageData
from chat_service.services.chat import ChatService
from chat_service.services.messaging import Messaging


def message() -> ChatMessage:
    return ChatMessage(
        message_id=uuid4(),
        conversation_id=uuid4(),
        sender_id=uuid4(),
        client_message_id=uuid4(),
        seq=9007199254740993,
        text="synthetic",
        created_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("replay", [False, True])
def test_send_waits_for_store_and_only_notifies_new_messages(replay):
    async def scenario():
        stored = Stored(message(), replay)
        payload = MessagePayload(stored.message.text)
        storing, release = asyncio.Event(), asyncio.Event()
        chat, delivery = Mock(spec=ChatService), Mock(spec=LocalDelivery)

        async def store(*args):
            storing.set()
            await release.wait()
            return stored

        chat.store = AsyncMock(side_effect=store)
        messaging = Messaging(chat, delivery)
        task = asyncio.create_task(
            messaging.send(
                stored.message.sender_id,
                stored.message.conversation_id,
                stored.message.client_message_id,
                payload,
            )
        )
        try:
            await asyncio.wait_for(storing.wait(), 1)
            delivery.notify.assert_not_called()
            release.set()
            assert await task is stored
            chat.store.assert_awaited_once_with(
                stored.message.sender_id,
                stored.message.conversation_id,
                stored.message.client_message_id,
                payload,
            )
            if replay:
                delivery.notify.assert_not_called()
            else:
                delivery.notify.assert_called_once_with(stored.message)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_store_failure_does_not_notify():
    async def scenario():
        chat, delivery = Mock(spec=ChatService), Mock(spec=LocalDelivery)
        chat.store = AsyncMock(side_effect=DatabaseBusy())
        with pytest.raises(DatabaseBusy):
            await Messaging(chat, delivery).send(
                uuid4(), uuid4(), uuid4(), MessagePayload("x")
            )
        delivery.notify.assert_not_called()

    asyncio.run(scenario())


def test_local_delivery_serializes_existing_event_contract():
    value, hub = message(), Mock(spec=ChatHub)
    LocalDelivery(hub).notify(value)
    hub.publish.assert_called_once_with(
        MessageCreated(
            event_id=value.message_id,
            message=MessageData.from_internal(value),
        )
    )
    hub.request_close_all.assert_not_called()
    wire = StoredMessageData.from_internal(value).model_dump(mode="json")
    assert wire["seq"] == "9007199254740993"
    assert wire["state"] == "stored"
    assert wire["created_at"].endswith("Z")


def test_publish_failure_preserves_stored_result_and_requests_recovery():
    async def scenario():
        stored, hub = Stored(message(), False), Mock(spec=ChatHub)
        chat = Mock(spec=ChatService)
        chat.store = AsyncMock(return_value=stored)
        hub.publish.side_effect = RuntimeError("synthetic publish failure")
        result = await Messaging(chat, LocalDelivery(hub)).send(
            uuid4(),
            uuid4(),
            uuid4(),
            MessagePayload("x"),
        )
        assert result is stored
        hub.request_close_all.assert_called_once_with(CloseReason.DELIVERY_FAILED)

    asyncio.run(scenario())


def test_conversion_failure_is_not_misclassified_as_delivery_failure():
    hub = Mock(spec=ChatHub)
    with pytest.raises(ValidationError):
        LocalDelivery(hub).notify(replace(message(), seq=0))
    hub.publish.assert_not_called()
    hub.request_close_all.assert_not_called()


def test_recovery_failure_is_not_hidden():
    hub = Mock(spec=ChatHub)
    hub.publish.side_effect = RuntimeError("synthetic publish failure")
    hub.request_close_all.side_effect = RuntimeError("synthetic recovery failure")
    with pytest.raises(RuntimeError, match="synthetic recovery failure"):
        LocalDelivery(hub).notify(message())
