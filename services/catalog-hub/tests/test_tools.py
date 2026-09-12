"""부하 발생기·메모리 도구의 계약 시험 (Step 1·2).

부하를 실제로 발생시키지 않는 단위 계약만 확인합니다. 측정 자체는 별도 회차입니다.
"""

import unittest

from catalog_hub.tools.cachemem import RSS_LIMIT_BYTES, SOURCE_DB_BYTES, deep_size
from catalog_hub.tools.loadgen import (
    BREAK_P95_MULTIPLE,
    COARSE_STEPS,
    MAX_ERROR_RATE,
    StepOutcome,
    StopCondition,
    build_parser,
    fine_steps,
    percentile_ns,
)


class CoarseStepTests(unittest.TestCase):
    """D5 정정판 계단입니다. 단건 약 124ms → 이론 상한 약 8 RPS."""

    def test_steps_match_corrected_ladder(self) -> None:
        self.assertEqual(COARSE_STEPS, (1, 2, 4, 6, 8, 12))

    def test_steps_are_increasing(self) -> None:
        self.assertEqual(list(COARSE_STEPS), sorted(COARSE_STEPS))

    def test_ladder_brackets_theoretical_limit(self) -> None:
        """이론 상한 8 RPS가 계단 안에 있어야 꺾임을 관측할 수 있습니다."""
        self.assertIn(8, COARSE_STEPS)
        self.assertGreater(max(COARSE_STEPS), 8)


class FineStepTests(unittest.TestCase):
    """세분화는 꺾인 계단과 직전 계단 사이를 0.5 RPS 간격으로 나눕니다."""

    def test_between_six_and_eight(self) -> None:
        self.assertEqual(fine_steps(8, COARSE_STEPS), (6.5, 7.0, 7.5))

    def test_between_four_and_six(self) -> None:
        self.assertEqual(fine_steps(6, COARSE_STEPS), (4.5, 5.0, 5.5))

    def test_first_step_has_no_predecessor(self) -> None:
        self.assertEqual(fine_steps(1, COARSE_STEPS), ())


class StopConditionTests(unittest.TestCase):
    def test_records_reason_when_tripped(self) -> None:
        stop = StopCondition()
        stop.check("error_rate", True, "0.02 > 0.01")
        self.assertTrue(stop.tripped)
        self.assertEqual(len(stop.reasons), 1)

    def test_stays_clear_when_not_tripped(self) -> None:
        stop = StopCondition()
        stop.check("error_rate", False, "fine")
        self.assertFalse(stop.tripped)
        self.assertEqual(stop.reasons, [])

    def test_thresholds_match_management_decision(self) -> None:
        self.assertEqual(MAX_ERROR_RATE, 0.01)
        self.assertEqual(BREAK_P95_MULTIPLE, 2.0)


