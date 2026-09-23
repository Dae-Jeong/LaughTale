from http import HTTPStatus
from typing import cast

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from chat_service.core.metrics import HttpMetrics

router = APIRouter(tags=["metrics"])


@router.get(
    "/metrics",
    response_class=Response,
    responses={
        HTTPStatus.OK: {"content": {"text/plain": {}}},
        HTTPStatus.SERVICE_UNAVAILABLE: {"description": "Metrics unavailable"},
    },
)
def metrics(request: Request) -> Response:
    state = cast(HttpMetrics, request.app.state.metrics)
    if state.failed:
        return Response(
            "Metrics unavailable\n",
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            media_type="text/plain",
        )
    try:
        return Response(
            generate_latest(state.registry),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )
    except Exception:
        state.failed = True
        return Response(
            "Metrics unavailable\n",
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            media_type="text/plain",
        )
