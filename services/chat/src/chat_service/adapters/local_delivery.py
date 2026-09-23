from chat_service.contracts.chat import ChatMessage
from chat_service.core.chat_hub import ChatHub
from chat_service.core.peer import CloseReason
from chat_service.schemas.chat import MessageCreated, MessageData


class LocalDelivery:
    def __init__(self, hub: ChatHub) -> None:
        self.hub = hub

    def notify(self, message: ChatMessage) -> None:
        data = MessageData.from_internal(message)
        event = MessageCreated(event_id=data.message_id, message=data)
        try:
            self.hub.publish(event)
        except Exception:
            # 저장은 완료됐습니다. ACK를 보존하고 history 복구를 유도합니다.
            self.hub.request_close_all(CloseReason.DELIVERY_FAILED)
