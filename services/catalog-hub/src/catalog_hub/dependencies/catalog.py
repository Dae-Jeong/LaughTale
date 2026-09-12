"""자원 provider (Laughtale 캐싱 실험).

provider만 app.state에 접근하고 업무 함수는 일반 인자를 받습니다.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """요청당 세션 하나입니다. 읽기 전용 조회이므로 commit하지 않습니다."""
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


type SessionDep = Annotated[AsyncSession, Depends(get_session)]
