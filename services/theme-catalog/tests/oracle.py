"""독립 기대값 oracle (LAUGH-KNOWLEDGE-READ-001).

서비스 구현의 store/cache/reader/key 함수를 재사용하지 않는 별도 reference model입니다.
JSON을 직접 읽고 tuple로만 보관하며 캐시 개념이 없습니다.
원본 dict와 캐시가 함께 틀려도 이쪽과 어긋나 실패해야 합니다.
"""

import json
from pathlib import Path

SERVICE_FIXTURE_PATH = (
    Path(__file__).parent.parent / "src" / "theme_catalog" / "fixtures" / "themes.json"
)


class ReferenceThemes:
    """fixture JSON을 독립적으로 읽어 (bg, fg, spacing) tuple을 소유하는 기대값 표입니다."""

    def __init__(self, path: Path = SERVICE_FIXTURE_PATH) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.rows: dict[str, tuple[str, str, int]] = {}
        self.order: list[str] = []
        for entry in raw["themes"]:
            theme_id = entry["theme_id"]
            self.rows[theme_id] = (
                entry["background"],
                entry["foreground"],
                entry["spacing_px"],
            )
            self.order.append(theme_id)
        self.updates = [
            (
                item["after_completed_reads"],
                item["theme_id"],
                item.get("background"),
                item.get("foreground"),
                item.get("spacing_px"),
            )
            for item in raw.get("updates", [])
        ]

    def expect(self, theme_id: str) -> tuple[str, str, int]:
        if theme_id not in self.rows:
            raise AssertionError(f"reference has no theme {theme_id}")
        return self.rows[theme_id]

    def apply(
        self,
        theme_id: str,
        *,
        background: str | None = None,
        foreground: str | None = None,
        spacing_px: int | None = None,
    ) -> None:
        """독립 기대값 쪽에도 같은 갱신을 반영합니다."""
        current = self.expect(theme_id)
        self.rows[theme_id] = (
            current[0] if background is None else background,
            current[1] if foreground is None else foreground,
            current[2] if spacing_px is None else spacing_px,
        )


def as_tuple(theme) -> tuple[str, str, int]:
    """구현 결과를 oracle 비교 형태로 바꿉니다. 구현 헬퍼를 쓰지 않습니다."""
    return (theme.background, theme.foreground, theme.spacing_px)


def payload_tuple(data: dict) -> tuple[str, str, int]:
    """HTTP 응답 본문을 oracle 비교 형태로 바꿉니다."""
    return (data["background"], data["foreground"], data["spacing_px"])
