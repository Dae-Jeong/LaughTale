"""경쟁·취소·update barrier 시험 (LAUGH-KNOWLEDGE-READ-001).

명시적 asyncio Event로 순서를 고정하고 선형화 순서·반환값·원본·최종 cache를 검증합니다.
제어 지연은 여기서만 쓰며 성능 모드에서는 설치하지 않습니다.
이 결과는 기능 검증이며 성능 이득의 근거가 아닙니다.
"""

import asyncio
import unittest
from collections.abc import Iterable

from oracle import ReferenceThemes, as_tuple

from theme_catalog.core.cache import BoundedLruCache
from theme_catalog.core.catalog import ThemeCatalog, build_catalog
from theme_catalog.core.control_hooks import ControlHooks
from theme_catalog.core.fixture import DEFAULT_FIXTURE_PATH, load_fixture
from theme_catalog.core.store import ThemeStore
from theme_catalog.core.theme import Theme


class FailingReadStore(ThemeStore):
    """원본 load 오류를 주입하는 대역입니다. 구현 클래스를 수정하지 않습니다."""

    __slots__ = ("failing_theme_id",)

    def __init__(
        self, themes: Iterable[Theme], *, failing_theme_id: str | None
    ) -> None:
        super().__init__(themes)
        self.failing_theme_id = failing_theme_id

    def read(self, theme_id: str) -> tuple[Theme, int]:
        if theme_id == self.failing_theme_id:
            raise RuntimeError("synthetic load failure")
        return super().read(theme_id)


class FailingPutCache(BoundedLruCache):
    """fill 오류를 주입하는 대역입니다."""

    __slots__ = ()

    def put(self, theme_id: str, theme: Theme) -> str | None:
        raise RuntimeError("synthetic fill failure")


class ConcurrencyTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = load_fixture(DEFAULT_FIXTURE_PATH)
        self.reference = ReferenceThemes()
        self.theme_ids = tuple(theme.theme_id for theme in self.fixture.themes)


class UpdateBarrierTests(ConcurrencyTestCase):
    """update barrier: snapshot 전 / 후 load 완료 전 / load 후 fill 전 / fill 후 응답 전."""

    async def test_update_before_snapshot_returns_new_value(self) -> None:
        target = self.theme_ids[0]
        released = asyncio.Event()

        async def before_snapshot(theme_id: str) -> None:
            if theme_id == target:
                await released.wait()

        hooks = ControlHooks(before_snapshot=before_snapshot)
        catalog = build_catalog(
            self.fixture.themes, cache_enabled=True, capacity=16, hooks=hooks
        )

        read_task = asyncio.create_task(catalog.get(target))
        await asyncio.sleep(0)
        await catalog.update(target, spacing_px=21)
        self.reference.apply(target, spacing_px=21)
        released.set()
        theme = await read_task

        self.assertEqual(as_tuple(theme), self.reference.expect(target))
        self.assertEqual(catalog.counters.rejected_fills, 0)
        self.assertEqual(catalog.counters.fills, 1)

    async def test_update_after_snapshot_before_fill_rejects_late_fill(self) -> None:
        """늦은 fill은 거절합니다. 새 값을 옛 snapshot으로 덮지 않습니다."""
        target = self.theme_ids[0]
        snapshot_taken = asyncio.Event()
        may_fill = asyncio.Event()

        async def after_snapshot(theme_id: str) -> None:
            if theme_id == target:
                snapshot_taken.set()

        async def before_fill(theme_id: str) -> None:
            if theme_id == target:
                await may_fill.wait()

        hooks = ControlHooks(after_snapshot=after_snapshot, before_fill=before_fill)
        catalog = build_catalog(
            self.fixture.themes, cache_enabled=True, capacity=16, hooks=hooks
        )

        read_task = asyncio.create_task(catalog.get(target))
        await snapshot_taken.wait()
        await catalog.update(target, spacing_px=33)
        self.reference.apply(target, spacing_px=33)
        may_fill.set()
        stale = await read_task

        # 겹친 조회는 선형화 순서상 옛 값 반환이 허용됩니다.
        self.assertEqual(stale.spacing_px, self.fixture.themes[0].spacing_px)
        # 그러나 옛 값이 캐시에 남으면 실패입니다.
        self.assertEqual(catalog.counters.rejected_fills, 1)
        self.assertEqual(catalog.counters.fills, 0)
        assert catalog.cache is not None
        self.assertEqual(len(catalog.cache), 0)

        # 갱신 뒤 시작된 조회는 새 값을 봅니다.
        fresh = await catalog.get(target)
        self.assertEqual(as_tuple(fresh), self.reference.expect(target))

    async def test_update_after_fill_before_response_leaves_no_stale_cache(
        self,
    ) -> None:
        target = self.theme_ids[0]
        filled = asyncio.Event()
        may_return = asyncio.Event()

        async def after_fill(theme_id: str) -> None:
            if theme_id == target:
                filled.set()
                await may_return.wait()

        hooks = ControlHooks(after_fill=after_fill)
        catalog = build_catalog(
            self.fixture.themes, cache_enabled=True, capacity=16, hooks=hooks
        )

        read_task = asyncio.create_task(catalog.get(target))
        await filled.wait()
        await catalog.update(target, spacing_px=44)
        self.reference.apply(target, spacing_px=44)
        may_return.set()
        await read_task

        self.assertEqual(catalog.counters.invalidations, 1)
        assert catalog.cache is not None
        self.assertIsNone(catalog.cache.get(target))
        fresh = await catalog.get(target)
        self.assertEqual(as_tuple(fresh), self.reference.expect(target))

    async def test_concurrent_misses_agree_and_count_loads(self) -> None:
        """동시 miss 4건입니다. 값이 일치하고 single-flight 없는 load 수와 맞습니다."""
        target = self.theme_ids[0]
        gate = asyncio.Event()
        arrived = 0

        async def after_snapshot(theme_id: str) -> None:
            nonlocal arrived
            if theme_id == target:
                arrived += 1
                if arrived >= 4:
                    gate.set()
                await gate.wait()

        hooks = ControlHooks(after_snapshot=after_snapshot)
        catalog = build_catalog(
            self.fixture.themes, cache_enabled=True, capacity=16, hooks=hooks
        )

        results = await asyncio.gather(*(catalog.get(target) for _ in range(4)))
        for theme in results:
            self.assertEqual(as_tuple(theme), self.reference.expect(target))
        self.assertEqual(catalog.counters.misses, 4)
        self.assertEqual(catalog.counters.origin_loads, 4)
        self.assertEqual(
            catalog.counters.hits + catalog.counters.misses,
            catalog.counters.cache_lookups,
        )

    async def test_concurrent_distinct_keys_stay_separate(self) -> None:
        """동시성4 기능 시험입니다. 대규모 처리량 증거가 아닙니다."""
        targets = self.theme_ids[:4]
        gate = asyncio.Event()
        arrived = 0

        async def after_snapshot(theme_id: str) -> None:
            nonlocal arrived
            arrived += 1
            if arrived >= 4:
                gate.set()
            await gate.wait()

        hooks = ControlHooks(after_snapshot=after_snapshot)
        catalog = build_catalog(
            self.fixture.themes, cache_enabled=True, capacity=16, hooks=hooks
        )

        results = await asyncio.gather(*(catalog.get(key) for key in targets))
        for theme_id, theme in zip(targets, results, strict=True):
            self.assertEqual(theme.theme_id, theme_id)
            self.assertEqual(as_tuple(theme), self.reference.expect(theme_id))


