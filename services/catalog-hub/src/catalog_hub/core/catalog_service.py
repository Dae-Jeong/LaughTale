"""카탈로그 조회 업무 (Laughtale 캐싱 실험).

정상 조회 순서가 그대로 읽히도록 한 함수에 두었습니다.

    1. current release 조회 (DB 쿼리 1회 — 캐시 hit에도 남음, D4)
    2. 캐시 조회 (키에 release_id 포함 → stale 없음)
    3. miss면 본문 쿼리 다회 + 트리 조립 + i18n 치환
    4. 캐시에 저장

Step 0에서는 캐시가 꺼져 있어 2·4가 건너뛰어지고 매 요청이 3을 수행합니다.
"""

from dataclasses import dataclass

from catalog_hub.core.assembled import AssembledCatalog, CacheKey
from catalog_hub.core.response_cache import ResponseCache
from catalog_hub.repository.catalog import CatalogRepository

CATALOG_ENDPOINT = "catalog.full"


@dataclass(frozen=True, slots=True)
class CatalogResult:
    """조회 결과와 그 요청이 캐시를 맞혔는지입니다."""

    catalog: AssembledCatalog
    cache_hit: bool


async def read_catalog(
    repository: CatalogRepository,
    cache: ResponseCache | None,
    *,
    lang: str,
) -> CatalogResult:
    """조립된 카탈로그를 돌려줍니다.

    `cache`가 None이면 Step 0 기준선(캐시 없음)입니다.
    """
    release = await repository.current_release()

    if cache is None:
        catalog = await repository.assemble_catalog(release, lang)
        return CatalogResult(catalog=catalog, cache_hit=False)

    key = CacheKey(release_id=release.id, endpoint=CATALOG_ENDPOINT, lang=lang)
    cached = cache.get(key)
    if cached is not None:
        return CatalogResult(catalog=cached, cache_hit=True)

    catalog = await repository.assemble_catalog(release, lang)
    cache.put(key, catalog)
    return CatalogResult(catalog=catalog, cache_hit=False)
