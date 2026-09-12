"""인프로세스 ASGI 기능 시험 (LAUGH-KNOWLEDGE-READ-001).

수명·입력·응답·앱별 상태 격리를 검증합니다.
이 결과를 실제 네트워크 처리량으로 표현하지 않습니다. 실제 서버를 띄우지 않습니다.
"""

import asyncio
import unittest
from contextlib import AsyncExitStack
from http import HTTPStatus
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from oracle import ReferenceThemes, payload_tuple

from theme_catalog.bootstrap.app import create_app
from theme_catalog.bootstrap.lifespan import prepare_resources
from theme_catalog.core.fixture import DEFAULT_FIXTURE_PATH, FixtureError
from theme_catalog.core.settings import Settings

KNOWN_THEME_ID = "theme-0001"
OTHER_THEME_ID = "theme-0002"
UNKNOWN_THEME_ID = "theme-9999"


def build_settings(
    *,
    cache_enabled: bool = False,
    cache_capacity: int = 16,
    fixture_path: Path = DEFAULT_FIXTURE_PATH,
) -> Settings:
    """테스트 소유 설정입니다. 기존 서비스 설정·환경·.env를 읽지 않습니다."""
    return Settings(
        _env_file=None,
        cache_enabled=cache_enabled,
        cache_capacity=cache_capacity,
        fixture_path=fixture_path,
    )


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reference = ReferenceThemes()


class ReadApiTests(ApiTestCase):
    """GET /v1/themes/{theme_id}."""

    def test_read_matches_independent_oracle(self) -> None:
        app = create_app(build_settings())
        with TestClient(app) as client:
            for theme_id in self.reference.order:
                response = client.get(f"/v1/themes/{theme_id}")
                self.assertEqual(response.status_code, HTTPStatus.OK)
                body = response.json()["data"]
                self.assertEqual(body["theme_id"], theme_id)
                self.assertEqual(payload_tuple(body), self.reference.expect(theme_id))

    def test_unknown_theme_returns_problem(self) -> None:
        app = create_app(build_settings())
        with TestClient(app) as client:
            response = client.get(f"/v1/themes/{UNKNOWN_THEME_ID}")
            self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
            body = response.json()
            self.assertEqual(body["code"], "THEME_NOT_FOUND")
            self.assertEqual(body["type"], "about:blank")

    def test_not_found_is_not_cached(self) -> None:
        app = create_app(build_settings(cache_enabled=True))
        with TestClient(app) as client:
            for _ in range(3):
                client.get(f"/v1/themes/{UNKNOWN_THEME_ID}")
            stats = client.get("/v1/stats").json()["data"]
            self.assertEqual(stats["cache_entries"], 0)
            self.assertEqual(stats["counters"]["not_found"], 3)

    def test_oversized_theme_id_is_rejected(self) -> None:
        app = create_app(build_settings())
        with TestClient(app) as client:
            response = client.get("/v1/themes/" + "t" * 65)
            self.assertEqual(response.status_code, HTTPStatus.UNPROCESSABLE_ENTITY)
            body = response.json()
            self.assertEqual(body["code"], "INVALID_INPUT")
            self.assertEqual(body["errors"][0]["location"], ["path", "theme_id"])

    def test_error_body_hides_input_values(self) -> None:
        app = create_app(build_settings())
        with TestClient(app) as client:
            response = client.get("/v1/themes/" + "s" * 70)
            body = response.json()
            self.assertNotIn("input", body)
            self.assertNotIn("msg", str(body))
            self.assertNotIn("s" * 70, str(body))


