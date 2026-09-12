"""비교용 bounded 로컬 캐시 (LAUGH-KNOWLEDGE-READ-001).

단일 프로세스 LRU입니다. Redis·공유 캐시·분산 lock은 범위 밖입니다.
실패·취소·not-found는 저장하지 않습니다.
"""

from collections import OrderedDict

from theme_catalog.core.theme import Theme

DEFAULT_CACHE_CAPACITY = 16


class BoundedLruCache:
    """용량 상한이 있는 LRU입니다. 초과 시 가장 오래 쓰이지 않은 key를 내보냅니다."""

    __slots__ = ("capacity", "entries", "eviction_count")

    def __init__(self, capacity: int = DEFAULT_CACHE_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("cache capacity must be positive")
        self.capacity = capacity
        self.entries: OrderedDict[str, Theme] = OrderedDict()
        self.eviction_count = 0

    def get(self, theme_id: str) -> Theme | None:
        """hit이면 최근 사용으로 올립니다."""
        entry = self.entries.get(theme_id)
        if entry is None:
            return None
        self.entries.move_to_end(theme_id)
        return entry

    def put(self, theme_id: str, theme: Theme) -> str | None:
        """항목을 넣고 용량 초과 시 evict한 key를 돌려줍니다."""
        self.entries[theme_id] = theme
        self.entries.move_to_end(theme_id)
        if len(self.entries) <= self.capacity:
            return None
        evicted_id, _ = self.entries.popitem(last=False)
        self.eviction_count += 1
        return evicted_id

    def invalidate(self, theme_id: str) -> bool:
        """갱신 시 해당 key만 제거합니다."""
        return self.entries.pop(theme_id, None) is not None

    def clear(self) -> None:
        """이 인스턴스가 소유한 항목만 비웁니다."""
        self.entries.clear()

    def __len__(self) -> int:
        return len(self.entries)
