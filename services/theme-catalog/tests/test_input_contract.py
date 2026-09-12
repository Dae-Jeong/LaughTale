"""입력 계약 대조 시험 (LAUGH-KNOWLEDGE-READ-001).

원본(store)과 HTTP schema가 **같은 정수/색상 규칙**을 적용하는지 실제 ASGI로 확인합니다.

배경: 원본은 `spacing_px`에 bool·문자열·실수를 거절하지만 pydantic 기본 모드는
`True`를 1로, `"12"`를 12로 바꿀 수 있습니다. 두 계약이 갈라지면 HTTP로 통과한 값이
원본 규칙을 우회하므로 `ThemePatch`는 strict 모드로 계약을 맞춥니다.
"""

import unittest
from http import HTTPStatus

from fastapi import FastAPI
from fastapi.testclient import TestClient
from oracle import ReferenceThemes, payload_tuple

from theme_catalog.bootstrap.app import create_app
from theme_catalog.core.fixture import DEFAULT_FIXTURE_PATH, load_fixture
from theme_catalog.core.settings import Settings
from theme_catalog.core.store import ThemeStore
from theme_catalog.core.theme import InvalidThemeValueError
from theme_catalog.schemas.themes import ThemePatch

KNOWN_THEME_ID = "theme-0001"

# 계약 위반 값을 일부러 넣는 시험이므로 `object`로 선언합니다.
# 타입 검사를 통과시키려 값을 바꾸면 시험의 의미가 사라집니다.
NON_INTEGER_SPACING: tuple[tuple[str, object], ...] = (
    ("bool true", True),
    ("bool false", False),
    ("string digits", "12"),
    ("float whole", 12.0),
    ("float fraction", 12.5),
    ("null-like string", "null"),
)

NON_COLOR_VALUES: tuple[tuple[str, object], ...] = (
    ("not a color", "not-a-color"),
    ("missing hash", "AABBCC"),
    ("too short", "#12345"),
    ("non hex", "#GGGGGG"),
    ("integer", 16),
    ("bool", True),
)


def build_app(*, cache_enabled: bool = False) -> FastAPI:
    """시험 소유 앱입니다. 기존 서비스 설정·환경·.env를 읽지 않습니다."""
    return create_app(Settings(_env_file=None, cache_enabled=cache_enabled))


class StoreSpacingContractTests(unittest.TestCase):
    """원본이 소유한 정수 규칙입니다. 이 시험이 기준선입니다."""

    def setUp(self) -> None:
        self.store = ThemeStore(load_fixture(DEFAULT_FIXTURE_PATH).themes)

    def test_store_rejects_non_integer_spacing(self) -> None:
        # 타입 검사가 이미 막는 값을 런타임에도 거절하는지 확인하는 시험이므로
        # 선언 타입을 벗어난 인자를 의도적으로 전달합니다.
        for name, value in NON_INTEGER_SPACING:
            with self.subTest(value=name):
                with self.assertRaises(InvalidThemeValueError):
                    self.store.apply_update(KNOWN_THEME_ID, spacing_px=value)  # ty: ignore[invalid-argument-type]

    def test_store_accepts_plain_integer(self) -> None:
        updated = self.store.apply_update(KNOWN_THEME_ID, spacing_px=12)
        self.assertEqual(updated.spacing_px, 12)

    def test_store_rejects_bad_colors(self) -> None:
        for name, value in NON_COLOR_VALUES:
            with self.subTest(value=name):
                with self.assertRaises(InvalidThemeValueError):
                    self.store.apply_update(KNOWN_THEME_ID, background=value)  # ty: ignore[invalid-argument-type]


