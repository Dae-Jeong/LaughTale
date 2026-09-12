"""원본 상태 소유권 시험 (LAUGH-KNOWLEDGE-READ-001).

조회된 값이 불변인지만 보지 않고, **공개 관측값으로 원본을 바꿀 수 없는지**를
확인합니다. 변경 카운터·캐시 무효화를 우회하는 공개 경로가 있으면 늦은 fill 방어가
무력해지므로 이 시험이 그 경계를 지킵니다.

관측 계약:
  - `read_only_view()` / `origin_view()`는 **live** 읽기 전용 보기입니다.
    쓰기가 불가능하고, 이후 갱신은 그대로 비칩니다.
  - `copy_of_themes()` / `copy_origin()`은 호출 시점을 고정한 detached 사본입니다.
    이후 갱신이 비치지 않습니다. 요청 hot path에서는 쓰지 않습니다.
"""

import unittest

from oracle import ReferenceThemes, as_tuple

from theme_catalog.core.catalog import build_catalog
from theme_catalog.core.fixture import DEFAULT_FIXTURE_PATH, load_fixture
from theme_catalog.core.store import ThemeStore
from theme_catalog.core.theme import Theme

KNOWN_THEME_ID = "theme-0001"
REPLACEMENT = Theme(
    theme_id=KNOWN_THEME_ID,
    background="#000000",
    foreground="#FFFFFF",
    spacing_px=1,
)


class StoreOwnershipTests(unittest.TestCase):
    """`ThemeStore`가 원본 dict를 공개하지 않는지 확인합니다."""

    def setUp(self) -> None:
        self.store = ThemeStore(load_fixture(DEFAULT_FIXTURE_PATH).themes)
        self.reference = ReferenceThemes()

    def test_no_public_mutable_mapping_attribute(self) -> None:
        """이전의 mutable public `themes`/`change_counters`가 남아 있지 않아야 합니다."""
        for name in ("themes", "change_counters"):
            with self.subTest(attribute=name):
                self.assertFalse(hasattr(self.store, name))

    def test_read_only_view_rejects_assignment(self) -> None:
        """반환 타입 `Mapping`에는 `__setitem__`이 없어 타입 검사에서도 거절됩니다.

        런타임에도 실제로 막히는지 확인합니다. 타입 검사를 통과시키려 캐스팅하지 않고
        `getattr`로 쓰기 수단 자체가 없음을 직접 봅니다.
        """
        view = self.store.read_only_view()
        self.assertIsNone(getattr(view, "__setitem__", None))
        with self.assertRaises(TypeError):
            dict.__setitem__(view, KNOWN_THEME_ID, REPLACEMENT)  # ty: ignore[invalid-argument-type]

    def test_read_only_view_rejects_deletion(self) -> None:
        view = self.store.read_only_view()
        self.assertIsNone(getattr(view, "__delitem__", None))
        with self.assertRaises(TypeError):
            dict.__delitem__(view, KNOWN_THEME_ID)  # ty: ignore[invalid-argument-type]

    def test_mutating_a_copy_does_not_touch_the_origin(self) -> None:
        copy = self.store.copy_of_themes()
        copy[KNOWN_THEME_ID] = REPLACEMENT
        copy["injected"] = REPLACEMENT
        theme, _ = self.store.read(KNOWN_THEME_ID)
        self.assertEqual(as_tuple(theme), self.reference.expect(KNOWN_THEME_ID))
        self.assertNotIn("injected", self.store.theme_ids())

    def test_change_counter_only_moves_through_apply_update(self) -> None:
        before = self.store.change_counter(KNOWN_THEME_ID)
        self.store.read(KNOWN_THEME_ID)
        self.store.read_only_view()
        self.store.copy_of_themes()
        self.assertEqual(self.store.change_counter(KNOWN_THEME_ID), before)

        self.store.apply_update(KNOWN_THEME_ID, spacing_px=7)
        self.assertEqual(self.store.change_counter(KNOWN_THEME_ID), before + 1)

    def test_theme_ids_result_is_detached(self) -> None:
        ids = self.store.theme_ids()
        self.assertIsInstance(ids, tuple)
        self.assertEqual(len(ids), len(self.store))

    def test_empty_store_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ThemeStore(())


class ViewSemanticsTests(unittest.TestCase):
    """live 보기와 detached 사본의 뜻이 이름과 맞는지 확인합니다."""

    def setUp(self) -> None:
        self.store = ThemeStore(load_fixture(DEFAULT_FIXTURE_PATH).themes)

    def test_read_only_view_is_live(self) -> None:
        view = self.store.read_only_view()
        before = view[KNOWN_THEME_ID].spacing_px
        self.store.apply_update(KNOWN_THEME_ID, spacing_px=(before + 1) % 65)
        self.assertNotEqual(view[KNOWN_THEME_ID].spacing_px, before)

    def test_copy_is_detached(self) -> None:
        copy = self.store.copy_of_themes()
        before = copy[KNOWN_THEME_ID].spacing_px
        self.store.apply_update(KNOWN_THEME_ID, spacing_px=(before + 1) % 65)
        self.assertEqual(copy[KNOWN_THEME_ID].spacing_px, before)


class CatalogOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """catalog가 노출하는 관측값으로도 원본을 바꿀 수 없어야 합니다."""

    def setUp(self) -> None:
        self.fixture = load_fixture(DEFAULT_FIXTURE_PATH)
        self.reference = ReferenceThemes()

    async def test_origin_view_cannot_bypass_invalidation(self) -> None:
        """관측 보기로 값을 바꿔 캐시 무효화를 건너뛸 수 없어야 합니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        await catalog.get(KNOWN_THEME_ID)

        view = catalog.origin_view()
        self.assertIsNone(getattr(view, "__setitem__", None))
        with self.assertRaises(TypeError):
            dict.__setitem__(view, KNOWN_THEME_ID, REPLACEMENT)  # ty: ignore[invalid-argument-type]

        self.assertEqual(catalog.counters.invalidations, 0)
        theme = await catalog.get(KNOWN_THEME_ID)
        self.assertEqual(as_tuple(theme), self.reference.expect(KNOWN_THEME_ID))

    async def test_copy_origin_is_detached(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        copy = catalog.copy_origin()
        copy[KNOWN_THEME_ID] = REPLACEMENT
        theme = await catalog.get(KNOWN_THEME_ID)
        self.assertEqual(as_tuple(theme), self.reference.expect(KNOWN_THEME_ID))

    async def test_update_through_catalog_moves_counter_and_invalidates(self) -> None:
        """정식 경로만 카운터를 올리고 캐시를 비웁니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        await catalog.get(KNOWN_THEME_ID)
        before = catalog.store.change_counter(KNOWN_THEME_ID)

        await catalog.update(KNOWN_THEME_ID, spacing_px=9)

        self.assertEqual(catalog.store.change_counter(KNOWN_THEME_ID), before + 1)
        self.assertEqual(catalog.counters.invalidations, 1)
        assert catalog.cache is not None
        self.assertIsNone(catalog.cache.get(KNOWN_THEME_ID))

    async def test_returned_theme_is_frozen(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        theme = await catalog.get(KNOWN_THEME_ID)
        with self.assertRaises(AttributeError):
            theme.spacing_px = 1  # ty: ignore[invalid-assignment]


if __name__ == "__main__":
    unittest.main()
