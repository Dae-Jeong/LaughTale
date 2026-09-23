import unittest

from calibrate import GIB, projection, sql_script


class CapacityTests(unittest.TestCase):
    def test_counts_both_postgres_copies(self):
        result = projection(
            [
                {
                    "rows": 100,
                    "message_bytes": 10000,
                    "outbox_bytes": 20000,
                    "event_bytes": 5000,
                }
            ],
            100 * GIB,
            100 * GIB,
        )
        self.assertEqual(result["components_bytes"]["primary_relations"], 3_000_000_000)
        self.assertEqual(result["components_bytes"]["replica_relations"], 3_000_000_000)

    def test_uses_worst_sample_not_optimistic_last(self):
        samples = [
            {
                "rows": 100,
                "message_bytes": 10000,
                "outbox_bytes": 20000,
                "event_bytes": 5000,
            },
            {
                "rows": 1000,
                "message_bytes": 10000,
                "outbox_bytes": 20000,
                "event_bytes": 5000,
            },
        ]
        self.assertEqual(
            projection(samples, 100 * GIB, 100 * GIB)[
                "postgres_bytes_per_message_and_outbox"
            ],
            300,
        )

    def test_host_and_vm_reserves_block(self):
        sample = [
            {
                "rows": 1,
                "message_bytes": 1000,
                "outbox_bytes": 1000,
                "event_bytes": 1000,
            }
        ]
        for host, vm in [
            (14 * GIB, 100 * GIB),
            (100 * GIB, GIB),
            (37 * GIB, 100 * GIB),
        ]:
            self.assertEqual(
                projection(sample, host, vm)["decision"], "storage_blocked"
            )

    def test_empty_is_not_pass(self):
        with self.assertRaises(ValueError):
            projection([], 100 * GIB, 100 * GIB)

    def test_script_only_inserts_into_temp_relations(self):
        script = sql_script("capacity_" + "a" * 32)
        inserts = [
            line for line in script.splitlines() if line.startswith("INSERT INTO")
        ]
        self.assertEqual(len(inserts), 20)
        self.assertTrue(all("pg_temp.capacity_" in line for line in inserts))
        self.assertTrue(script.endswith("ROLLBACK;"))
        self.assertIn("ON COMMIT DROP", script)
        self.assertNotIn("TRUNCATE", script)
        self.assertNotIn("DELETE FROM", script)

    def test_sql_rejects_untrusted_application_name(self):
        with self.assertRaises(ValueError):
            sql_script("'; DROP SCHEMA chat; --")


if __name__ == "__main__":
    unittest.main()
