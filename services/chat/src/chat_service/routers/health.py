from http import HTTPStatus

from fastapi import APIRouter, Request, Response

from chat_service.core.contracts import HealthStatus

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": HealthStatus.ALIVE}


@router.get(
    "/ready",
    response_model=None,
    responses={HTTPStatus.SERVICE_UNAVAILABLE: {"description": "Not ready"}},
)
async def readiness(request: Request, response: Response) -> dict[str, str]:
    ready = request.app.state.ready
    response.status_code = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
    return {"status": HealthStatus.READY if ready else HealthStatus.NOT_READY}
