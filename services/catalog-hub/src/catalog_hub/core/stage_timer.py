"""요청 내 구간 타이머 (Step 0 구간 분해).

Step 0의 필수 분리 구간 6개 중 서버 내부 5개를 이 타이머가 잽니다.
네트워크 구간은 서버가 볼 수 없으므로 클라이언트 전체 지연에서 서버 처리 시간을
빼서 얻습니다 (`tools/step0.py` 참고).

    STAGE_ROUTING       라우팅·입력 검증
    STAGE_RELEASE       current release 조회 쿼리 (D4 — 캐시 hit에도 남는 비용)
    STAGE_DB            본문 DB 쿼리 다회
    STAGE_ASSEMBLE      트리 조립 + i18n 치환
    STAGE_SERIALIZE     직렬화 (D1 — 캐시 hit에도 남으므로 이득 상한에서 제외)

**타이머 자체가 비용입니다.** 그래서 두 가지를 지킵니다.

  1. `stage_timing_enabled=False`면 `NullStageTimer`가 들어가 `mark`가 즉시 반환합니다.
     타이머 없는 회차와 비교해 타이머 비용을 보정합니다.
  2. 구간 값과 전체 서버 처리 시간을 **같은 요청 안에서 쌍으로** 기록합니다.
     별도 회차 간 p95 차이로 타이머 비용을 귀속하지 않습니다.
"""

import time
from dataclasses import dataclass, field

STAGE_ROUTING = "routing"
STAGE_RELEASE = "release_lookup"
STAGE_DB = "db_query"
STAGE_ASSEMBLE = "assemble_i18n"
STAGE_SERIALIZE = "serialize"

# 보고 순서를 고정합니다. 요청 처리 순서와 같습니다.
STAGE_ORDER: tuple[str, ...] = (
    STAGE_ROUTING,
    STAGE_RELEASE,
    STAGE_DB,
    STAGE_ASSEMBLE,
    STAGE_SERIALIZE,
)

# 캐시 hit에도 남는 구간입니다. 캐시 이득 상한 계산에서 제외합니다 (D1·D4).
STAGES_SURVIVING_CACHE_HIT: frozenset[str] = frozenset(
    {STAGE_ROUTING, STAGE_RELEASE, STAGE_SERIALIZE}
)


class StageTimer:
    """한 요청의 구간 시간을 모읍니다. 요청마다 새로 만듭니다."""

    __slots__ = ("started_ns", "stages", "_mark_ns")

    def __init__(self) -> None:
        now = time.perf_counter_ns()
        self.started_ns = now
        self._mark_ns = now
        self.stages: dict[str, int] = {}

    def mark(self, stage: str) -> None:
        """직전 mark 이후 경과를 `stage`에 더합니다."""
        now = time.perf_counter_ns()
        self.stages[stage] = self.stages.get(stage, 0) + (now - self._mark_ns)
        self._mark_ns = now

    def skip(self) -> None:
        """구간에 넣지 않을 시간을 버립니다 (측정 경계 재설정)."""
        self._mark_ns = time.perf_counter_ns()

    def total_ns(self) -> int:
        """타이머 생성부터 지금까지의 서버 내부 처리 시간입니다."""
        return time.perf_counter_ns() - self.started_ns

    def enabled(self) -> bool:
        return True

    def report(self) -> dict[str, int]:
        """구간별 ns입니다. 측정되지 않은 구간은 생략합니다."""
        return {
            stage: self.stages[stage] for stage in STAGE_ORDER if stage in self.stages
        }


class NullStageTimer(StageTimer):
    """타이머 off 회차용입니다. `mark`가 아무 일도 하지 않습니다.

    타이머 비용을 뺀 기준선을 얻기 위해 씁니다. 전체 처리 시간은 여전히 잽니다.
    """

    __slots__ = ()

    def mark(self, stage: str) -> None:
        return

    def skip(self) -> None:
        return

    def enabled(self) -> bool:
        return False

    # report()는 재정의하지 않습니다. `mark`가 아무것도 기록하지 않으므로 상위
    # 구현이 빈 dict를 돌려줍니다. 여기서 `{}`를 강제로 반환하면 `mark`가 실제로
    # 기록을 남기는 회귀가 가려져 타이머 보정 기준이 조용히 무너집니다.


def create_stage_timer(*, enabled: bool) -> StageTimer:
    """설정에 따라 실제 타이머 또는 no-op 타이머를 만듭니다."""
    return StageTimer() if enabled else NullStageTimer()


@dataclass(slots=True)
class StageSample:
    """한 요청의 측정 결과입니다. 구간과 전체를 쌍으로 보관합니다."""

    server_total_ns: int
    timing_enabled: bool
    cache_hit: bool
    stages: dict[str, int] = field(default_factory=dict)

    def accounted_ns(self) -> int:
        """구간 합계입니다. 전체와의 차이가 미계측 구간입니다."""
        return sum(self.stages.values())

    def unaccounted_ns(self) -> int:
        """전체에서 구간 합계를 뺀 값입니다. 음수면 측정 오류입니다."""
        return self.server_total_ns - self.accounted_ns()
