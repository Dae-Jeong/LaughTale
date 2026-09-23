from functools import partial
from typing import Annotated
from uuid import UUID

from fastapi import Depends, WebSocket

from chat_service.dependencies.chat import create_service
from chat_service.services.external import ExternalService
from chat_service.transports.chat_ws import ChatConnection
from chat_service.transports.external_ws import ExternalConnection


def get_chat_connection(socket: WebSocket) -> ChatConnection:
    return ChatConnection(
        socket.app.state.sessions,
        partial(create_service, socket.app),
        socket.app.state.chat_hub,
    )


def get_external_connection(socket: WebSocket) -> ExternalConnection:
    app = socket.app

    def service() -> ExternalService:
        # 자원 확인은 연결 제한·인증 이후에 수행합니다.
        return ExternalService(
            create_service(app),
            app.state.clock,
            connection_ids=frozenset(
                UUID(key) for key in app.state.settings.external_connection_credentials
            ),
        )

    return ExternalConnection(app.state.sessions, service, app.state.external_slots)


ChatConnectionDep = Annotated[ChatConnection, Depends(get_chat_connection)]
ExternalConnectionDep = Annotated[ExternalConnection, Depends(get_external_connection)]
