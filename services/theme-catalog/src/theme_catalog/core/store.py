"""메모리 authoritative 원본 (LAUGH-KNOWLEDGE-READ-001).

시작 때 업무 `Theme`들로 한 번 구성하고 불변 값만 반환합니다. 입력이 어디서 왔는지
(JSON fixture인지 다른 경로인지)는 알지 못하며 조립은 호출자가 담당합니다.
첫 버전의 갱신은 프로세스 수명까지만 유지됩니다. 영속 DB·공유 원본은 범위 밖입니다.

**상태 소유권**: 값 교체와 변경 카운터 증가는 `apply_update` 하나가 함께 수행합니다.
호출자가 원본 dict를 직접 바꿔 카운터·무효화를 우회할 수 있는 경로를 공개하지 않습니다.
관측은 `read_only_view`(live)·`copy_of_themes`(detached)·`theme_ids`·`load_count`로
제공하며, 어느 것으로도 원본을 바꿀 수 없습니다.

lock은 소유하지 않습니다. 직렬화는 호출자인 `ThemeCatalog`의 공통 보호 구간이
담당하며 cache-off/on 두 경로에 같은 구간을 적용합니다.
"""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from types import MappingProxyType

from theme_catalog.core.theme import Theme, ensure_hex_color, ensure_spacing_px


class ThemeNotFoundError(KeyError):
    """미등록 theme_id입니다. 명시적 not-found이며 negative caching하지 않습니다."""

    def __init__(self, theme_id: str) -> None:
        super().__init__(theme_id)
        self.theme_id = theme_id


class EmptyUpdateError(ValueError):
    """바꿀 속성이 하나도 없는 갱신 요청입니다."""


class ThemeStore:
    """authoritative dict입니다. 변경은 메서드로만, 관측은 읽기 전용으로 제공합니다."""

    __slots__ = ("_themes", "_change_counters", "load_count", "update_count")

    def __init__(self, themes: Iterable[Theme]) -> None:
        """업무 Theme들로 원본을 구성합니다. fixture·JSON 형식을 알지 못합니다."""
        self._themes: dict[str, Theme] = {theme.theme_id: theme for theme in themes}
        if not self._themes:
            raise ValueError("store requires at least one theme")
        # key별 변경 카운터입니다. 늦은 fill을 거절하는 기준이며 apply_update만 올립니다.
        self._change_counters: dict[str, int] = dict.fromkeys(self._themes, 0)
        self.load_count = 0
        self.update_count = 0

    def read(self, theme_id: str) -> tuple[Theme, int]:
        """단건 조회입니다. 값과 그 시점의 변경 카운터를 함께 돌려줍니다."""
        theme = self._themes.get(theme_id)
        if theme is None:
            raise ThemeNotFoundError(theme_id)
        self.load_count += 1
        return theme, self._change_counters[theme_id]

    def apply_update(
        self,
        theme_id: str,
        *,
        background: str | None = None,
        foreground: str | None = None,
        spacing_px: int | None = None,
    ) -> Theme:
        """값 교체와 변경 카운터 증가를 한 번에 수행합니다.

        둘을 따로 호출할 수 없게 묶어 두어야 늦은 fill 방어가 유효합니다.
        """
        current = self._themes.get(theme_id)
        if current is None:
            raise ThemeNotFoundError(theme_id)

        where = f"update[{theme_id}]"
        changes: dict[str, str | int] = {}
        if background is not None:
            changes["background"] = ensure_hex_color(background, where=where)
        if foreground is not None:
            changes["foreground"] = ensure_hex_color(foreground, where=where)
        if spacing_px is not None:
            changes["spacing_px"] = ensure_spacing_px(spacing_px, where=where)
        if not changes:
            raise EmptyUpdateError("update must change at least one attribute")

        updated = replace(current, **changes)
        self._themes[theme_id] = updated
        self._change_counters[theme_id] += 1
        self.update_count += 1
        return updated

    def change_counter(self, theme_id: str) -> int:
        """fill 직전 대조용 현재 카운터입니다."""
        counter = self._change_counters.get(theme_id)
        if counter is None:
            raise ThemeNotFoundError(theme_id)
        return counter

    def theme_ids(self) -> tuple[str, ...]:
        """등록된 key 목록입니다. 측정 도구와 시험이 사용합니다."""
        return tuple(self._themes)

    def read_only_view(self) -> Mapping[str, Theme]:
        """원본의 **live** 읽기 전용 보기입니다.

        이 보기로 값을 바꿀 수는 없지만 이후 갱신은 그대로 비칩니다.
        특정 시점의 값을 고정해 두려면 `copy_of_themes`를 쓰십시오.
        """
        return MappingProxyType(self._themes)

    def copy_of_themes(self) -> dict[str, Theme]:
        """호출 시점 값을 고정한 detached 사본입니다.

        관측·시험 전용입니다. 요청 hot path에서는 호출하지 않습니다.
        """
        return dict(self._themes)

    def __len__(self) -> int:
        return len(self._themes)
