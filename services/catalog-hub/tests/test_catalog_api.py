"""조립 조회·캐시 정합성 시험 (Laughtale 캐싱 실험).

합성 DB가 필요합니다. 없으면 skip합니다 (`pytest -m postgres`로 선택 실행).

**oracle 검증은 성능 회차와 분리**됩니다. 이 파일은 기능 검증 전용이며 여기서
측정한 시간을 성능 근거로 쓰지 않습니다.
"""

import asyncio
import unittest

from fastapi.testclient import TestClient
from oracle import (
    fetch_expected_item,
    fetch_expected_labels,
    fetch_expected_shape,
    flatten_categories,
    flatten_items,
)

from catalog_hub.bootstrap.app import create_app
from catalog_hub.core.settings import SUPPORTED_LANGS, Settings
from catalog_hub.tools.release_transition import (
    publish_successor_release,
    rollback_to_release,
)

DB_URL = "postgresql+asyncpg://thready:thready@127.0.0.1:5433/catalog_hub_lab"


def build_settings(
    *, response_cache_enabled: bool = False, stage_timing_enabled: bool = False
) -> Settings:
    """시험 소유 설정입니다. 기존 서비스 설정·환경·.env를 읽지 않습니다."""
    return Settings(
        _env_file=None,
        db_url=DB_URL,
        response_cache_enabled=response_cache_enabled,
        stage_timing_enabled=stage_timing_enabled,
    )


def database_available() -> bool:
    """합성 DB에 붙을 수 있는지 확인합니다."""
    try:
        with TestClient(create_app(build_settings())) as client:
            return client.get("/health/ready").status_code == 200
    except Exception:
        return False


DB_READY = database_available()
requires_db = unittest.skipUnless(DB_READY, "synthetic catalog_hub_lab database absent")


@requires_db
class ShapeOracleTests(unittest.TestCase):
    """응답 규모가 독립 oracle과 일치하는지 확인합니다."""

    def test_counts_match_independent_oracle(self) -> None:
        expected = asyncio.run(fetch_expected_shape())
        with TestClient(create_app(build_settings())) as client:
            body = client.get("/v1/catalog/full?lang=ko").json()

        self.assertEqual(body["release_id"], expected["release_id"])
        self.assertEqual(body["category_count"], expected["category_count"])
        self.assertEqual(body["item_count"], expected["item_count"])
        self.assertEqual(len(body["roots"]), expected["root_count"])

    def test_tree_contains_every_category_once(self) -> None:
        """조립 트리가 분류를 빠뜨리거나 중복하지 않아야 합니다."""
        expected = asyncio.run(fetch_expected_shape())
        with TestClient(create_app(build_settings())) as client:
            body = client.get("/v1/catalog/full?lang=ko").json()
        flat = flatten_categories(body["roots"])
        self.assertEqual(len(flat), expected["category_count"])

    def test_tree_contains_every_item_once(self) -> None:
        expected = asyncio.run(fetch_expected_shape())
        with TestClient(create_app(build_settings())) as client:
            body = client.get("/v1/catalog/full?lang=ko").json()
        flat = flatten_items(body["roots"])
        self.assertEqual(len(flat), expected["item_count"])


@requires_db
class I18nOracleTests(unittest.TestCase):
    """i18n 치환이 독립 기대값과 일치하는지 확인합니다."""

    def test_category_labels_match_oracle(self) -> None:
        for lang in ("ko", "en", "ja"):
            with self.subTest(lang=lang):
                expected = asyncio.run(fetch_expected_labels(lang))
                with TestClient(create_app(build_settings())) as client:
                    body = client.get(f"/v1/catalog/full?lang={lang}").json()
                flat = flatten_categories(body["roots"])
                for code, label in expected.items():
                    self.assertEqual(flat[code]["label"], label)

    def test_item_detail_matches_oracle(self) -> None:
        with TestClient(create_app(build_settings())) as client:
            body = client.get("/v1/catalog/full?lang=ko").json()
        items = flatten_items(body["roots"])
        sample_code = sorted(items)[0]
        expected = asyncio.run(fetch_expected_item(sample_code, "ko"))
        actual = items[sample_code]

        self.assertEqual(actual["label"], expected["label"])
        self.assertEqual(actual["magnitude"], expected["magnitude"])
        self.assertEqual(len(actual["attributes"]), expected["attribute_count"])
        self.assertEqual(len(actual["related_codes"]), expected["related_count"])

    def test_languages_produce_different_labels(self) -> None:
        """언어별로 실제 다른 라벨이 나와야 합니다. 치환 누락 검출용입니다."""
        with TestClient(create_app(build_settings())) as client:
            ko = client.get("/v1/catalog/full?lang=ko").json()
            en = client.get("/v1/catalog/full?lang=en").json()
        ko_flat = flatten_categories(ko["roots"])
        en_flat = flatten_categories(en["roots"])
        code = sorted(ko_flat)[0]
        self.assertNotEqual(ko_flat[code]["label"], en_flat[code]["label"])

    def test_unsupported_lang_falls_back(self) -> None:
        with TestClient(create_app(build_settings())) as client:
            body = client.get("/v1/catalog/full?lang=xx").json()
        self.assertEqual(body["lang"], "ko")

    def test_every_supported_lang_resolves(self) -> None:
        with TestClient(create_app(build_settings())) as client:
            for lang in SUPPORTED_LANGS:
                with self.subTest(lang=lang):
                    response = client.get(f"/v1/catalog/full?lang={lang}")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["lang"], lang)


