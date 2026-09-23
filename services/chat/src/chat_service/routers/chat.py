from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from starlette.exceptions import HTTPException

from chat_service.core.sessions import COOKIE_NAME, SYNTHETIC_USERS
from chat_service.dependencies.chat import ActorDep, ChatServiceDep, MessagingDep
from chat_service.http.errors import PROBLEM_RESPONSES
from chat_service.schemas.chat import (
    ActorData,
    ConversationData,
    HistoryResponse,
    SendMessageRequest,
    Seq,
    SessionRequest,
    StoredMessageData,
)
from chat_service.schemas.responses import Problem, Success

router = APIRouter(
    prefix="/v1",
    responses={
        **PROBLEM_RESPONSES,
        **{
            status: {"model": Problem}
            for status in (
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.CONFLICT,
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        },
    },
)


@router.post("/dev/session")
async def create_session(
    body: SessionRequest, request: Request, response: Response, service: ChatServiceDep
) -> Success[ActorData]:
    if not request.app.state.settings.dev_sessions_enabled:
        raise HTTPException(HTTPStatus.NOT_FOUND)
    actor = await service.actor(SYNTHETIC_USERS[body.user])
    token = await request.app.state.sessions.issue(
        actor, request.cookies.get(COOKIE_NAME)
    )
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="strict", max_age=28800, path="/v1"
    )
    response.headers["Cache-Control"] = "no-store"
    return Success(data=ActorData.from_internal(actor))


@router.get("/session")
async def current_session(actor: ActorDep, response: Response) -> Success[ActorData]:
    response.headers["Cache-Control"] = "no-store"
    return Success(data=ActorData.from_internal(actor))


@router.get("/internal-conversations")
async def list_rooms(
    actor: ActorDep, service: ChatServiceDep
) -> Success[list[ConversationData]]:
    rooms = await service.rooms(actor.user_id)
    return Success(data=[ConversationData.from_internal(room) for room in rooms])


@router.post(
    "/internal-conversations/{conversation_id}/messages",
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": Success[StoredMessageData]}},
)
async def send_message(
    conversation_id: UUID,
    body: SendMessageRequest,
    response: Response,
    actor: ActorDep,
    messaging: MessagingDep,
) -> Success[StoredMessageData]:
    result = await messaging.send(
        actor.user_id, conversation_id, body.client_message_id, body.payload()
    )
    response.status_code = HTTPStatus.OK if result.replay else HTTPStatus.CREATED
    return Success(data=StoredMessageData.from_internal(result.message))


@router.get("/internal-conversations/{conversation_id}/messages")
async def history(
    conversation_id: UUID,
    actor: ActorDep,
    service: ChatServiceDep,
    after_seq: Annotated[Seq, Query()] = "0",
    snapshot_head_seq: Annotated[Seq | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> HistoryResponse:
    result = await service.history(
        actor.user_id,
        conversation_id,
        int(after_seq),
        int(snapshot_head_seq) if snapshot_head_seq is not None else None,
        limit,
    )
    return HistoryResponse.from_internal(result)
