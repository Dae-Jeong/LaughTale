"""Offline safety and correctness tests; never connect to PostgreSQL."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("index_compare", Path(__file__).resolve().parents[1] / "experiments/index_compare.py")
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)


class IndexExperimentTests(unittest.TestCase):
    def test_reject_other_services_databases_and_unsafe_parameters(self):
        for host, port, db, owner, limit in [
            ("db.example.com", 5434, experiment.DATABASE, 3, 20),
            ("127.0.0.1", 5433, experiment.DATABASE, 3, 20),
            ("127.0.0.1", 5434, "thready", 3, 20),
            ("127.0.0.1", 5434, "postgresql://secret@localhost/db", 3, 20),
            ("127.0.0.1", 5434, experiment.DATABASE, "3;DROP TABLE x", 20),
            ("127.0.0.1", 5434, experiment.DATABASE, 3, 100000),
        ]:
            with self.subTest(host=host, port=port, db=db), self.assertRaises(ValueError):
                experiment.validate(host, port, db, owner, limit)

    def test_connection_cannot_be_redirected_by_ambient_libpq_variables(self):
        with patch.dict(os.environ, {"PGHOSTADDR": "10.0.0.1", "PGSERVICE": "production",
                                     "PGOPTIONS": "-c search_path=public", "PGPASSWORD": "test-only"}):
            env = experiment.connection_env("localhost")
        self.assertEqual(env["PGHOSTADDR"], "127.0.0.1")
        self.assertNotIn("PGSERVICE", env)
        self.assertNotIn("PGOPTIONS", env)
        self.assertEqual(env["PGPASSWORD"], "test-only")

    def test_default_is_offline_and_rejects_unsafe_input_before_subprocess(self):
        with patch.object(experiment.subprocess, "run") as run:
            experiment.main([])
            with self.assertRaises(ValueError):
                experiment.main(["--run", "--database", "postgres"])
            run.assert_not_called()

    def test_order_duplicates_and_missing_rows_are_not_set_equality(self):
        experiment.assert_ids([8, 3, 1], [8, 3, 1])
        for wrong in ([3, 8, 1], [8, 3], [8, 3, 3]):
            with self.assertRaises(ValueError):
                experiment.assert_ids(wrong, [8, 3, 1])

    def test_oracle_ties_and_indexed_update(self):
        # IDs 4 and 9974 have the same owner and timestamp; DESC id breaks ties.
        ids = experiment.expected_ids(10000, 3, 1000)
        self.assertLess(ids.index(9974), ids.index(4))
        self.assertEqual(experiment.expected_ids(10000, 3, 20, updated=True)[0], 4)
        self.assertNotEqual(experiment.expected_ids(10000, 3, 20)[0], 4)

    def test_incomplete_run_cannot_publish_success(self):
        with self.assertRaises(ValueError):
            experiment.check_records([], 3, 20)
        records = []
        for n in experiment.SIZES:
            for variant in experiment.VARIANTS:
                for stage in ("before", "after"):
                    records.append(dict(rows=n, variant=variant, kind="ids", stage=stage,
                                        ids=experiment.expected_ids(n, 3, 20, stage == "after")))
                records.extend(dict(rows=n, variant=variant, kind="read") for _ in range(5))
        with self.assertRaises(ValueError):
            experiment.check_records(records, 3, 20)
        records.append(dict(kind="cleanup", schema_absent=True))
        experiment.check_records(records, 3, 20)
        records[0]["ids"].reverse()
        with self.assertRaises(ValueError):
            experiment.check_records(records, 3, 20)


if __name__ == "__main__":
    unittest.main()
