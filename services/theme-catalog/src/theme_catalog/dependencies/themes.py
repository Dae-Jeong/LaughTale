"""조회 객체 provider (LAUGH-KNOWLEDGE-READ-001).

provider만 app.state에 접근하고 업무 함수는 일반 인자를 받습니다.
"""

from typing import Annotated

from fastapi import Depends, Request

from theme_catalog.core.catalog import ThemeCatalog


def get_catalog(request: Request) -> ThemeCatalog:
    """앱별 catalog를 전달합니다."""
    return request.app.state.catalog


type CatalogDep = Annotated[ThemeCatalog, Depends(get_catalog)]
