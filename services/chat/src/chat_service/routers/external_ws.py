from fastapi import APIRouter, WebSocket

from chat_service.dependencies.connections import ExternalConnectionDep

router = APIRouter()


@router.websocket("/v1/external-ws")
async def external_socket(socket: WebSocket, connection: ExternalConnectionDep) -> None:
    await connection.run(socket)
