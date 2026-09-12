"""합성 fixture 적재 (LAUGH-KNOWLEDGE-READ-001).

JSON 파일을 읽어 업무 타입으로 바꾸는 **적재 전용** 모듈입니다.
값 규칙은 `core/theme.py`가 소유하며 여기서는 문서 구조만 검증합니다.
store·cache·reader·schema는 이 모듈을 import하지 않습니다.

fixture 값은 전부 임의 생성이며 회사 원자료와 무관합니다.
구조 오류는 앱 시작·측정 시작 전에 거절합니다.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from theme_catalog.core.theme import (
    InvalidThemeValueError,
    Theme,
    ensure_hex_color,
    ensure_spacing_px,
    ensure_theme_id,
)

DEFAULT_FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "themes.json"
SUPPORTED_FIXTURE_VERSION = 1


class FixtureError(ValueError):
    """fixture 문서 구조 오류입니다."""


@dataclass(frozen=True, slots=True)
class ThemeUpdate:
    """fixture가 소유한 합성 갱신 입력입니다. 기존 ID의 속성만 교체합니다."""

    after_completed_reads: int
    theme_id: str
    background: str | None = None
    foreground: str | None = None
    spacing_px: int | None = None


@dataclass(frozen=True, slots=True)
class Fixture:
    """검증을 통과한 합성 입력입니다."""

    fixture_version: int
    seed: int
    themes: tuple[Theme, ...]
    updates: tuple[ThemeUpdate, ...]


def read_theme(raw: object, *, where: str) -> Theme:
    """항목 하나를 업무 Theme으로 바꿉니다. 값 규칙은 theme 모듈에 위임합니다."""
    if not isinstance(raw, dict):
        raise FixtureError(f"{where}: must be an object")
    return Theme(
        theme_id=ensure_theme_id(raw.get("theme_id"), where=where),
        background=ensure_hex_color(raw.get("background"), where=where),
        foreground=ensure_hex_color(raw.get("foreground"), where=where),
        spacing_px=ensure_spacing_px(raw.get("spacing_px"), where=where),
    )


def read_update(raw: object, *, where: str, known_ids: frozenset[str]) -> ThemeUpdate:
    """갱신 입력 하나를 검증합니다. 기존 ID의 속성만 바꿀 수 있습니다."""
    if not isinstance(raw, dict):
        raise FixtureError(f"{where}: must be an object")
    theme_id = ensure_theme_id(raw.get("theme_id"), where=where)
    if theme_id not in known_ids:
        raise FixtureError(f"{where}: theme_id must reference an existing theme")

    after = raw.get("after_completed_reads")
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise FixtureError(f"{where}: after_completed_reads must be a non-negative int")

    background = raw.get("background")
    foreground = raw.get("foreground")
    spacing = raw.get("spacing_px")
    if background is None and foreground is None and spacing is None:
        raise FixtureError(f"{where}: update must change at least one attribute")

    return ThemeUpdate(
        after_completed_reads=after,
        theme_id=theme_id,
        background=(
            None if background is None else ensure_hex_color(background, where=where)
        ),
        foreground=(
            None if foreground is None else ensure_hex_color(foreground, where=where)
        ),
        spacing_px=(
            None if spacing is None else ensure_spacing_px(spacing, where=where)
        ),
    )


def parse_fixture(document: object) -> Fixture:
    """합성 fixture 문서를 검증해 불변 구조로 바꿉니다.

    값 규칙 위반(`InvalidThemeValueError`)도 fixture 적재 실패이므로 `FixtureError`로
    모아 전달합니다. 호출자는 한 가지 예외만 다루면 됩니다.
    """
    try:
        return build_fixture(document)
    except InvalidThemeValueError as error:
        raise FixtureError(str(error)) from error


def build_fixture(document: object) -> Fixture:
    """구조 검증 본체입니다. 값 규칙 위반은 그대로 전파합니다."""
    if not isinstance(document, dict):
        raise FixtureError("fixture root must be an object")

    version = document.get("fixture_version")
    if version != SUPPORTED_FIXTURE_VERSION:
        raise FixtureError(f"fixture_version must be {SUPPORTED_FIXTURE_VERSION}")
    seed = document.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise FixtureError("seed must be an int")

    raw_themes = document.get("themes")
    if not isinstance(raw_themes, list) or not raw_themes:
        raise FixtureError("themes must be a non-empty array")

    themes: list[Theme] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_themes):
        theme = read_theme(raw, where=f"themes[{index}]")
        if theme.theme_id in seen:
            raise FixtureError(f"themes[{index}]: duplicate theme_id {theme.theme_id}")
        seen.add(theme.theme_id)
        themes.append(theme)

    raw_updates = document.get("updates", [])
    if not isinstance(raw_updates, list):
        raise FixtureError("updates must be an array")
    known_ids = frozenset(seen)
    updates = [
        read_update(raw, where=f"updates[{index}]", known_ids=known_ids)
        for index, raw in enumerate(raw_updates)
    ]

    return Fixture(
        fixture_version=version,
        seed=seed,
        themes=tuple(themes),
        updates=tuple(sorted(updates, key=lambda item: item.after_completed_reads)),
    )


def load_fixture(path: Path = DEFAULT_FIXTURE_PATH) -> Fixture:
    """fixture 파일을 한 번 읽어 검증합니다. 요청마다 재파싱하지 않습니다."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise FixtureError(f"cannot read fixture at {path}: {error}") from error
    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise FixtureError(f"invalid JSON in {path}: {error}") from error
    return parse_fixture(document)
