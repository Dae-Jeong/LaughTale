"""테마 조회·갱신 라우터 (LAUGH-KNOWLEDGE-READ-001).

입력 검증과 응답 변환만 담당합니다. 업무 순서는 `core/catalog.py`가 소유합니다.
"""

from fastapi import APIRouter, Path

from theme_catalog.core.theme import MAX_THEME_ID_LENGTH
from theme_catalog.dependencies.themes import CatalogDep
from theme_catalog.http.errors import EmptyPatchError
from theme_catalog.schemas.responses import Success
from theme_catalog.schemas.themes import ThemeData, ThemePatch, to_theme_data

router = APIRouter(prefix="/v1/themes", tags=["themes"])


@router.get("/{theme_id}")
async def read_theme(
    catalog: CatalogDep,
    theme_id: str = Path(min_length=1, max_length=MAX_THEME_ID_LENGTH),
) -> Success[ThemeData]:
    """단건 조회입니다. 미등록 ID는 명시적 404이며 캐시에 남기지 않습니다."""
    theme = await catalog.get(theme_id)
    return Success(data=to_theme_data(theme))


@router.patch("/{theme_id}")
async def update_theme(
    catalog: CatalogDep,
    patch: ThemePatch,
    theme_id: str = Path(min_length=1, max_length=MAX_THEME_ID_LENGTH),
) -> Success[ThemeData]:
    """드문 갱신입니다. 지정한 속성만 교체하고 해당 key를 무효화합니다.

    갱신 결과는 이 프로세스 수명까지만 유지됩니다.
    """
    if patch.is_empty():
        raise EmptyPatchError
    theme = await catalog.update(
        theme_id,
        background=patch.background,
        foreground=patch.foreground,
        spacing_px=patch.spacing_px,
    )
    return Success(data=to_theme_data(theme))
