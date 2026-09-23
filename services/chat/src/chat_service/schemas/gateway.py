from uuid import UUID

from chat_service.contracts.subscriptions import GatewayOutcome
from chat_service.schemas.chat import MessageCreated, WireModel


class GatewayDelivery(WireModel):
    instance_id: UUID
    event: MessageCreated


class GatewayAcceptance(WireModel):
    instance_id: UUID
    event_id: UUID
    outcome: GatewayOutcome
