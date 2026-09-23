from uuid import UUID

from chat_service.adapters.local_delivery import LocalDelivery
from chat_service.contracts.chat import Stored
from chat_service.domain.chat import MessagePayload
from chat_service.services.chat import ChatService


class Messaging:
    def __init__(self, chat: ChatService, delivery: LocalDelivery) -> None:
        self.chat = chat
        self.delivery = delivery

    async def send(
        self, actor: UUID, room: UUID, key: UUID, payload: MessagePayload
    ) -> Stored:
        result = await self.chat.store(actor, room, key, payload)
        if not result.replay:
            self.delivery.notify(result.message)
        return result
