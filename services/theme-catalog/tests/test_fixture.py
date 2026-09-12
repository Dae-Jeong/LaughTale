"""fixture 검증 시험 (LAUGH-KNOWLEDGE-READ-001).

구조 오류는 앱 시작·측정 시작 전에 거절합니다.
"""

import unittest

from oracle import SERVICE_FIXTURE_PATH

from theme_catalog.core.fixture import (
    DEFAULT_FIXTURE_PATH,
    FixtureError,
    load_fixture,
    parse_fixture,
)


def base_document() -> dict:
    return {
        "fixture_version": 1,
        "seed": 41,
        "themes": [
            {
                "theme_id": "theme-0001",
                "background": "#112233",
                "foreground": "#AABBCC",
                "spacing_px": 8,
            }
        ],
        "updates": [],
    }


class FixtureValidationTests(unittest.TestCase):
    def test_valid_fixture_parses(self) -> None:
        fixture = parse_fixture(base_document())
        self.assertEqual(fixture.fixture_version, 1)
        self.assertEqual(len(fixture.themes), 1)

    def test_shipped_fixture_matches_declared_shape(self) -> None:
        fixture = load_fixture(DEFAULT_FIXTURE_PATH)
        self.assertEqual(fixture.fixture_version, 1)
        self.assertEqual(len(fixture.themes), 32)
        self.assertEqual(len({theme.theme_id for theme in fixture.themes}), 32)

    def test_default_path_points_at_service_fixture(self) -> None:
        self.assertEqual(DEFAULT_FIXTURE_PATH.resolve(), SERVICE_FIXTURE_PATH.resolve())

    def test_rejects_non_object_root(self) -> None:
        with self.assertRaises(FixtureError):
            parse_fixture([])

    def test_rejects_wrong_version(self) -> None:
        document = base_document()
        document["fixture_version"] = 2
        with self.assertRaises(FixtureError):
            parse_fixture(document)

    def test_rejects_empty_themes(self) -> None:
        document = base_document()
        document["themes"] = []
        with self.assertRaises(FixtureError):
            parse_fixture(document)

    def test_rejects_bad_color(self) -> None:
        document = base_document()
        document["themes"][0]["background"] = "#12345"
        with self.assertRaises(FixtureError):
            parse_fixture(document)

    def test_rejects_non_hex_color(self) -> None:
        document = base_document()
        document["themes"][0]["foreground"] = "#GGGGGG"
        with self.assertRaises(FixtureError):
            parse_fixture(document)

    def test_rejects_out_of_range_spacing(self) -> None:
        document = base_document()
        document["themes"][0]["spacing_px"] = 65
        with self.assertRaises(FixtureError):
            parse_fixture(document)

    def test_rejects_bool_spacing(self) -> None:
        document = base_document()
        document["themes"][0]["spacing_px"] = True
        with self.assertRaises(FixtureError):
            parse_fixture(document)

    def test_rejects_oversized_theme_id(self) -> None:
        document = base_document()
        document["themes"][0]["theme_id"] = "t" * 65
        with self.assertRaises(FixtureError):
            parse_fixture(document)

    def test_rejects_duplicate_theme_id(self) -> None:
        document = base_document()
        document["themes"].append(dict(document["themes"][0]))
        with self.assertRaises(FixtureError):
            parse_fixture(document)

    def test_rejects_update_for_unknown_theme(self) -> None:
        document = base_document()
        document["updates"] = [
            {"after_completed_reads": 1, "theme_id": "theme-9999", "spacing_px": 4}
        ]
        with self.assertRaises(FixtureError):
            parse_fixture(document)

    def test_rejects_empty_update(self) -> None:
        document = base_document()
        document["updates"] = [{"after_completed_reads": 1, "theme_id": "theme-0001"}]
        with self.assertRaises(FixtureError):
            parse_fixture(document)

    def test_rejects_negative_read_offset(self) -> None:
        document = base_document()
        document["updates"] = [
            {"after_completed_reads": -1, "theme_id": "theme-0001", "spacing_px": 4}
        ]
        with self.assertRaises(FixtureError):
            parse_fixture(document)

    def test_missing_file_is_rejected(self) -> None:
        with self.assertRaises(FixtureError):
            load_fixture(SERVICE_FIXTURE_PATH.parent / "absent.json")


if __name__ == "__main__":
    unittest.main()
