from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request

from chat_service.exceptions.gateway import InstanceMismatch
from chat_service.services.gateway import GatewayService


def get_gateway(request: Request) -> GatewayService:
    return GatewayService(request.app.state.target, request.app.state.chat_hub)


def get_recipient(request: Request, instance: UUID) -> UUID:
    # Run after body validation so malformed input still takes precedence.
    if request.headers.getlist("x-gateway-instance-id") != [str(instance)]:
        raise InstanceMismatch()
    return instance


GatewayServiceDep = Annotated[GatewayService, Depends(get_gateway)]
