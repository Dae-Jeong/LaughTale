"""관측 라우터 (Laughtale 캐싱 실험).

구간 표본과 캐시 상태를 그대로 노출합니다. 기능 검증·측정 분모 확인용이며
성능 우위의 근거가 아닙니다.
"""

from fastapi import APIRouter, Request

from catalog_hub.core.stage_timer import STAGE_ORDER, StageSample
from catalog_hub.schemas.responses import Success

router = APIRouter(prefix="/v1/stats", tags=["stats"])


@router.get("")
async def read_stats(request: Request) -> Success[dict[str, object]]:
    """현재 프로세스의 구간 표본 요약과 캐시 상태입니다."""
    state = request.app.state
    samples: list[StageSample] = state.stage_samples
    cache = state.response_cache
    return Success(
        data={
            "instance_id": state.instance_id,
            "sample_count": len(samples),
            "cache_enabled": cache is not None,
            "cache": cache.stats() if cache is not None else None,
            "stage_timing_enabled": state.settings.stage_timing_enabled,
            "stage_order": list(STAGE_ORDER),
        }
    )


@router.post("/reset")
async def reset_stats(request: Request) -> Success[dict[str, object]]:
    """표본 버퍼를 비웁니다. 회차 사이에 호출합니다."""
    state = request.app.state
    dropped = len(state.stage_samples)
    state.stage_samples.clear()
    return Success(data={"dropped_samples": dropped})
