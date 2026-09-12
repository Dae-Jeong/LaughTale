"""DB 없이 검증 가능한 단위 계약 (Laughtale 캐싱 실험)."""

import unittest

from catalog_hub.core.assembled import AssembledCatalog, CacheKey
from catalog_hub.core.response_cache import ResponseCache
from catalog_hub.core.settings import SUPPORTED_LANGS, Settings
from catalog_hub.core.stage_timer import (
    STAGE_DB,
    STAGE_ORDER,
    STAGES_SURVIVING_CACHE_HIT,
    NullStageTimer,
    StageSample,
    StageTimer,
    create_stage_timer,
)
from catalog_hub.tools.seed import DEFAULT_PLAN


def make_catalog(release_id: int = 1, lang: str = "ko") -> AssembledCatalog:
    return AssembledCatalog(
        release_id=release_id,
        release_label=f"rel-{release_id}",
        lang=lang,
        roots=(),
        category_count=0,
        item_count=0,
    )


class CacheKeyTests(unittest.TestCase):
    """release_id 키잉이 stale을 구조적으로 막는지 확인합니다 (D4)."""

    def test_key_is_hashable_and_value_based(self) -> None:
        left = CacheKey(release_id=1, endpoint="catalog.full", lang="ko")
        right = CacheKey(release_id=1, endpoint="catalog.full", lang="ko")
        self.assertEqual(left, right)
        self.assertEqual(hash(left), hash(right))

    def test_release_change_yields_a_different_key(self) -> None:
        old = CacheKey(release_id=1, endpoint="catalog.full", lang="ko")
        new = CacheKey(release_id=2, endpoint="catalog.full", lang="ko")
        self.assertNotEqual(old, new)

    def test_lang_is_part_of_the_key(self) -> None:
        self.assertNotEqual(
            CacheKey(release_id=1, endpoint="catalog.full", lang="ko"),
            CacheKey(release_id=1, endpoint="catalog.full", lang="en"),
        )


class ResponseCacheTests(unittest.TestCase):
    def test_miss_then_hit(self) -> None:
        cache = ResponseCache()
        key = CacheKey(release_id=1, endpoint="catalog.full", lang="ko")
        self.assertIsNone(cache.get(key))
        cache.put(key, make_catalog())
        self.assertIsNotNone(cache.get(key))
        self.assertEqual(cache.misses, 1)
        self.assertEqual(cache.hits, 1)

    def test_old_release_entry_is_never_returned_for_new_key(self) -> None:
        """전환 후 옛 엔트리가 남아 있어도 새 키로는 조회되지 않습니다."""
        cache = ResponseCache()
        old_key = CacheKey(release_id=1, endpoint="catalog.full", lang="ko")
        new_key = CacheKey(release_id=2, endpoint="catalog.full", lang="ko")
        cache.put(old_key, make_catalog(release_id=1))
        self.assertIsNone(cache.get(new_key))

    def test_dropping_other_releases_reclaims_memory_only(self) -> None:
        cache = ResponseCache()
        cache.put(CacheKey(1, "catalog.full", "ko"), make_catalog(1))
        cache.put(CacheKey(2, "catalog.full", "ko"), make_catalog(2))
        dropped = cache.drop_other_releases(2)
        self.assertEqual(dropped, 1)
        self.assertEqual(cache.release_ids(), (2,))

    def test_stats_shape(self) -> None:
        cache = ResponseCache()
        cache.put(CacheKey(1, "catalog.full", "ko"), make_catalog())
        stats = cache.stats()
        self.assertEqual(stats["entries"], 1)
        self.assertEqual(stats["release_ids"], [1])


class StageTimerTests(unittest.TestCase):
    def test_null_timer_records_nothing(self) -> None:
        timer = create_stage_timer(enabled=False)
        timer.mark(STAGE_DB)
        self.assertIsInstance(timer, NullStageTimer)
        self.assertFalse(timer.enabled())
        self.assertEqual(timer.report(), {})

    def test_real_timer_records_stages(self) -> None:
        timer = create_stage_timer(enabled=True)
        self.assertIsInstance(timer, StageTimer)
        timer.mark(STAGE_DB)
        self.assertIn(STAGE_DB, timer.report())
        self.assertTrue(timer.enabled())

    def test_repeated_marks_accumulate(self) -> None:
        timer = StageTimer()
        timer.mark(STAGE_DB)
        first = timer.report()[STAGE_DB]
        timer.mark(STAGE_DB)
        self.assertGreaterEqual(timer.report()[STAGE_DB], first)

    def test_total_is_at_least_stage_sum(self) -> None:
        timer = StageTimer()
        for stage in STAGE_ORDER:
            timer.mark(stage)
        self.assertGreaterEqual(timer.total_ns(), sum(timer.report().values()))

    def test_surviving_stages_match_decisions(self) -> None:
        """D1·D4: 라우팅·release조회·직렬화는 캐시 hit에도 남습니다."""
        self.assertEqual(
            STAGES_SURVIVING_CACHE_HIT,
            frozenset({"routing", "release_lookup", "serialize"}),
        )

    def test_sample_unaccounted_is_non_negative(self) -> None:
        sample = StageSample(
            server_total_ns=1000,
            timing_enabled=True,
            cache_hit=False,
            stages={"routing": 100, "db_query": 400},
        )
        self.assertEqual(sample.accounted_ns(), 500)
        self.assertEqual(sample.unaccounted_ns(), 500)


class SettingsTests(unittest.TestCase):
    def test_defaults_disable_cache_and_timing(self) -> None:
        """Step 0 기준선은 캐시 없음·타이머 없음입니다."""
        settings = Settings(_env_file=None)
        self.assertFalse(settings.response_cache_enabled)
        self.assertFalse(settings.stage_timing_enabled)

    def test_db_url_points_at_synthetic_database(self) -> None:
        """회사 DB(procedure_hub)를 가리키지 않아야 합니다."""
        settings = Settings(_env_file=None)
        self.assertIn("catalog_hub_lab", settings.db_url)
        self.assertNotIn("procedure_hub", settings.db_url)
        self.assertIn("5433", settings.db_url)

    def test_supported_langs(self) -> None:
        self.assertEqual(len(SUPPORTED_LANGS), 7)
        self.assertEqual(SUPPORTED_LANGS[0], "ko")


class SeedPlanTests(unittest.TestCase):
    """합성 생성 계획의 산술이 맞는지 확인합니다."""

    def test_counts_are_self_consistent(self) -> None:
        plan = DEFAULT_PLAN
        rows = plan.estimated_rows()
        self.assertEqual(rows["item"], plan.leaf_count() * plan.items_per_leaf)
        self.assertEqual(rows["attribute"], rows["item"] * plan.attributes_per_item)
        self.assertEqual(rows["item_link"], rows["item"] * plan.links_per_item)

    def test_translations_cover_every_language(self) -> None:
        plan = DEFAULT_PLAN
        rows = plan.estimated_rows()
        entities = rows["category"] + rows["item"] + rows["attribute"]
        self.assertEqual(rows["translation"], entities * len(plan.langs))

    def test_tree_depth_produces_expected_category_count(self) -> None:
        plan = DEFAULT_PLAN
        # 6 + 6*4 + 6*4*4 = 126
        self.assertEqual(plan.category_count(), 126)


if __name__ == "__main__":
    unittest.main()
