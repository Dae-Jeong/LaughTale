"""테마 조회·갱신 업무 (LAUGH-KNOWLEDGE-READ-001).

`ThemeCatalog`가 조회와 갱신을 함께 소유합니다. 두 가지가 같은 원본·같은 lock·같은
무효화 규칙을 공유하므로, 나누면 그 순서 계약이 두 곳으로 흩어집니다.

선형화 지점은 **유효 cache hit 또는 원본 snapshot 취득 시점**입니다.

- 갱신 성공 뒤 시작된 조회는 새 값을 반환합니다.
- 갱신과 겹친 조회는 그 지점의 순서에 맞는 옛 값 또는 새 값을 허용합니다.
- 응답 직전까지 항상 최신이라는 더 강한 보장은 주장하지 않습니다.

이 계약은 단일 프로세스 범위입니다. 여러 worker·Pod에서는 로컬 lock과 캐시가
공유되지 않으므로 다중 서버 최신성을 보장하지 않습니다.
"""

import asyncio
from collections.abc import Iterable, Mapping

from theme_catalog.core.cache import DEFAULT_CACHE_CAPACITY, BoundedLruCache
from theme_catalog.core.control_hooks import NO_HOOKS, ControlHooks
from theme_catalog.core.counters import ReadCounters
from theme_catalog.core.store import ThemeNotFoundError, ThemeStore
from theme_catalog.core.theme import Theme


class ThemeCatalog:
    """조회·갱신 진입점입니다. cache-off/on 경로를 선택합니다.

    두 경로 모두 같은 공통 lock 아래서 원본을 만지므로 캐시에 유리한 비대칭을
    만들지 않습니다. 정상 조회 경로에는 불필요한 파싱·전수 스캔·인위적 지연이 없습니다.

    `hooks`는 시험 전용이며 기본값은 빈 hook입니다 (`core/control_hooks.py` 참고).
    """

    __slots__ = ("store", "cache", "counters", "hooks", "lock")

    def __init__(
        self,
        store: ThemeStore,
        *,
        cache: BoundedLruCache | None = None,
        hooks: ControlHooks = NO_HOOKS,
    ) -> None:
        self.store = store
        self.cache = cache
        self.counters = ReadCounters()
        self.hooks = hooks
        self.lock = asyncio.Lock()

    async def get(self, theme_id: str) -> Theme:
        """단건 조회입니다.

        cache-off: lock 아래 원본 1회 조회.
        cache-on: lock 아래 hit 확인 → miss면 원본 snapshot과 변경 카운터 취득 →
        (lock 밖) → lock 아래 카운터를 다시 확인해 그대로일 때만 fill.
        """
        try:
            hooks = self.hooks
            if hooks.before_snapshot is not None:
                await hooks.before_snapshot(theme_id)

            if self.cache is None:
                async with self.lock:
                    theme, _ = self.store.read(theme_id)
                if hooks.after_snapshot is not None:
                    await hooks.after_snapshot(theme_id)
                self.counters.record_key_read(theme_id)
                return theme

            async with self.lock:
                self.counters.cache_lookups += 1
                cached = self.cache.get(theme_id)
                if cached is not None:
                    self.counters.hits += 1
                    self.counters.record_key_read(theme_id)
                    return cached
                self.counters.misses += 1
                theme, counter_at_snapshot = self.store.read(theme_id)
                self.counters.origin_loads += 1

            if hooks.after_snapshot is not None:
                await hooks.after_snapshot(theme_id)
            if hooks.before_fill is not None:
                await hooks.before_fill(theme_id)

            async with self.lock:
                # 늦은 fill 방어: snapshot 이후 같은 key가 바뀌었으면 채우지 않습니다.
                # 새 값을 옛 snapshot으로 덮지 않기 위해 카운터를 lock 안에서 다시 읽습니다.
                if self.store.change_counter(theme_id) == counter_at_snapshot:
                    evicted = self.cache.put(theme_id, theme)
                    self.counters.fills += 1
                    if evicted is not None:
                        self.counters.evictions += 1
                else:
                    self.counters.rejected_fills += 1

            if hooks.after_fill is not None:
                await hooks.after_fill(theme_id)

            self.counters.record_key_read(theme_id)
            return theme
        except ThemeNotFoundError:
            # not-found는 캐시에 남기지 않습니다.
            self.counters.not_found += 1
            raise
        except asyncio.CancelledError:
            self.counters.cancellations += 1
            raise
        except Exception:
            self.counters.errors += 1
            raise

    async def update(
        self,
        theme_id: str,
        *,
        background: str | None = None,
        foreground: str | None = None,
        spacing_px: int | None = None,
    ) -> Theme:
        """드문 갱신입니다.

        한 lock 구간 안에서 원본 값 교체·변경 카운터 증가(`apply_update`)와 해당 key
        무효화를 함께 수행합니다. 둘 사이에 다른 조회가 끼어들어 옛 값을 채울 수
        없어야 하므로 이 순서를 helper로 감추지 않습니다.
        """
        try:
            async with self.lock:
                updated = self.store.apply_update(
                    theme_id,
                    background=background,
                    foreground=foreground,
                    spacing_px=spacing_px,
                )
                if self.cache is not None and self.cache.invalidate(theme_id):
                    self.counters.invalidations += 1
                self.counters.updates += 1
                return updated
        except ThemeNotFoundError:
            self.counters.not_found += 1
            raise
        except asyncio.CancelledError:
            self.counters.cancellations += 1
            raise
        except Exception:
            self.counters.errors += 1
            raise

    # --- 관측 ---------------------------------------------------------------
    # 아래는 업무 흐름이 아니라 상태 보고용입니다.

    @property
    def cache_enabled(self) -> bool:
        return self.cache is not None

    def origin_view(self) -> Mapping[str, Theme]:
        """원본의 **live** 읽기 전용 보기입니다.

        이 보기로 원본을 바꿀 수 없지만 이후 갱신은 그대로 비칩니다.
        """
        return self.store.read_only_view()

    def copy_origin(self) -> dict[str, Theme]:
        """호출 시점 값을 고정한 detached 사본입니다. 요청 hot path에서 쓰지 않습니다."""
        return self.store.copy_of_themes()

    def reset_counters(self) -> None:
        """warmup 계수를 측정 회차와 섞지 않기 위해 씁니다."""
        self.counters = ReadCounters()


def build_catalog(
    themes: Iterable[Theme],
    *,
    cache_enabled: bool,
    capacity: int = DEFAULT_CACHE_CAPACITY,
    hooks: ControlHooks = NO_HOOKS,
) -> ThemeCatalog:
    """업무 Theme들로 원본과 선택적 캐시를 갖춘 catalog를 만듭니다.

    fixture 적재는 호출자(조립 경계)가 담당하므로 이 모듈은 JSON을 알지 못합니다.
    """
    cache = BoundedLruCache(capacity) if cache_enabled else None
    return ThemeCatalog(ThemeStore(themes), cache=cache, hooks=hooks)
