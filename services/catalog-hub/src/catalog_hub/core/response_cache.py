"""조립된 응답 캐시 (D1·D3·D4).

Step 0에서는 **꺼져 있습니다.** 기준선은 캐시 없음이며, 이 모듈은 Step 2에서
켜기 위해 자리만 잡아 둡니다.

설계 근거:
  - 키에 `release_id`가 들어갑니다. release는 불변이고 동시에 published 하나이므로
    release가 바뀌면 키 전체가 자연히 무효가 됩니다 — TTL·부분 무효화가 불필요합니다.
  - 용량 관리(LRU·eviction)를 두지 않았습니다. 단 이것은 **잠정 가설**입니다(D3).
    27MB는 DB 크기이지 응답 캐시 크기가 아니므로, Step 2에서 엔트리당 메모리·배율·
    RSS 증가분을 실측한 뒤 전량 상주를 확정하거나 철회합니다.
  - 키 조합 상한은 `release_id × endpoint × lang`입니다. 현재 endpoint 1종·lang 7개.
"""

from catalog_hub.core.assembled import AssembledCatalog, CacheKey


class ResponseCache:
    """release 키잉 응답 캐시입니다. 용량 상한이 없습니다 (D3 잠정 가설).

    단일 워커 전제이므로 lock을 두지 않습니다(D2). 여러 요청이 같은 키를 동시에
    miss하면 각자 조립한 뒤 같은 값을 덮어씁니다. release 키잉 덕분에 어느 쪽이
    이기든 값이 같으므로 오염이 없습니다.
    """

    __slots__ = ("entries", "hits", "misses", "stores")

    def __init__(self) -> None:
        self.entries: dict[CacheKey, AssembledCatalog] = {}
        self.hits = 0
        self.misses = 0
        self.stores = 0

    def get(self, key: CacheKey) -> AssembledCatalog | None:
        entry = self.entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, key: CacheKey, value: AssembledCatalog) -> None:
        self.entries[key] = value
        self.stores += 1

    def release_ids(self) -> tuple[int, ...]:
        """보관 중인 release들입니다. 전환 후 옛 엔트리 잔류를 관측할 때 씁니다."""
        return tuple(sorted({key.release_id for key in self.entries}))

    def drop_other_releases(self, current_release_id: int) -> int:
        """옛 release 엔트리를 버립니다.

        정합성에는 필요 없습니다 — 키가 달라 조회되지 않기 때문입니다.
        순전히 **메모리 회수**를 위한 것이며, 하지 않으면 전환마다 메모리가 누적됩니다.
        Step 2에서 이 회수 유무의 메모리 차이를 관측합니다.
        """
        stale = [key for key in self.entries if key.release_id != current_release_id]
        for key in stale:
            del self.entries[key]
        return len(stale)

    def stats(self) -> dict[str, object]:
        return {
            "entries": len(self.entries),
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "release_ids": list(self.release_ids()),
        }

    def __len__(self) -> int:
        return len(self.entries)
