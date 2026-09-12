"""테마 업무 타입과 값 규칙 (LAUGH-KNOWLEDGE-READ-001).

업무 값과 그 허용 범위만 소유합니다. JSON·파일·HTTP를 알지 못하므로
store·cache·reader·schema가 fixture 적재 구현에 의존하지 않습니다.

`spacing_px`는 정수 계약입니다. bool은 파이썬에서 int의 하위 타입이지만
간격 값으로 허용하지 않으며, 문자열·실수도 자동 변환하지 않습니다.
이 규칙은 원본(store)과 HTTP 입력 양쪽에서 같아야 합니다.
"""

from dataclasses import dataclass

HEX_DIGITS = frozenset("0123456789ABCDEFabcdef")
HEX_COLOR_LENGTH = 7
MIN_SPACING_PX = 0
MAX_SPACING_PX = 64
MAX_THEME_ID_LENGTH = 64


class InvalidThemeValueError(ValueError):
    """허용하지 않는 테마 값입니다. 원본과 HTTP 입력에 같은 규칙으로 적용합니다."""


@dataclass(frozen=True, slots=True)
class Theme:
    """불변 테마 속성입니다. 같은 객체 반환 계약의 단위입니다."""

    theme_id: str
    background: str
    foreground: str
    spacing_px: int


def ensure_theme_id(raw: object, *, where: str) -> str:
    """비어 있지 않은 짧은 문자열만 허용해 입력 크기를 한정합니다."""
    if not isinstance(raw, str) or not raw:
        raise InvalidThemeValueError(f"{where}: theme_id must be a non-empty string")
    if len(raw) > MAX_THEME_ID_LENGTH:
        raise InvalidThemeValueError(
            f"{where}: theme_id exceeds {MAX_THEME_ID_LENGTH} characters"
        )
    return raw


def ensure_hex_color(raw: object, *, where: str) -> str:
    """'#' + 6자리 hex만 허용합니다."""
    if not isinstance(raw, str):
        raise InvalidThemeValueError(f"{where}: color must be a string")
    if (
        len(raw) != HEX_COLOR_LENGTH
        or raw[0] != "#"
        or any(digit not in HEX_DIGITS for digit in raw[1:])
    ):
        raise InvalidThemeValueError(
            f"{where}: color must be '#' followed by 6 hex digits"
        )
    return raw


def ensure_spacing_px(raw: object, *, where: str) -> int:
    """0-64 정수만 허용합니다. bool·문자열·실수는 변환하지 않고 거절합니다."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InvalidThemeValueError(f"{where}: spacing_px must be an int")
    if not MIN_SPACING_PX <= raw <= MAX_SPACING_PX:
        raise InvalidThemeValueError(
            f"{where}: spacing_px must be within {MIN_SPACING_PX}-{MAX_SPACING_PX}"
        )
    return raw