class PatchSchemaContractTests(unittest.TestCase):
    """HTTP 입력 스키마가 원본과 같은 규칙을 적용하는지 직접 확인합니다."""

    def test_schema_rejects_non_integer_spacing(self) -> None:
        for name, value in NON_INTEGER_SPACING:
            with self.subTest(value=name):
                with self.assertRaises(ValueError):
                    ThemePatch(spacing_px=value)  # ty: ignore[invalid-argument-type]

    def test_schema_accepts_plain_integer(self) -> None:
        self.assertEqual(ThemePatch(spacing_px=12).spacing_px, 12)

    def test_schema_rejects_bad_colors(self) -> None:
        for name, value in NON_COLOR_VALUES:
            with self.subTest(value=name):
                with self.assertRaises(ValueError):
                    ThemePatch(background=value)  # ty: ignore[invalid-argument-type]


class HttpSpacingContractTests(unittest.TestCase):
    """실제 ASGI 대조입니다. 원본이 거절하는 값은 HTTP에서도 422여야 합니다."""

    def setUp(self) -> None:
        self.reference = ReferenceThemes()

    def test_http_rejects_every_non_integer_spacing(self) -> None:
        with TestClient(build_app()) as client:
            for name, value in NON_INTEGER_SPACING:
                with self.subTest(value=name):
                    response = client.patch(
                        f"/v1/themes/{KNOWN_THEME_ID}", json={"spacing_px": value}
                    )
                    self.assertEqual(
                        response.status_code, HTTPStatus.UNPROCESSABLE_ENTITY
                    )
                    self.assertEqual(response.json()["code"], "INVALID_INPUT")

    def test_rejected_input_does_not_change_origin(self) -> None:
        """거절된 입력이 원본을 바꾸지 않아야 합니다."""
        with TestClient(build_app()) as client:
            for _, value in NON_INTEGER_SPACING:
                client.patch(f"/v1/themes/{KNOWN_THEME_ID}", json={"spacing_px": value})
            body = client.get(f"/v1/themes/{KNOWN_THEME_ID}").json()["data"]
            self.assertEqual(payload_tuple(body), self.reference.expect(KNOWN_THEME_ID))

    def test_http_accepts_plain_integer(self) -> None:
        with TestClient(build_app()) as client:
            response = client.patch(
                f"/v1/themes/{KNOWN_THEME_ID}", json={"spacing_px": 12}
            )
            self.assertEqual(response.status_code, HTTPStatus.OK)
            self.assertEqual(response.json()["data"]["spacing_px"], 12)

    def test_http_rejects_bad_colors(self) -> None:
        with TestClient(build_app()) as client:
            for name, value in NON_COLOR_VALUES:
                with self.subTest(value=name):
                    response = client.patch(
                        f"/v1/themes/{KNOWN_THEME_ID}", json={"background": value}
                    )
                    self.assertEqual(
                        response.status_code, HTTPStatus.UNPROCESSABLE_ENTITY
                    )

    def test_empty_and_null_patches_report_invalid_input(self) -> None:
        """빈 PATCH·null도 입력 오류 코드로 답합니다 (이전에는 HTTP_ERROR였습니다)."""
        with TestClient(build_app()) as client:
            for body in ({}, {"spacing_px": None}):
                with self.subTest(body=body):
                    response = client.patch(f"/v1/themes/{KNOWN_THEME_ID}", json=body)
                    self.assertEqual(
                        response.status_code, HTTPStatus.UNPROCESSABLE_ENTITY
                    )
                    self.assertEqual(response.json()["code"], "INVALID_INPUT")

    def test_spacing_bounds_are_enforced_over_http(self) -> None:
        with TestClient(build_app()) as client:
            for value in (-1, 65, 200):
                with self.subTest(value=value):
                    response = client.patch(
                        f"/v1/themes/{KNOWN_THEME_ID}", json={"spacing_px": value}
                    )
                    self.assertEqual(
                        response.status_code, HTTPStatus.UNPROCESSABLE_ENTITY
                    )
            for value in (0, 64):
                with self.subTest(value=value):
                    response = client.patch(
                        f"/v1/themes/{KNOWN_THEME_ID}", json={"spacing_px": value}
                    )
                    self.assertEqual(response.status_code, HTTPStatus.OK)


if __name__ == "__main__":
    unittest.main()
