"""외부 테마 스키마 (LAUGH-KNOWLEDGE-READ-001).

내부 불변 `Theme`을 API가 명시적으로 변환합니다. 업무 타입을 그대로 노출하지 않습니다.

**입력 계약은 원본(store)과 같아야 합니다.** pydantic 기본 모드는 `True`를 1로,
`"12"`를 12로 바꿀 수 있지만 원본은 이를 거절합니다. 두 계약이 갈라지면 HTTP로는
통과한 값이 원본 규칙을 우회하므로 `strict=True`로 정수 계약을 맞춥니다.
"""

from pydantic import BaseModel, ConfigDict, Field

from theme_catalog.core.theme import (
    MAX_SPACING_PX,
    MAX_THEME_ID_LENGTH,
    MIN_SPACING_PX,
    Theme,
)

HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"


class ThemeData(BaseModel):
    """조회 응답 본문입니다."""

    model_config = ConfigDict(frozen=True)

    theme_id: str = Field(min_length=1, max_length=MAX_THEME_ID_LENGTH)
    background: str = Field(pattern=HEX_COLOR_PATTERN)
    foreground: str = Field(pattern=HEX_COLOR_PATTERN)
    spacing_px: int = Field(ge=MIN_SPACING_PX, le=MAX_SPACING_PX)


class ThemePatch(BaseModel):
    """갱신 입력입니다. 지정한 속성만 교체합니다.

    `strict=True`이므로 bool·문자열·실수를 정수로 자동 변환하지 않습니다.
    원본의 `ensure_spacing_px`와 같은 규칙입니다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    background: str | None = Field(default=None, pattern=HEX_COLOR_PATTERN)
    foreground: str | None = Field(default=None, pattern=HEX_COLOR_PATTERN)
    spacing_px: int | None = Field(default=None, ge=MIN_SPACING_PX, le=MAX_SPACING_PX)

    def is_empty(self) -> bool:
        """바꿀 속성이 하나도 없으면 참입니다."""
        return (
            self.background is None
            and self.foreground is None
            and self.spacing_px is None
        )


def to_theme_data(theme: Theme) -> ThemeData:
    """내부 업무 값을 외부 응답으로 변환합니다."""
    return ThemeData(
        theme_id=theme.theme_id,
        background=theme.background,
        foreground=theme.foreground,
        spacing_px=theme.spacing_px,
    )
