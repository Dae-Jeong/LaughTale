from http import HTTPStatus

from fastapi import APIRouter, Request, Response

from chat_service.core.contracts import HealthStatus
from chat_service.dependencies.gateway import GatewayServiceDep, get_recipient
from chat_service.http.errors import PROBLEM_RESPONSES
from chat_service.schemas.gateway import GatewayAcceptance, GatewayDelivery
from chat_service.schemas.responses import Problem, Success

router = APIRouter()


@router.get("/health/live")
async def live(request: Request) -> dict[str, str]:
    return {
        "status": HealthStatus.ALIVE,
        "instance_id": str(request.app.state.target.instance_id),
    }


@router.get("/health/ready", response_model=None)
async def ready(request: Request, response: Response) -> dict[str, str]:
    app = request.app
    ok = app.state.ready and app.state.chat_hub.healthy()
    response.status_code = HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE
    return {
        "status": HealthStatus.READY if ok else HealthStatus.UNAVAILABLE,
        "instance_id": str(app.state.target.instance_id),
    }


@router.post(
    "/internal/events",
    response_model=None,
    responses={
        **PROBLEM_RESPONSES,
        **{
            status: {"model": Problem}
            for status in (
                HTTPStatus.FORBIDDEN,
                HTTPStatus.CONFLICT,
                HTTPStatus.CONTENT_TOO_LARGE,
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        },
    },
)
async def deliver(
    body: GatewayDelivery, request: Request, service: GatewayServiceDep
) -> Success[GatewayAcceptance]:
    instance = get_recipient(request, body.instance_id)
    outcome = await service.accept(instance, body.event)
    return Success(
        data=GatewayAcceptance(
            instance_id=instance, event_id=body.event.event_id, outcome=outcome
        )
    )
