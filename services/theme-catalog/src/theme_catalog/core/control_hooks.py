"""경쟁·취소 시험 전용 제어 hook (LAUGH-KNOWLEDGE-READ-001).

**시험 전용입니다.** 정상 조회에는 I/O await가 없으므로, 갱신과 조회가 겹치는
경계를 시험에서 관찰하려면 그 지점을 명시적으로 열어 줄 장치가 필요합니다.

사용 경계:
  - 설치 위치는 `ThemeCatalog(hooks=...)` 하나뿐이며 기본값은 빈 hook입니다.
  - 성능·측정 회차에서는 절대 설치하지 않습니다 (`tools/measure.py`는 인자를
    받지 않습니다). 따라서 측정 표본에 제어 지연이 섞이지 않습니다.
  - hook은 흐름을 관찰·지연시킬 뿐 반환값·저장 여부를 바꾸지 않습니다.
    업무 판단(늦은 fill 거절, 무효화)은 hook이 아니라 `ThemeCatalog`가 소유합니다.
  - 호출부는 `if hook is not None: await hook(...)` 분기를 직접 씁니다. 정상 경로에
    불필요한 coroutine 생성·await를 더하지 않기 위해 감싸는 helper를 두지 않습니다.

hook이 열어 주는 네 지점은 조회의 update barrier와 같습니다:
snapshot 전 / snapshot 후 / fill 전 / fill 후.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

type ControlHook = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ControlHooks:
    """시험이 조회 경계를 붙잡을 수 있게 하는 지점 모음입니다."""

    before_snapshot: ControlHook | None = None
    after_snapshot: ControlHook | None = None
    before_fill: ControlHook | None = None
    after_fill: ControlHook | None = None

    def enabled(self) -> bool:
        """하나라도 설치되어 있으면 제어 모드입니다. 성능 회차에서는 False여야 합니다."""
        return any(
            (
                self.before_snapshot,
                self.after_snapshot,
                self.before_fill,
                self.after_fill,
            )
        )


NO_HOOKS = ControlHooks()
