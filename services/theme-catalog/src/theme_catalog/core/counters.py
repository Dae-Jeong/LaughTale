"""조회·갱신 계수 (LAUGH-KNOWLEDGE-READ-001).

기능 검증과 측정 분모 확인용 관측값입니다. 업무 결정에 사용하지 않으며
hit 비율이 높다는 사실만으로 자원 효율 개선을 주장하지 않습니다.
"""

from dataclasses import dataclass, field


@dataclass(slots=True)
class ReadCounters:
    """성공 lookup과 오류/not-found를 분리해 셉니다."""

    cache_lookups: int = 0
    hits: int = 0
    misses: int = 0
    origin_loads: int = 0
    fills: int = 0
    rejected_fills: int = 0
    evictions: int = 0
    invalidations: int = 0
    updates: int = 0
    not_found: int = 0
    errors: int = 0
    cancellations: int = 0
    key_reads: dict[str, int] = field(default_factory=dict)

    def record_key_read(self, theme_id: str) -> None:
        """key 편중·working-set 기록입니다."""
        self.key_reads[theme_id] = self.key_reads.get(theme_id, 0) + 1

    def snapshot(self) -> dict[str, object]:
        """보고용 평면 dict입니다. 이후 변경이 반영되지 않는 사본입니다."""
        return {
            "cache_lookups": self.cache_lookups,
            "hits": self.hits,
            "misses": self.misses,
            "origin_loads": self.origin_loads,
            "fills": self.fills,
            "rejected_fills": self.rejected_fills,
            "evictions": self.evictions,
            "invalidations": self.invalidations,
            "updates": self.updates,
            "not_found": self.not_found,
            "errors": self.errors,
            "cancellations": self.cancellations,
            "key_reads": dict(self.key_reads),
        }
