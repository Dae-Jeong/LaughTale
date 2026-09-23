from uuid import UUID

from chat_service.contracts.subscriptions import (
    GatewayOutcome,
    GatewayTarget,
    RegistryUnavailable,
)
from chat_service.core.gateway_hub import GatewayHub
from chat_service.exceptions.gateway import (
    EventIdentityMismatch,
    InstanceMismatch,
    ResyncUnconfirmed,
)
from chat_service.schemas.chat import MessageCreated


class GatewayService:
    def __init__(self, target: GatewayTarget, hub: GatewayHub) -> None:
        self.target = target
        self.hub = hub

    async def accept(self, recipient: UUID, event: MessageCreated) -> GatewayOutcome:
        if recipient != self.target.instance_id:
            raise InstanceMismatch()
        if event.event_id != event.message.message_id:
            raise EventIdentityMismatch()
        if not self.hub.healthy():
            raise RegistryUnavailable()
        try:
            return await self.hub.deliver(event)
        except TimeoutError:
            raise ResyncUnconfirmed() from None
