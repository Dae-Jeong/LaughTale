from http import HTTPStatus
from typing import Annotated, cast

from fastapi import Depends, FastAPI, Request
from starlette.exceptions import HTTPException

from chat_service.adapters.local_delivery import LocalDelivery
from chat_service.contracts.chat import Actor
from chat_service.core.sessions import COOKIE_NAME, SessionStore
from chat_service.exceptions.database import ChatResourcesUnavailable
from chat_service.services.chat import ChatService
from chat_service.services.messaging import Messaging


def create_service(app: FastAPI) -> ChatService:
    factory = getattr(app.state, "primary_session_factory", None)
    metrics = getattr(app.state, "database_metrics", None)
    if factory is None or metrics is None:
        raise ChatResourcesUnavailable()
    return ChatService(factory, metrics)


def get_chat_service(request: Request) -> ChatService:
    try:
        return create_service(request.app)
    except ChatResourcesUnavailable:
        raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE) from None


async def get_actor(request: Request) -> Actor:
    return await cast(SessionStore, request.app.state.sessions).resolve(
        request.cookies.get(COOKIE_NAME)
    )


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
ActorDep = Annotated[Actor, Depends(get_actor)]


def get_messaging(request: Request, service: ChatServiceDep) -> Messaging:
    return Messaging(service, LocalDelivery(request.app.state.chat_hub))


MessagingDep = Annotated[Messaging, Depends(get_messaging)]
