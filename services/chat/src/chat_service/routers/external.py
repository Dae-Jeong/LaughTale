from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from platform_contracts.wire import ConnectionSeed, InboundEvent

from chat_service.dependencies.chat import ActorDep
from chat_service.dependencies.external import (
    ExternalServiceDep,
    get_connection,
    require_connection,
    require_control,
)
from chat_service.http.errors import PROBLEM_RESPONSES
from chat_service.schemas.chat import SendMessageRequest, Seq
from chat_service.schemas.external import (
    ExternalConversationData,
    ExternalHistoryResponse,
    ExternalMessageData,
)
from chat_service.schemas.responses import Success

router = APIRouter(prefix="/v1", responses=PROBLEM_RESPONSES)


@router.get("/external-conversations/{conversation_id}/messages/{message_id}")
async def external_message(
    conversation_id: UUID,
    message_id: UUID,
    actor: ActorDep,
    service: ExternalServiceDep,
) -> Success[ExternalMessageData]:
    return Success(
        data=ExternalMessageData.from_internal(
            await service.message(actor.user_id, conversation_id, message_id)
        )
    )


@router.post(
    "/dev/external-connections",
    status_code=HTTPStatus.CREATED,
    dependencies=[Depends(require_control)],
)
async def seed_connection(
    body: ConnectionSeed, request: Request, service: ExternalServiceDep
) -> Success[dict[str, UUID]]:
    get_connection(request, body.connection_id, body.profile)
    return Success(data={"conversation_id": await service.seed(body)})


@router.post("/external-events", status_code=HTTPStatus.CREATED)
async def receive_event(
    body: InboundEvent,
    request: Request,
    response: Response,
    service: ExternalServiceDep,
) -> Success[ExternalMessageData]:
    require_connection(request, body.connection_id, body.profile)
    result = await service.receive(body)
    response.status_code = HTTPStatus.OK if result.replay else HTTPStatus.CREATED
    return Success(data=ExternalMessageData.from_internal(result.message))


@router.get("/external-conversations")
async def external_rooms(
    actor: ActorDep, service: ExternalServiceDep
) -> Success[list[ExternalConversationData]]:
    return Success(
        data=[
            ExternalConversationData.from_internal(room)
            for room in await service.rooms(actor.user_id)
        ]
    )


@router.post(
    "/external-conversations/{conversation_id}/messages", status_code=HTTPStatus.CREATED
)
async def send_external(
    conversation_id: UUID,
    body: SendMessageRequest,
    response: Response,
    actor: ActorDep,
    service: ExternalServiceDep,
) -> Success[ExternalMessageData]:
    result = await service.send(
        actor.user_id, conversation_id, body.client_message_id, body.payload()
    )
    response.status_code = HTTPStatus.OK if result.replay else HTTPStatus.CREATED
    return Success(data=ExternalMessageData.from_internal(result.message))


@router.get("/external-conversations/{conversation_id}/messages")
async def external_history(
    conversation_id: UUID,
    actor: ActorDep,
    service: ExternalServiceDep,
    after_seq: Annotated[Seq, Query()] = "0",
    snapshot_head_seq: Annotated[Seq | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ExternalHistoryResponse:
    result = await service.history(
        actor.user_id,
        conversation_id,
        int(after_seq),
        int(snapshot_head_seq) if snapshot_head_seq is not None else None,
        limit,
    )
    return ExternalHistoryResponse.from_internal(result)
