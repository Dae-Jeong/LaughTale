import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from ledger import Ledger, digest, percentile, resource_failure


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "ledger.sqlite"
        self.ledger = Ledger(self.path, str(uuid4()), 1)
        self.cid = self.ledger.db.execute("SELECT cid FROM attempts").fetchone()[0]
        self.mid = str(uuid4())
        self.ledger.db.execute(
            "UPDATE attempts SET state='ack',mid=?,seq=1,sent=1,ack=1.1,dispatch_delay=.001",
            (self.mid,),
        )
        self.message = {
            "client_message_id": self.cid,
            "message_id": self.mid,
            "seq": 1,
            "text": "sample",
        }
        for channel in ("primary", "replica", "kafka", "ws"):
            self.ledger.observe(channel, self.message, 1.2)

    def tearDown(self):
        self.ledger.close()
        self.folder.cleanup()

    def test_complete_evidence_passes(self):
        self.assertTrue(all(self.ledger.report(digest("sample"))["checks"].values()))

    def test_http_failure_is_reported_without_response_body(self):
        self.ledger.db.execute("UPDATE attempts SET state='unknown',code=503")
        self.assertEqual(
            self.ledger.report(digest("sample"))["http_status_counts"], {"503": 1}
        )

    def test_missing_receipt_fails(self):
        self.ledger.db.execute("DELETE FROM observed WHERE channel='ws'")
        self.assertFalse(self.ledger.report(digest("sample"))["checks"]["ws"])

    def test_corrupt_message_fails(self):
        self.ledger.observe("replica", self.message | {"text": "wrong"}, 1.3)
        self.assertFalse(self.ledger.report(digest("sample"))["checks"]["replica"])

    def test_duplicate_ws_fails(self):
        self.ledger.observe("ws", self.message, 1.3)
        self.assertFalse(
            self.ledger.report(digest("sample"))["checks"]["ws_no_duplicates"]
        )

    def test_identical_kafka_redelivery_is_counted_not_logical_corruption(self):
        self.ledger.observe("kafka", self.message, 1.3)
        result = self.ledger.report(digest("sample"))
        self.assertTrue(result["checks"]["kafka"])
        self.assertEqual(result["kafka_raw_duplicates"], 1)

    def test_rejected_but_committed_fails(self):
        self.ledger.db.execute("UPDATE attempts SET state='rejected'")
        self.assertFalse(
            self.ledger.report(digest("sample"))["checks"]["no_rejected_commits"]
        )

    def test_unknown_commit_is_separate(self):
        self.ledger.db.execute("UPDATE attempts SET state='unknown'")
        self.assertEqual(self.ledger.report(digest("sample"))["unknown_committed"], 1)

    def test_existing_evidence_cannot_be_overwritten(self):
        with self.assertRaises(ValueError):
            Ledger(self.path, str(uuid4()), 1)

    def test_unrelated_event_is_ignored(self):
        self.assertFalse(
            self.ledger.observe(
                "kafka", self.message | {"client_message_id": str(uuid4())}, 1.3
            )
        )

    def test_percentiles(self):
        self.assertEqual(percentile([3, 1, 2], 0.99), 3)
        self.assertIsNone(percentile([], 0.99))


class GuardTests(unittest.TestCase):
    def test_floor_and_missing_values(self):
        normal = {
            "host_free_percent": 50,
            "internal_free_gib": 30,
            "external_free_gib": 1700,
            "guest_free_gib": 1200,
            "healthy": True,
        }
        self.assertIsNone(resource_failure(normal))
        for key, value in (
            ("host_free_percent", 14),
            ("internal_free_gib", 14),
            ("external_free_gib", 199),
            ("guest_free_gib", 99),
            ("healthy", False),
            ("host_free_percent", float("nan")),
        ):
            with self.subTest(key=key, value=value):
                self.assertIsNotNone(resource_failure(normal | {key: value}))
        self.assertEqual(resource_failure({}), "missing_resource_evidence")


if __name__ == "__main__":
    unittest.main()
