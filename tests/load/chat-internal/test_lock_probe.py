import json
import unittest
from unittest.mock import patch

from lock_probe import SQL, cpu_stat, guard, pods, sql, validate_options


class ProbeTests(unittest.TestCase):
    def test_cpu_stat_is_numeric_snapshot(self):
        with patch(
            "lock_probe.command", return_value="usage_usec 10\nnr_throttled 2\n"
        ):
            self.assertEqual(
                cpu_stat("test-pod"), {"usage_usec": 10, "nr_throttled": 2}
            )

    def test_profile_budget(self):
        for rate in (10, 25, 50, 100):
            validate_options(rate, 60, 1)
            validate_options(rate, 60, 10)
        for args in ((1000, 60, 1), (100, 100, 1), (10, 0, 1), (10, 60, 100)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_options(*args)

    def test_oom_prevents_load(self):
        state = [{"State": {"Running": True, "OOMKilled": True}}]
        with (
            patch("lock_probe.command", return_value=json.dumps(state)),
            self.assertRaisesRegex(ValueError, "node_stopped_or_oom"),
        ):
            guard({})

    def test_empty_pods_fail_closed(self):
        with (
            patch("lock_probe.command", return_value='{"items":[]}'),
            self.assertRaisesRegex(ValueError, "no_pods"),
        ):
            pods()

    def test_query_uses_timeout_and_no_source_query_column(self):
        with patch("lock_probe.command", return_value="[]") as run:
            self.assertEqual(sql(SQL), [])
            self.assertIn("SET statement_timeout='2s'", run.call_args.kwargs["input"])
        self.assertIn("pg_blocking_pids(pid)", SQL)
        self.assertNotIn("SELECT query", SQL)


if __name__ == "__main__":
    unittest.main()