class StepOutcomeTests(unittest.TestCase):
    def make_outcome(self, **overrides: object) -> StepOutcome:
        defaults: dict[str, object] = {
            "target_rps": 4.0,
            "duration_s": 30.0,
            "scheduled": 120,
            "started": 120,
            "completed": 120,
            "errors": 0,
            "dropped": 0,
            "late_starts": 0,
            "late_ns": [],
            "client_ns": list(range(1_000_000, 1_000_000 + 120)),
            "server_ns": list(range(900_000, 900_000 + 120)),
            "hit_client_ns": [],
            "miss_client_ns": list(range(1_000_000, 1_000_000 + 120)),
            "generator_cpu_s": 1.0,
            "max_rss_bytes": 130_000_000,
        }
        defaults.update(overrides)
        return StepOutcome(**defaults)

    def test_error_rate_uses_started_as_denominator(self) -> None:
        outcome = self.make_outcome(started=100, errors=3)
        self.assertAlmostEqual(outcome.error_rate(), 0.03)

    def test_zero_started_is_not_a_division_error(self) -> None:
        outcome = self.make_outcome(started=0, errors=0)
        self.assertEqual(outcome.error_rate(), 0.0)

    def test_summary_separates_scheduled_started_completed(self) -> None:
        """목표·실제시작·완료·거절·dropped를 구분해야 합니다 (설계서 Step 1).

        `dropped`는 발생기가 계산해 넘기는 저장 필드입니다(scheduled - started).
        요약이 이 다섯 가지를 합치지 않고 따로 싣는지 확인합니다.
        """
        outcome = self.make_outcome(
            scheduled=120, started=118, completed=115, errors=3, dropped=2
        )
        summary = outcome.summary()
        self.assertEqual(summary["scheduled"], 120)
        self.assertEqual(summary["started"], 118)
        self.assertEqual(summary["completed"], 115)
        self.assertEqual(summary["errors"], 3)
        self.assertEqual(summary["dropped"], 2)

    def test_small_subgroup_is_not_given_a_percentile(self) -> None:
        """하위집단 표본이 부족하면 percentile을 내지 않습니다 (판정 규칙 3)."""
        outcome = self.make_outcome(hit_client_ns=[1, 2, 3])
        summary = outcome.summary()
        hit = summary["hit_client_ns"]
        assert isinstance(hit, dict)
        self.assertEqual(hit["sample_count"], 3)
        self.assertNotIn("p95", hit)
        self.assertIn("note", hit)

    def test_large_subgroup_gets_a_percentile(self) -> None:
        outcome = self.make_outcome(hit_client_ns=list(range(100)))
        summary = outcome.summary()
        hit = summary["hit_client_ns"]
        assert isinstance(hit, dict)
        self.assertIn("p95", hit)

    def test_generator_cpu_is_reported_separately(self) -> None:
        """발생기 자원을 서비스와 분리 보고해야 합니다."""
        summary = self.make_outcome(generator_cpu_s=2.5).summary()
        self.assertEqual(summary["generator_cpu_s"], 2.5)


class PercentileTests(unittest.TestCase):
    def test_nearest_rank(self) -> None:
        samples = list(range(1, 101))
        self.assertEqual(percentile_ns(samples, 0.50), 50)
        self.assertEqual(percentile_ns(samples, 0.95), 95)
        self.assertEqual(percentile_ns(samples, 0.99), 99)

    def test_empty_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            percentile_ns([], 0.5)


class LoadgenCliTests(unittest.TestCase):
    def test_dry_run_is_the_default(self) -> None:
        args = build_parser().parse_args([])
        self.assertFalse(args.execute)

    def test_fine_requires_bounds(self) -> None:
        from catalog_hub.tools.loadgen import resolve_steps

        args = build_parser().parse_args(["--fine"])
        with self.assertRaises(SystemExit):
            resolve_steps(args)


class CacheMemoryTests(unittest.TestCase):
    """D3 임계와 근사 측정 함수입니다."""

    def test_limit_matches_management_decision(self) -> None:
        """기준 RSS 124.7 MiB의 2배 = 250 MiB."""
        self.assertEqual(RSS_LIMIT_BYTES, 250 * 1024 * 1024)

    def test_source_db_bytes_matches_measured_size(self) -> None:
        self.assertEqual(SOURCE_DB_BYTES, 25_099_287)

    def test_deep_size_follows_nested_containers(self) -> None:
        shallow = deep_size({"a": 1})
        nested = deep_size({"a": {"b": {"c": "x" * 1000}}})
        self.assertGreater(nested, shallow)

    def test_deep_size_counts_shared_objects_once(self) -> None:
        shared = ["x" * 1000]
        once = deep_size({"a": shared})
        twice = deep_size({"a": shared, "b": shared})
        # 두 번째 참조는 거의 더해지지 않아야 합니다 (dict 항목 비용만).
        self.assertLess(twice - once, 200)

    def test_deep_size_handles_slots_objects(self) -> None:
        from catalog_hub.core.assembled import AssembledCatalog

        catalog = AssembledCatalog(
            release_id=1,
            release_label="rel",
            lang="ko",
            roots=(),
            category_count=0,
            item_count=0,
        )
        self.assertGreater(deep_size(catalog), 0)


if __name__ == "__main__":
    unittest.main()
