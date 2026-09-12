"""계수 조회 라우터 (LAUGH-KNOWLEDGE-READ-001).

hit/miss/load/eviction·갱신·오류 계수를 그대로 노출합니다. 이 값은 기능 검증과
측정 분모 확인용이며 성능 우위의 근거가 아닙니다.
"""

from fastapi import APIRouter

from theme_catalog.dependencies.themes import CatalogDep
from theme_catalog.schemas.responses import Success

router = APIRouter(prefix="/v1/stats", tags=["stats"])


@router.get("")
async def read_stats(catalog: CatalogDep) -> Success[dict[str, object]]:
    """현재 프로세스의 조회·갱신 계수입니다.

    캐시 존재 판정은 `is not None`으로 합니다. `BoundedLruCache`는 `__len__`을
    제공하므로 항목이 0개인 캐시는 falsy입니다. 진위 판정을 쓰면 cache-on이지만
    아직 비어 있는 cold 상태나 마지막 항목을 무효화한 직후에 capacity가 null로
    잘못 보고됩니다.
    """
    cache = catalog.cache
    return Success(
        data={
            "cache_enabled": catalog.cache_enabled,
            "cache_capacity": cache.capacity if cache is not None else None,
            "cache_entries": len(cache) if cache is not None else 0,
            "origin_theme_count": len(catalog.store),
            "origin_load_count": catalog.store.load_count,
            "origin_update_count": catalog.store.update_count,
            "counters": catalog.counters.snapshot(),
        }
    )