@requires_db
class CacheConsistencyTests(unittest.TestCase):
    """캐시 경로가 캐시 우회 직조회와 같은 답을 주는지 확인합니다 (R6)."""

    def test_cache_path_equals_bypass_path(self) -> None:
        """A(캐시) == B(우회) — 그리고 둘 다 독립 oracle과 일치해야 합니다."""
        expected = asyncio.run(fetch_expected_shape())

        with TestClient(create_app(build_settings())) as bypass_client:
            bypass = bypass_client.get("/v1/catalog/full?lang=ko").json()

        with TestClient(
            create_app(build_settings(response_cache_enabled=True))
        ) as cache_client:
            first = cache_client.get("/v1/catalog/full?lang=ko")
            second = cache_client.get("/v1/catalog/full?lang=ko")

        self.assertEqual(first.headers["X-Cache"], "miss")
        self.assertEqual(second.headers["X-Cache"], "hit")
        self.assertEqual(first.json(), bypass)
        self.assertEqual(second.json(), bypass)
        # A와 B가 함께 틀려도 잡히도록 고정 기대값과 대조합니다.
        self.assertEqual(bypass["category_count"], expected["category_count"])
        self.assertEqual(bypass["item_count"], expected["item_count"])

    def test_cache_keys_separate_languages(self) -> None:
        """lang이 키에 들어가므로 언어가 섞이면 안 됩니다."""
        with TestClient(
            create_app(build_settings(response_cache_enabled=True))
        ) as client:
            ko = client.get("/v1/catalog/full?lang=ko").json()
            en = client.get("/v1/catalog/full?lang=en").json()
            ko_again = client.get("/v1/catalog/full?lang=ko").json()

            stats = client.get("/v1/stats").json()["data"]["cache"]

        self.assertEqual(ko["lang"], "ko")
        self.assertEqual(en["lang"], "en")
        self.assertEqual(ko_again, ko)
        self.assertEqual(stats["entries"], 2)

    def test_cache_hit_skips_db_and_assemble_stages(self) -> None:
        """D1·D4 확인: hit에서 db/assemble은 사라지고 release·직렬화는 남습니다."""
        with TestClient(
            create_app(
                build_settings(response_cache_enabled=True, stage_timing_enabled=True)
            )
        ) as client:
            import json as json_module

            client.get("/v1/catalog/full?lang=ko")
            hit = client.get("/v1/catalog/full?lang=ko")
            stages = json_module.loads(hit.headers["X-Stage-Timing"])

        self.assertEqual(hit.headers["X-Cache"], "hit")
        self.assertNotIn("db_query", stages)
        self.assertNotIn("assemble_i18n", stages)
        self.assertIn("release_lookup", stages)
        self.assertIn("serialize", stages)