class CancellationTests(ConcurrencyTestCase):
    """취소·오류가 다른 요청을 취소하거나 lock을 남기지 않는지 확인합니다."""

    async def test_cancelled_read_leaves_no_cache_entry(self) -> None:
        target = self.theme_ids[0]
        blocked = asyncio.Event()

        async def before_fill(theme_id: str) -> None:
            if theme_id == target:
                blocked.set()
                await asyncio.Event().wait()

        hooks = ControlHooks(before_fill=before_fill)
        catalog = build_catalog(
            self.fixture.themes, cache_enabled=True, capacity=16, hooks=hooks
        )

        task = asyncio.create_task(catalog.get(target))
        await blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        assert catalog.cache is not None
        self.assertEqual(len(catalog.cache), 0)
        self.assertEqual(catalog.counters.cancellations, 1)
        self.assertFalse(catalog.lock.locked())

    async def test_cancelled_read_does_not_cancel_others(self) -> None:
        first_key = self.theme_ids[0]
        second_key = self.theme_ids[1]
        blocked = asyncio.Event()

        async def before_fill(theme_id: str) -> None:
            if theme_id == first_key:
                blocked.set()
                await asyncio.Event().wait()

        hooks = ControlHooks(before_fill=before_fill)
        catalog = build_catalog(
            self.fixture.themes, cache_enabled=True, capacity=16, hooks=hooks
        )

        doomed = asyncio.create_task(catalog.get(first_key))
        await blocked.wait()
        survivor = asyncio.create_task(catalog.get(second_key))
        doomed.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await doomed
        theme = await survivor

        self.assertEqual(as_tuple(theme), self.reference.expect(second_key))
        self.assertFalse(catalog.lock.locked())

    async def test_load_error_releases_lock_and_stores_nothing(self) -> None:
        """원본 오류가 lock을 남기거나 실패값을 캐시에 남기면 실패입니다."""
        target = self.theme_ids[0]
        store = FailingReadStore(self.fixture.themes, failing_theme_id=target)
        catalog = ThemeCatalog(store, cache=BoundedLruCache(16))

        with self.assertRaises(RuntimeError):
            await catalog.get(target)

        self.assertEqual(catalog.counters.errors, 1)
        self.assertFalse(catalog.lock.locked())
        assert catalog.cache is not None
        self.assertIsNone(catalog.cache.get(target))

        # 오류가 사라지면 같은 reader로 정상 조회가 이어집니다.
        store.failing_theme_id = None
        theme = await catalog.get(target)
        self.assertEqual(as_tuple(theme), self.reference.expect(target))

    async def test_fill_error_releases_lock(self) -> None:
        target = self.theme_ids[0]
        catalog = ThemeCatalog(
            ThemeStore(self.fixture.themes), cache=FailingPutCache(16)
        )

        with self.assertRaises(RuntimeError):
            await catalog.get(target)

        self.assertEqual(catalog.counters.errors, 1)
        self.assertFalse(catalog.lock.locked())
        assert catalog.cache is not None
        self.assertEqual(len(catalog.cache), 0)

    async def test_timeout_on_read_cleans_up(self) -> None:
        target = self.theme_ids[0]

        async def before_fill(theme_id: str) -> None:
            if theme_id == target:
                await asyncio.Event().wait()

        hooks = ControlHooks(before_fill=before_fill)
        catalog = build_catalog(
            self.fixture.themes, cache_enabled=True, capacity=16, hooks=hooks
        )

        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.05):
                await catalog.get(target)

        assert catalog.cache is not None
        self.assertEqual(len(catalog.cache), 0)
        self.assertFalse(catalog.lock.locked())

    async def test_normal_path_installs_no_control_hooks(self) -> None:
        """정상 조회 경로에는 제어 지연이 없습니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        self.assertFalse(catalog.hooks.enabled())


if __name__ == "__main__":
    unittest.main()
