from fastapi import APIRouter, WebSocket

from chat_service.dependencies.connections import ChatConnectionDep

router = APIRouter()


@router.websocket("/v1/ws")
async def websocket_chat(websocket: WebSocket, connection: ChatConnectionDep) -> None:
    await connection.run(websocket)