@requires_db
class ReleaseTransitionTests(unittest.TestCase):
    """release 전환에서 캐시가 옛 응답을 내놓지 않는지 확인합니다 (D4 핵심).

    이 시험이 없으면 캐시 키에서 release_id를 없애도 검출되지 않습니다.
    전환을 실제로 일으켜야 키잉이 하는 일이 드러납니다.
    """

    def setUp(self) -> None:
        self.transition: tuple[int, int] | None = None

    def tearDown(self) -> None:
        if self.transition is not None:
            old_id, new_id = self.transition
            asyncio.run(rollback_to_release(old_id, new_id))

    def test_cache_serves_new_release_after_transition(self) -> None:
        """전환 전 캐시된 응답이 전환 후에 노출되면 실패입니다."""
        with TestClient(
            create_app(build_settings(response_cache_enabled=True))
        ) as client:
            before = client.get("/v1/catalog/full?lang=ko").json()
            self.assertEqual(
                client.get("/v1/catalog/full?lang=ko").headers["X-Cache"], "hit"
            )

            self.transition = asyncio.run(publish_successor_release("t1"))
            old_id, new_id = self.transition

            after = client.get("/v1/catalog/full?lang=ko")
            body = after.json()

        self.assertEqual(before["release_id"], old_id)
        self.assertEqual(body["release_id"], new_id)
        # 전환 후 첫 요청은 새 키이므로 miss여야 합니다.
        self.assertEqual(after.headers["X-Cache"], "miss")
        # 새 release의 라벨이 실제로 반영되어야 합니다.
        sample = flatten_categories(body["roots"])[
            sorted(flatten_categories(body["roots"]))[0]
        ]
        self.assertTrue(sample["label"].startswith("SUCCESSOR "))

    def test_old_release_entry_remains_but_is_unreachable(self) -> None:
        """옛 엔트리는 메모리에 남아도 조회되지 않습니다 — 정합성과 메모리의 구분."""
        with TestClient(
            create_app(build_settings(response_cache_enabled=True))
        ) as client:
            client.get("/v1/catalog/full?lang=ko")
            self.transition = asyncio.run(publish_successor_release("t2"))
            client.get("/v1/catalog/full?lang=ko")
            stats = client.get("/v1/stats").json()["data"]["cache"]

        # 두 release의 엔트리가 공존합니다. 이것이 Step 2에서 잴 메모리 누적입니다.
        self.assertEqual(len(stats["release_ids"]), 2)

    def test_bypass_path_also_sees_new_release(self) -> None:
        """캐시 없이도 같은 답이어야 합니다 (A/B 동시 오류 방지)."""
        self.transition = asyncio.run(publish_successor_release("t3"))
        _, new_id = self.transition
        expected = asyncio.run(fetch_expected_shape())

        with TestClient(create_app(build_settings())) as client:
            body = client.get("/v1/catalog/full?lang=ko").json()

        self.assertEqual(body["release_id"], new_id)
        self.assertEqual(body["release_id"], expected["release_id"])
        self.assertEqual(body["category_count"], expected["category_count"])


@requires_db
class StageTimingTests(unittest.TestCase):
    """구간 타이머 계약입니다."""

    def test_timing_off_reports_no_stages(self) -> None:
        with TestClient(
            create_app(build_settings(stage_timing_enabled=False))
        ) as client:
            response = client.get("/v1/catalog/full?lang=ko")
        self.assertNotIn("X-Stage-Timing", response.headers)
        self.assertIn("X-Server-Total-Ns", response.headers)

    def test_timing_off_records_no_stage_samples(self) -> None:
        """타이머 off 회차가 구간을 기록하면 보정 기준이 무너집니다."""
        app = create_app(build_settings(stage_timing_enabled=False))
        with TestClient(app) as client:
            client.get("/v1/catalog/full?lang=ko")
            samples = app.state.stage_samples
            self.assertTrue(samples)
            for sample in samples:
                self.assertFalse(sample.timing_enabled)
                self.assertEqual(sample.stages, {})

    def test_timing_on_reports_all_stages_on_miss(self) -> None:
        import json as json_module

        with TestClient(
            create_app(build_settings(stage_timing_enabled=True))
        ) as client:
            response = client.get("/v1/catalog/full?lang=ko")
            stages = json_module.loads(response.headers["X-Stage-Timing"])

        for stage in (
            "routing",
            "release_lookup",
            "db_query",
            "assemble_i18n",
            "serialize",
        ):
            with self.subTest(stage=stage):
                self.assertIn(stage, stages)
                self.assertGreaterEqual(stages[stage], 0)

    def test_stage_sum_does_not_exceed_server_total(self) -> None:
        """구간 합계는 전체 서버 시간을 넘을 수 없습니다."""
        import json as json_module

        with TestClient(
            create_app(build_settings(stage_timing_enabled=True))
        ) as client:
            response = client.get("/v1/catalog/full?lang=ko")
            stages = json_module.loads(response.headers["X-Stage-Timing"])
            total = int(response.headers["X-Server-Total-Ns"])

        self.assertLessEqual(sum(stages.values()), total)


@requires_db
class AppIsolationTests(unittest.TestCase):
    """앱별 상태 격리입니다."""

    def test_caches_are_per_app(self) -> None:
        left = create_app(build_settings(response_cache_enabled=True))
        right = create_app(build_settings(response_cache_enabled=True))
        with TestClient(left) as left_client, TestClient(right) as right_client:
            left_client.get("/v1/catalog/full?lang=ko")
            left_stats = left_client.get("/v1/stats").json()["data"]["cache"]
            right_stats = right_client.get("/v1/stats").json()["data"]["cache"]

        self.assertEqual(left_stats["entries"], 1)
        self.assertEqual(right_stats["entries"], 0)

    def test_cache_disabled_reports_none(self) -> None:
        with TestClient(create_app(build_settings())) as client:
            data = client.get("/v1/stats").json()["data"]
        self.assertFalse(data["cache_enabled"])
        self.assertIsNone(data["cache"])

    def test_resources_released_after_shutdown(self) -> None:
        app = create_app(build_settings())
        with TestClient(app):
            self.assertTrue(hasattr(app.state, "engine"))
        self.assertFalse(hasattr(app.state, "engine"))
        self.assertFalse(hasattr(app.state, "session_factory"))


if __name__ == "__main__":
    unittest.main()
