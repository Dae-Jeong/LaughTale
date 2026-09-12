"""원본·캐시 조회·갱신 계약 시험 (LAUGH-KNOWLEDGE-READ-001).

독립 oracle과 대조합니다. cache-off/on 동등성만으로 통과하지 않습니다.
"""

import unittest

from oracle import ReferenceThemes, as_tuple

from theme_catalog.core.cache import BoundedLruCache
from theme_catalog.core.catalog import build_catalog
from theme_catalog.core.counters import ReadCounters
from theme_catalog.core.fixture import DEFAULT_FIXTURE_PATH, load_fixture
from theme_catalog.core.store import EmptyUpdateError, ThemeNotFoundError, ThemeStore
from theme_catalog.core.theme import InvalidThemeValueError
from theme_catalog.tools.measure import key_sequence


class CatalogTestCase(unittest.IsolatedAsyncioTestCase):
    """모든 시험이 자기 소유 fixture·catalog·cache만 만듭니다."""

    def setUp(self) -> None:
        self.fixture = load_fixture(DEFAULT_FIXTURE_PATH)
        self.reference = ReferenceThemes()
        self.theme_ids = tuple(theme.theme_id for theme in self.fixture.themes)


class BaselineOracleTests(CatalogTestCase):
    """F0: cache-off 원본 lookup·갱신·독립 기대값 대조."""

    async def test_every_theme_matches_reference(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        for theme_id in self.theme_ids:
            theme = await catalog.get(theme_id)
            self.assertEqual(as_tuple(theme), self.reference.expect(theme_id))
        self.assertEqual(catalog.counters.errors, 0)

    async def test_unknown_theme_is_not_found(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        with self.assertRaises(ThemeNotFoundError):
            await catalog.get("theme-9999")
        self.assertEqual(catalog.counters.not_found, 1)

    async def test_update_is_visible_to_later_read(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        target = self.theme_ids[0]
        await catalog.update(target, spacing_px=10)
        self.reference.apply(target, spacing_px=10)
        theme = await catalog.get(target)
        self.assertEqual(as_tuple(theme), self.reference.expect(target))
        self.assertEqual(theme.spacing_px, 10)

    async def test_update_touches_only_target_theme(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        target = self.theme_ids[3]
        await catalog.update(target, background="#0A0A0A")
        self.reference.apply(target, background="#0A0A0A")
        for theme_id in self.theme_ids:
            theme = await catalog.get(theme_id)
            self.assertEqual(as_tuple(theme), self.reference.expect(theme_id))

    async def test_update_on_unknown_theme_is_not_found(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        with self.assertRaises(ThemeNotFoundError):
            await catalog.update("theme-9999", spacing_px=4)

    async def test_update_rejects_invalid_value(self) -> None:
        """원본 값 규칙은 fixture 적재가 아니라 theme 모듈이 소유합니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        with self.assertRaises(InvalidThemeValueError):
            await catalog.update(self.theme_ids[0], spacing_px=200)
        theme = await catalog.get(self.theme_ids[0])
        self.assertEqual(as_tuple(theme), self.reference.expect(self.theme_ids[0]))

    async def test_update_without_attributes_is_rejected(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        with self.assertRaises(EmptyUpdateError):
            await catalog.update(self.theme_ids[0])

    async def test_returned_value_is_immutable(self) -> None:
        """같은 객체 반환 계약입니다. mutable dict를 노출하지 않습니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        theme = await catalog.get(self.theme_ids[0])
        with self.assertRaises(AttributeError):
            theme.spacing_px = 1  # ty: ignore[invalid-assignment]

    async def test_negative_control_detects_wrong_expectation(self) -> None:
        """독립 기대값을 일부러 틀리면 대조가 실패해야 합니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        target = self.theme_ids[0]
        self.reference.apply(
            target, spacing_px=(self.reference.expect(target)[2] + 1) % 65
        )
        theme = await catalog.get(target)
        self.assertNotEqual(as_tuple(theme), self.reference.expect(target))

    async def test_negative_control_detects_missing_update(self) -> None:
        """원본 갱신이 누락되면 독립 기대값과 어긋납니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        target = self.theme_ids[1]
        self.reference.apply(target, spacing_px=63)
        theme = await catalog.get(target)
        self.assertNotEqual(as_tuple(theme), self.reference.expect(target))

    async def test_counters_balance_on_cache_off(self) -> None:
        """cache-off는 cache lookup을 세지 않습니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        for theme_id in self.theme_ids:
            await catalog.get(theme_id)
        counters = catalog.counters
        self.assertEqual(counters.cache_lookups, 0)
        self.assertEqual(counters.hits, 0)
        self.assertEqual(counters.misses, 0)
        self.assertEqual(sum(counters.key_reads.values()), len(self.theme_ids))


class CacheFunctionalTests(CatalogTestCase):
    """KR-2: cold miss, warm hit, eviction, 무효화, 계수 정합."""

    async def test_cold_miss_then_warm_hit(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        target = self.theme_ids[0]
        first = await catalog.get(target)
        self.assertEqual(catalog.counters.misses, 1)
        self.assertEqual(catalog.counters.hits, 0)
        second = await catalog.get(target)
        self.assertEqual(catalog.counters.hits, 1)
        self.assertEqual(catalog.counters.origin_loads, 1)
        self.assertEqual(as_tuple(first), self.reference.expect(target))
        self.assertEqual(as_tuple(second), self.reference.expect(target))

    async def test_distinct_themes_do_not_collide(self) -> None:
        """key 충돌 검출입니다. 서로 다른 theme_id가 같은 값을 돌려주면 실패합니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        for theme_id in self.theme_ids[:8]:
            await catalog.get(theme_id)
        for theme_id in self.theme_ids[:8]:
            theme = await catalog.get(theme_id)
            self.assertEqual(theme.theme_id, theme_id)
            self.assertEqual(as_tuple(theme), self.reference.expect(theme_id))

    async def test_capacity_overflow_evicts_oldest(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        for theme_id in self.theme_ids[:17]:
            await catalog.get(theme_id)
        assert catalog.cache is not None
        self.assertEqual(len(catalog.cache), 16)
        self.assertEqual(catalog.counters.evictions, 1)
        # 가장 오래된 key가 빠졌으므로 다시 읽으면 miss입니다.
        before = catalog.counters.misses
        await catalog.get(self.theme_ids[0])
        self.assertEqual(catalog.counters.misses, before + 1)

    async def test_hot_working_set_stays_cached(self) -> None:
        """hot4 순환은 32테마 순환과 다른 조건입니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        keys = key_sequence(self.theme_ids, pattern="hot4", count=40)
        for theme_id in keys:
            await catalog.get(theme_id)
        self.assertEqual(catalog.counters.misses, 4)
        self.assertEqual(catalog.counters.hits, 36)
        self.assertEqual(catalog.counters.evictions, 0)

    async def test_cycle32_over_capacity16_never_hits(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        keys = key_sequence(self.theme_ids, pattern="cycle32", count=64)
        for theme_id in keys:
            await catalog.get(theme_id)
        self.assertEqual(catalog.counters.hits, 0)
        self.assertEqual(catalog.counters.misses, 64)

    async def test_update_invalidates_cached_key(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        target = self.theme_ids[0]
        await catalog.get(target)
        await catalog.update(target, spacing_px=10)
        self.reference.apply(target, spacing_px=10)
        self.assertEqual(catalog.counters.invalidations, 1)
        theme = await catalog.get(target)
        self.assertEqual(as_tuple(theme), self.reference.expect(target))
        self.assertEqual(theme.spacing_px, 10)

    async def test_update_does_not_invalidate_other_keys(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        for theme_id in self.theme_ids[:4]:
            await catalog.get(theme_id)
        await catalog.update(self.theme_ids[0], spacing_px=12)
        self.reference.apply(self.theme_ids[0], spacing_px=12)
        before_hits = catalog.counters.hits
        for theme_id in self.theme_ids[1:4]:
            theme = await catalog.get(theme_id)
            self.assertEqual(as_tuple(theme), self.reference.expect(theme_id))
        self.assertEqual(catalog.counters.hits, before_hits + 3)

    async def test_cache_on_matches_oracle_under_updates(self) -> None:
        """갱신이 섞여도 캐시 경로가 독립 기대값과 일치해야 합니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        for round_index in range(6):
            for theme_id in self.theme_ids:
                theme = await catalog.get(theme_id)
                self.assertEqual(as_tuple(theme), self.reference.expect(theme_id))
            target = self.theme_ids[round_index]
            new_spacing = (round_index * 7) % 65
            await catalog.update(target, spacing_px=new_spacing)
            self.reference.apply(target, spacing_px=new_spacing)

    async def test_not_found_is_never_cached(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        for _ in range(3):
            with self.assertRaises(ThemeNotFoundError):
                await catalog.get("theme-9999")
        assert catalog.cache is not None
        self.assertEqual(len(catalog.cache), 0)
        self.assertEqual(catalog.counters.not_found, 3)
        self.assertEqual(catalog.counters.fills, 0)

    async def test_lookup_counters_balance(self) -> None:
        """hit+miss=cache lookup, single-flight 없는 miss=원본 load입니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        keys = key_sequence(self.theme_ids, pattern="hot4", count=25)
        for theme_id in keys:
            await catalog.get(theme_id)
        counters = catalog.counters
        self.assertEqual(counters.hits + counters.misses, counters.cache_lookups)
        self.assertEqual(counters.misses, counters.origin_loads)
        self.assertEqual(sum(counters.key_reads.values()), 25)

    async def test_cache_off_and_on_agree_on_every_key(self) -> None:
        """동등성 확인입니다. 이것만으로 통과하지 않으며 oracle 대조를 함께 둡니다."""
        off = build_catalog(self.fixture.themes, cache_enabled=False)
        on = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        for theme_id in self.theme_ids * 2:
            left = await off.get(theme_id)
            right = await on.get(theme_id)
            self.assertEqual(as_tuple(left), as_tuple(right))
            self.assertEqual(as_tuple(left), self.reference.expect(theme_id))


class CacheUnitTests(unittest.TestCase):
    """bounded LRU 자체의 경계입니다. 잘못된 eviction 처리를 검출합니다."""

    def test_rejects_non_positive_capacity(self) -> None:
        with self.assertRaises(ValueError):
            BoundedLruCache(0)

    def test_lru_order_is_recency_based(self) -> None:
        cache = BoundedLruCache(2)
        cache.put("a", "a")  # ty: ignore[invalid-argument-type]
        cache.put("b", "b")  # ty: ignore[invalid-argument-type]
        cache.get("a")
        evicted = cache.put("c", "c")  # ty: ignore[invalid-argument-type]
        self.assertEqual(evicted, "b")
        self.assertIsNotNone(cache.get("a"))
        self.assertIsNone(cache.get("b"))

    def test_invalidate_reports_presence(self) -> None:
        cache = BoundedLruCache(2)
        cache.put("a", "a")  # ty: ignore[invalid-argument-type]
        self.assertTrue(cache.invalidate("a"))
        self.assertFalse(cache.invalidate("a"))

    def test_clear_empties_only_this_instance(self) -> None:
        left = BoundedLruCache(2)
        right = BoundedLruCache(2)
        left.put("a", "a")  # ty: ignore[invalid-argument-type]
        right.put("a", "a")  # ty: ignore[invalid-argument-type]
        left.clear()
        self.assertEqual(len(left), 0)
        self.assertEqual(len(right), 1)


class StoreIsolationTests(CatalogTestCase):
    """reader·store 인스턴스끼리 원본·캐시를 공유하지 않습니다."""

    async def test_separate_readers_have_separate_state(self) -> None:
        left = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        right = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        target = self.theme_ids[0]

        await left.update(target, spacing_px=7)
        left_theme = await left.get(target)
        right_theme = await right.get(target)

        self.assertEqual(left_theme.spacing_px, 7)
        self.assertEqual(as_tuple(right_theme), self.reference.expect(target))

    async def test_fixture_object_is_not_mutated_by_updates(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        target = self.theme_ids[0]
        original = self.fixture.themes[0]
        await catalog.update(target, spacing_px=(original.spacing_px + 1) % 65)
        self.assertEqual(self.fixture.themes[0], original)

    def test_store_does_not_expose_internal_dict(self) -> None:
        store = ThemeStore(self.fixture.themes)
        self.assertIsInstance(store.theme_ids(), tuple)

    def test_counters_snapshot_is_detached(self) -> None:
        counters = ReadCounters()
        counters.key_reads["theme-0001"] = 1
        snapshot = counters.snapshot()
        counters.key_reads["theme-0001"] = 99
        self.assertEqual(snapshot["key_reads"], {"theme-0001": 1})


if __name__ == "__main__":
    unittest.main()