class UpdateApiTests(ApiTestCase):
    """PATCH /v1/themes/{theme_id}."""

    def test_update_then_read_returns_new_value(self) -> None:
        app = create_app(build_settings(cache_enabled=True))
        with TestClient(app) as client:
            client.get(f"/v1/themes/{KNOWN_THEME_ID}")
            patch = client.patch(
                f"/v1/themes/{KNOWN_THEME_ID}", json={"spacing_px": 10}
            )
            self.assertEqual(patch.status_code, HTTPStatus.OK)
            self.reference.apply(KNOWN_THEME_ID, spacing_px=10)

            read = client.get(f"/v1/themes/{KNOWN_THEME_ID}")
            self.assertEqual(
                payload_tuple(read.json()["data"]),
                self.reference.expect(KNOWN_THEME_ID),
            )

    def test_update_invalidates_only_that_key(self) -> None:
        app = create_app(build_settings(cache_enabled=True))
        with TestClient(app) as client:
            client.get(f"/v1/themes/{KNOWN_THEME_ID}")
            client.get(f"/v1/themes/{OTHER_THEME_ID}")
            client.patch(f"/v1/themes/{KNOWN_THEME_ID}", json={"spacing_px": 12})
            stats = client.get("/v1/stats").json()["data"]
            self.assertEqual(stats["counters"]["invalidations"], 1)

            other = client.get(f"/v1/themes/{OTHER_THEME_ID}")
            self.assertEqual(
                payload_tuple(other.json()["data"]),
                self.reference.expect(OTHER_THEME_ID),
            )

    def test_update_on_unknown_theme_returns_not_found(self) -> None:
        app = create_app(build_settings())
        with TestClient(app) as client:
            response = client.patch(
                f"/v1/themes/{UNKNOWN_THEME_ID}", json={"spacing_px": 4}
            )
            self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
            self.assertEqual(response.json()["code"], "THEME_NOT_FOUND")

    def test_empty_patch_is_rejected(self) -> None:
        app = create_app(build_settings())
        with TestClient(app) as client:
            response = client.patch(f"/v1/themes/{KNOWN_THEME_ID}", json={})
            self.assertEqual(response.status_code, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_invalid_color_is_rejected(self) -> None:
        app = create_app(build_settings())
        with TestClient(app) as client:
            response = client.patch(
                f"/v1/themes/{KNOWN_THEME_ID}", json={"background": "not-a-color"}
            )
            self.assertEqual(response.status_code, HTTPStatus.UNPROCESSABLE_ENTITY)
            self.assertEqual(
                response.json()["errors"][0]["location"], ["body", "background"]
            )

    def test_out_of_range_spacing_is_rejected(self) -> None:
        app = create_app(build_settings())
        with TestClient(app) as client:
            response = client.patch(
                f"/v1/themes/{KNOWN_THEME_ID}", json={"spacing_px": 200}
            )
            self.assertEqual(response.status_code, HTTPStatus.UNPROCESSABLE_ENTITY)
            self.assertEqual(
                response.json()["errors"][0]["location"], ["body", "spacing_px"]
            )

    def test_unknown_field_is_rejected(self) -> None:
        app = create_app(build_settings())
        with TestClient(app) as client:
            response = client.patch(
                f"/v1/themes/{KNOWN_THEME_ID}", json={"radius_px": 4}
            )
            self.assertEqual(response.status_code, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_update_does_not_survive_a_new_app(self) -> None:
        """첫 버전의 갱신은 프로세스·앱 수명까지만 유지됩니다."""
        settings = build_settings()
        with TestClient(create_app(settings)) as client:
            client.patch(f"/v1/themes/{KNOWN_THEME_ID}", json={"spacing_px": 3})

        with TestClient(create_app(settings)) as client:
            body = client.get(f"/v1/themes/{KNOWN_THEME_ID}").json()["data"]
            self.assertEqual(payload_tuple(body), self.reference.expect(KNOWN_THEME_ID))


class CacheModeTests(ApiTestCase):
    """cache off/on 경로가 같은 값을 돌려주는지 확인합니다."""

    def test_cache_off_reports_no_lookups(self) -> None:
        app = create_app(build_settings(cache_enabled=False))
        with TestClient(app) as client:
            client.get(f"/v1/themes/{KNOWN_THEME_ID}")
            client.get(f"/v1/themes/{KNOWN_THEME_ID}")
            stats = client.get("/v1/stats").json()["data"]
            self.assertFalse(stats["cache_enabled"])
            self.assertIsNone(stats["cache_capacity"])
            self.assertEqual(stats["counters"]["cache_lookups"], 0)
            self.assertEqual(stats["origin_load_count"], 2)

    def test_cache_on_reports_hit_after_miss(self) -> None:
        app = create_app(build_settings(cache_enabled=True))
        with TestClient(app) as client:
            client.get(f"/v1/themes/{KNOWN_THEME_ID}")
            client.get(f"/v1/themes/{KNOWN_THEME_ID}")
            stats = client.get("/v1/stats").json()["data"]
            self.assertTrue(stats["cache_enabled"])
            self.assertEqual(stats["counters"]["misses"], 1)
            self.assertEqual(stats["counters"]["hits"], 1)
            self.assertEqual(stats["origin_load_count"], 1)

    def test_capacity_is_reported_while_cache_is_empty(self) -> None:
        """빈 캐시는 falsy이므로 진위 판정을 쓰면 cold 상태에서 capacity가 null이 됩니다."""
        app = create_app(build_settings(cache_enabled=True, cache_capacity=16))
        with TestClient(app) as client:
            stats = client.get("/v1/stats").json()["data"]
            self.assertTrue(stats["cache_enabled"])
            self.assertEqual(stats["cache_capacity"], 16)
            self.assertEqual(stats["cache_entries"], 0)

    def test_capacity_is_reported_after_last_entry_is_invalidated(self) -> None:
        """마지막 항목을 무효화해 다시 빈 캐시가 되어도 capacity는 유지됩니다."""
        app = create_app(build_settings(cache_enabled=True, cache_capacity=16))
        with TestClient(app) as client:
            client.get(f"/v1/themes/{KNOWN_THEME_ID}")
            self.assertEqual(client.get("/v1/stats").json()["data"]["cache_entries"], 1)
            client.patch(f"/v1/themes/{KNOWN_THEME_ID}", json={"spacing_px": 5})
            stats = client.get("/v1/stats").json()["data"]
            self.assertEqual(stats["cache_entries"], 0)
            self.assertTrue(stats["cache_enabled"])
            self.assertEqual(stats["cache_capacity"], 16)

    def test_cache_off_reports_none_capacity(self) -> None:
        """cache-off에서만 capacity가 null입니다. 빈 캐시와 구분됩니다."""
        app = create_app(build_settings(cache_enabled=False))
        with TestClient(app) as client:
            stats = client.get("/v1/stats").json()["data"]
            self.assertFalse(stats["cache_enabled"])
            self.assertIsNone(stats["cache_capacity"])
            self.assertEqual(stats["cache_entries"], 0)

    def test_both_modes_agree_on_every_theme(self) -> None:
        off = create_app(build_settings(cache_enabled=False))
        on = create_app(build_settings(cache_enabled=True))
        with TestClient(off) as off_client, TestClient(on) as on_client:
            for theme_id in self.reference.order:
                left = off_client.get(f"/v1/themes/{theme_id}").json()["data"]
                right = on_client.get(f"/v1/themes/{theme_id}").json()["data"]
                self.assertEqual(left, right)
                self.assertEqual(payload_tuple(left), self.reference.expect(theme_id))

    def test_capacity_bound_is_respected(self) -> None:
        app = create_app(build_settings(cache_enabled=True, cache_capacity=4))
        with TestClient(app) as client:
            for theme_id in self.reference.order:
                client.get(f"/v1/themes/{theme_id}")
            stats = client.get("/v1/stats").json()["data"]
            self.assertEqual(stats["cache_entries"], 4)
            self.assertEqual(stats["cache_capacity"], 4)
            self.assertGreater(stats["counters"]["evictions"], 0)


class LifespanTests(ApiTestCase):
    """자원 수명과 readiness 경계입니다."""

    def test_ready_is_false_before_and_after_lifespan(self) -> None:
        app = create_app(build_settings())
        self.assertFalse(app.state.ready)
        with TestClient(app) as client:
            self.assertTrue(app.state.ready)
            self.assertEqual(client.get("/health/ready").status_code, HTTPStatus.OK)
        self.assertFalse(app.state.ready)

    def test_liveness_does_not_require_resources(self) -> None:
        app = create_app(build_settings())
        with TestClient(app) as client:
            response = client.get("/health/live")
            self.assertEqual(response.status_code, HTTPStatus.OK)
            self.assertEqual(response.json()["status"], "alive")

    def test_resources_are_released_after_shutdown(self) -> None:
        app = create_app(build_settings())
        with TestClient(app):
            self.assertTrue(hasattr(app.state, "catalog"))
        self.assertFalse(hasattr(app.state, "catalog"))
        self.assertFalse(hasattr(app.state, "fixture"))

    def test_prepare_failure_cleans_up_and_propagates(self) -> None:
        """부분 초기화 실패에서도 이미 획득한 자원을 정리하고 원래 실패를 전파합니다."""
        released: list[str] = []

        async def failing_prepare(app: FastAPI, stack: AsyncExitStack) -> None:
            app.state.probe = object()
            stack.callback(released.append, "probe")
            stack.callback(delattr, app.state, "probe")
            raise RuntimeError("synthetic prepare failure")

        app = create_app(build_settings(), prepare=failing_prepare)
        with self.assertRaises(RuntimeError):
            with TestClient(app):
                pass
        self.assertEqual(released, ["probe"])
        self.assertFalse(hasattr(app.state, "probe"))
        self.assertFalse(app.state.ready)

    def test_prepare_cancellation_cleans_up(self) -> None:
        """취소도 자원을 정리한 뒤 원래 실패로 전파합니다.

        TestClient portal이 CancelledError를 감싸므로 lifespan을 직접 구동합니다.
        """
        released: list[str] = []

        async def cancelled_prepare(app: FastAPI, stack: AsyncExitStack) -> None:
            stack.callback(released.append, "probe")
            raise asyncio.CancelledError

        app = create_app(build_settings(), prepare=cancelled_prepare)

        async def drive() -> None:
            async with app.router.lifespan_context(app):
                pass

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(drive())
        self.assertEqual(released, ["probe"])
        self.assertFalse(app.state.ready)

    def test_bad_fixture_path_fails_startup(self) -> None:
        """구조·경로 오류는 시작 단계에서 거절합니다. 준비 실패는 ready로 넘어가지 않습니다."""
        settings = build_settings(
            fixture_path=DEFAULT_FIXTURE_PATH.parent / "absent.json"
        )
        app = create_app(settings)

        async def drive() -> None:
            async with app.router.lifespan_context(app):
                pass

        with self.assertRaises(FixtureError):
            asyncio.run(drive())
        self.assertFalse(app.state.ready)
        self.assertFalse(hasattr(app.state, "catalog"))


class AppIsolationTests(ApiTestCase):
    """앱별 상태 격리입니다. 여러 앱이 원본·캐시를 공유하지 않습니다."""

    def test_update_in_one_app_does_not_reach_another(self) -> None:
        settings = build_settings(cache_enabled=True)
        left = create_app(settings)
        right = create_app(settings)
        with TestClient(left) as left_client, TestClient(right) as right_client:
            left_client.patch(f"/v1/themes/{KNOWN_THEME_ID}", json={"spacing_px": 5})
            left_body = left_client.get(f"/v1/themes/{KNOWN_THEME_ID}").json()["data"]
            right_body = right_client.get(f"/v1/themes/{KNOWN_THEME_ID}").json()["data"]
            self.assertEqual(left_body["spacing_px"], 5)
            self.assertEqual(
                payload_tuple(right_body), self.reference.expect(KNOWN_THEME_ID)
            )

    def test_counters_are_per_app(self) -> None:
        settings = build_settings(cache_enabled=True)
        left = create_app(settings)
        right = create_app(settings)
        with TestClient(left) as left_client, TestClient(right) as right_client:
            for _ in range(3):
                left_client.get(f"/v1/themes/{KNOWN_THEME_ID}")
            left_stats = left_client.get("/v1/stats").json()["data"]
            right_stats = right_client.get("/v1/stats").json()["data"]
            self.assertEqual(left_stats["counters"]["cache_lookups"], 3)
            self.assertEqual(right_stats["counters"]["cache_lookups"], 0)

    def test_catalogs_are_distinct_objects(self) -> None:
        settings = build_settings()
        left = create_app(settings)
        right = create_app(settings)
        with TestClient(left), TestClient(right):
            self.assertIsNot(left.state.catalog, right.state.catalog)
            self.assertIsNot(left.state.catalog.store, right.state.catalog.store)


class PrepareResourcesTests(unittest.IsolatedAsyncioTestCase):
    """기본 준비 함수가 자원 획득 직후 정리를 등록하는지 확인합니다."""

    async def test_prepare_registers_cleanup(self) -> None:
        settings = build_settings(cache_enabled=True)
        app = create_app(settings)
        async with AsyncExitStack() as stack:
            await prepare_resources(app, stack, settings=settings)
            self.assertTrue(app.state.catalog.cache_enabled)
        self.assertFalse(hasattr(app.state, "catalog"))
        self.assertFalse(hasattr(app.state, "fixture"))


if __name__ == "__main__":
    unittest.main()
