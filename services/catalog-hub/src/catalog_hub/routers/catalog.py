"""카탈로그 조회 라우터 (Laughtale 캐싱 실험).

Step 0의 구간 경계가 여기서 확정됩니다.

    STAGE_ROUTING    : 핸들러 진입까지 (라우팅·쿼리 검증)
    STAGE_RELEASE/DB/ASSEMBLE : repository가 표시
    STAGE_SERIALIZE  : 조립 객체 → 응답 스키마 → JSON 바이트

직렬화를 **라우터에서 직접 수행**하고 그 구간을 재는 이유는, FastAPI가 반환값을
자동 직렬화하면 그 비용이 핸들러 바깥에서 발생해 구간에 잡히지 않기 때문입니다.
`Response`를 직접 만들어 직렬화를 측정 안으로 들여왔습니다 (D1 — 직렬화는 캐시
hit에도 남으므로 반드시 분리해야 합니다).
"""

import json
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from catalog_hub.core.catalog_service import read_catalog
from catalog_hub.core.settings import DEFAULT_LANG, SUPPORTED_LANGS
from catalog_hub.core.stage_timer import (
    STAGE_ROUTING,
    STAGE_SERIALIZE,
    StageSample,
    create_stage_timer,
)
from catalog_hub.dependencies.catalog import SessionDep
from catalog_hub.repository.catalog import CatalogRepository
from catalog_hub.schemas.catalog import to_catalog_data

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])

LangQuery = Annotated[str, Query(pattern="^[a-z]{2,3}$")]

# 서버 처리 시간을 클라이언트가 읽을 수 있게 헤더로 돌려줍니다.
# 클라이언트 전체 지연 - 이 값 = 네트워크·프레임워크 바깥 구간입니다.
SERVER_TIMING_HEADER = "X-Server-Total-Ns"
STAGE_HEADER = "X-Stage-Timing"
CACHE_HEADER = "X-Cache"

# 어느 인스턴스가 응답했는지 표시합니다. Pod별 hit률·전파 지연·rolling update 구간의
# 구·신 Pod 구분에 필요합니다. 단일 프로세스 실행에서는 호스트명이 들어갑니다.
INSTANCE_HEADER = "X-Instance"
RELEASE_HEADER = "X-Release-Id"


@router.get("/full")
async def read_full_catalog(
    request: Request,
    session: SessionDep,
    lang: LangQuery = DEFAULT_LANG,
) -> Response:
    """전체 카탈로그를 조립해 돌려줍니다.

    지원하지 않는 lang은 기본 언어로 대체합니다 (합성 fallback).
    """
    app_state = request.app.state
    timer = create_stage_timer(enabled=app_state.settings.stage_timing_enabled)
    resolved_lang = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    timer.mark(STAGE_ROUTING)

    repository = CatalogRepository(session, timer)
    result = await read_catalog(
        repository, app_state.response_cache, lang=resolved_lang
    )

    payload = to_catalog_data(result.catalog).model_dump()
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    timer.mark(STAGE_SERIALIZE)

    sample = StageSample(
        server_total_ns=timer.total_ns(),
        timing_enabled=timer.enabled(),
        cache_hit=result.cache_hit,
        stages=timer.report(),
    )
    app_state.stage_samples.append(sample)

    headers = {
        SERVER_TIMING_HEADER: str(sample.server_total_ns),
        CACHE_HEADER: "hit" if result.cache_hit else "miss",
        INSTANCE_HEADER: app_state.instance_id,
        RELEASE_HEADER: str(result.catalog.release_id),
    }
    if sample.stages:
        headers[STAGE_HEADER] = json.dumps(sample.stages, separators=(",", ":"))
    return Response(content=body, media_type="application/json", headers=headers)
