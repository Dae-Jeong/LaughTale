import unittest
from unittest.mock import patch

from resources import SampleError, containers, memory, safe_metrics, stop_reasons, swap


class ResourceTests(unittest.TestCase):
    def test_memory_does_not_apply_pressure(self):
        with patch(
            "resources.command", return_value="System-wide memory free percentage: 42%"
        ) as command:
            self.assertEqual(memory(1), {"free_percent": 42})
            self.assertEqual(command.call_args.args[0], ["/usr/bin/memory_pressure"])

    def test_swap_keeps_numeric_data_only(self):
        with patch(
            "resources.command",
            return_value="total = 1024.00M used = 3.00M free = 1021.00M (encrypted)",
        ):
            self.assertEqual(swap(1)["used_bytes"], 3 * 1024**2)

    def test_only_exact_lab_containers_are_observed(self):
        raw = "/laughtale-postgres-lab-primary-1|true|false|0|laughtale-postgres-lab|primary\n/laughtale-postgres-lab-replica-1|true|false|0|laughtale-postgres-lab|replica"
        with patch("resources.command", return_value=raw) as command:
            self.assertEqual(len(containers(1)), 2)
            self.assertNotIn("thready-postgres", command.call_args.args[0])
        with (
            patch(
                "resources.command",
                return_value=raw.replace("laughtale-postgres-lab|", "other|"),
            ),
            self.assertRaises(SampleError),
        ):
            containers(1)

    def test_metric_labels_cannot_leak_tokens_or_customer_routes(self):
        raw = '# HELP x secret-help\nhttp_requests_total{route="/secret-customer"} 1\ndb_pool_connections_in_use{role="primary"} 2\ndb_sessions_active{token="secret-token"} 1\ndb_transaction_duration_seconds_bucket{le="+Inf",outcome="committed",role="primary"} 4\n'
        text, metadata = safe_metrics(raw)
        self.assertNotIn("secret", text)
        self.assertIn("db_pool_connections_in_use", text)
        self.assertFalse(metadata["event_loop_metric_observed"])
        self.assertEqual(metadata["discarded_unscoped_series"], 2)

    def test_unparseable_or_missing_scoped_metrics_is_incomplete(self):
        for raw in ("invalid text", "http_requests_total 1"):
            with self.assertRaises(SampleError):
                safe_metrics(raw)

    def test_stop_criteria_are_targeted(self):
        self.assertIn(
            "host_memory_below_10_percent",
            stop_reasons({"memory": {"free_percent": 9}, "errors": {}}, None),
        )
        self.assertEqual(
            stop_reasons({"memory": {"free_percent": 10}, "errors": {}}, None), []
        )
        self.assertIn(
            "target_missing_or_unobservable",
            stop_reasons(
                {"errors": {"processes": "app_missing_or_unobservable_18082"}}, None
            ),
        )
        self.assertIn(
            "target_container_stopped_or_oom",
            stop_reasons(
                {"containers": [{"running": True, "oom_killed": True}], "errors": {}},
                None,
            ),
        )

    def test_pid_change_is_not_silently_rebound(self):
        before = {"processes": [{"port": 18082, "pid": 1}], "errors": {}}
        after = {"processes": [{"port": 18082, "pid": 2}], "errors": {}}
        self.assertIn("target_process_changed", stop_reasons(after, before))


if __name__ == "__main__":
    unittest.main()
