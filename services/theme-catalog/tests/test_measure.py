"""측정 도구 계약 시험 (LAUGH-KNOWLEDGE-READ-001).

계수·표본·percentile 산출 방식과 CLI help/dry-run 경계를 확인합니다.
이 시험은 성능 수치를 판정하지 않습니다.
"""

import io
import json
import unittest
from contextlib import redirect_stdout

from oracle import ReferenceThemes, as_tuple

from theme_catalog.core.catalog import build_catalog
from theme_catalog.core.fixture import DEFAULT_FIXTURE_PATH, load_fixture
from theme_catalog.tools.measure import (
    build_parser,
    dry_run_plan,
    key_sequence,
    main,
    max_rss_bytes,
    run_reads,
)


class MeasureTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = load_fixture(DEFAULT_FIXTURE_PATH)
        self.reference = ReferenceThemes()
        self.theme_ids = tuple(theme.theme_id for theme in self.fixture.themes)


class RunnerTests(MeasureTestCase):
    async def test_timed_run_collects_one_sample_per_read(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        keys = key_sequence(self.theme_ids, pattern="cycle32", count=64)
        result = await run_reads(catalog, keys, timed=True)
        self.assertEqual(result.sample_count, 64)
        self.assertEqual(len(result.samples_ns), 64)
        self.assertTrue(all(sample >= 0 for sample in result.samples_ns))
        self.assertGreater(result.wall_ns, 0)
        self.assertGreater(result.max_rss_bytes, 0)
        self.assertIsNone(result.traced_peak_bytes)

    async def test_batch_run_collects_no_samples(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        keys = key_sequence(self.theme_ids, pattern="cycle32", count=64)
        result = await run_reads(catalog, keys, timed=False)
        self.assertEqual(result.samples_ns, ())
        self.assertEqual(result.sample_count, 64)
        summary = result.summary()
        self.assertNotIn("p95_ns", summary)
        self.assertIsNotNone(summary["batch_mean_wall_ns"])

    async def test_percentile_uses_nearest_rank(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=False)
        keys = key_sequence(self.theme_ids, pattern="cycle32", count=100)
        result = await run_reads(catalog, keys, timed=True)
        ordered = sorted(result.samples_ns)
        self.assertEqual(result.percentile_ns(0.50), ordered[49])
        self.assertEqual(result.percentile_ns(0.95), ordered[94])

    async def test_trace_memory_run_reports_peak(self) -> None:
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        keys = key_sequence(self.theme_ids, pattern="hot4", count=32)
        result = await run_reads(catalog, keys, timed=False, trace_memory=True)
        self.assertIsNotNone(result.traced_peak_bytes)
        assert result.traced_peak_bytes is not None
        self.assertGreater(result.traced_peak_bytes, 0)

    async def test_update_every_run_stays_consistent_with_origin(self) -> None:
        """갱신 섞인 회차에서도 최종 값이 원본과 어긋나지 않습니다."""
        catalog = build_catalog(self.fixture.themes, cache_enabled=True, capacity=16)
        keys = key_sequence(self.theme_ids, pattern="cycle32", count=400)
        result = await run_reads(catalog, keys, timed=False, update_every=100)
        self.assertEqual(result.counters["updates"], 4)
        for theme_id in self.theme_ids:
            theme = await catalog.get(theme_id)
            self.assertEqual(
                as_tuple(theme), as_tuple(catalog.store.read_only_view()[theme_id])
            )

    def test_key_sequence_patterns_differ(self) -> None:
        hot = key_sequence(self.theme_ids, pattern="hot4", count=40)
        cycle = key_sequence(self.theme_ids, pattern="cycle32", count=40)
        self.assertEqual(len(set(hot)), 4)
        self.assertEqual(len(set(cycle)), 32)

    def test_key_sequence_rejects_unknown_pattern(self) -> None:
        with self.assertRaises(ValueError):
            key_sequence(self.theme_ids, pattern="zipf", count=10)

    def test_max_rss_is_positive(self) -> None:
        self.assertGreater(max_rss_bytes(), 0)


class CliTests(MeasureTestCase):
    def test_help_exits_cleanly(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()):
                parser.parse_args(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_dry_run_is_the_default(self) -> None:
        args = build_parser().parse_args([])
        self.assertFalse(args.execute)

    def test_dry_run_plan_reports_conditions(self) -> None:
        args = build_parser().parse_args(["--cache", "on", "--key-pattern", "hot4"])
        plan = dry_run_plan(args, self.fixture)
        self.assertEqual(plan["mode"], "dry-run")
        self.assertEqual(plan["cache"], "on")
        self.assertEqual(plan["distinct_keys"], 4)
        self.assertEqual(plan["theme_count"], 32)
        self.assertEqual(plan["external_calls"], 0)

    def test_main_dry_run_prints_json_and_returns_zero(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["mode"], "dry-run")

    def test_main_rejects_missing_fixture(self) -> None:
        code = main(["--fixture", str(DEFAULT_FIXTURE_PATH.parent / "absent.json")])
        self.assertEqual(code, 2)

    def test_main_execute_small_run(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--execute", "--reads", "64", "--timing", "batch"])
        self.assertEqual(code, 0)
        summary = json.loads(buffer.getvalue())
        self.assertEqual(summary["sample_count"], 64)
        self.assertFalse(summary["cache_enabled"])


if __name__ == "__main__":
    unittest.main()
