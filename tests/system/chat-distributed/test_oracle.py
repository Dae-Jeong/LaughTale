import asyncio
import unittest
from copy import deepcopy
from hashlib import sha256
from unittest.mock import AsyncMock

from oracle import ACTOR, CHECKS, ROOM, EvidenceError, compare, normalize
from runner import event_message, history, require_status, wait_receipts


def message():
    return {
        "message_id": "00000000-0000-4000-8000-000000000101",
        "conversation_id": ROOM,
        "sender_id": ACTOR,
        "client_message_id": "00000000-0000-4000-8000-000000000102",
        "seq": "1",
        "text": "synthetic",
    }


def evidence():
    row = normalize(message())
    intent = {
        key: row[key]
        for key in ("client_message_id", "conversation_id", "sender_id", "text_sha256")
    }
    return {
        "complete": True,
        "plan": [intent],
        "attempts": [{**intent, "outcome": "acknowledged", "message": row}],
        "expected_peers": 2,
        "peers": [
            {"id": str(index), "received": [deepcopy(row)], "history_recovered": []}
            for index in range(2)
        ],
        "checks": dict.fromkeys(CHECKS, True),
    }


class OracleTests(unittest.TestCase):
    def test_late_ws_overlap_does_not_prove_history_dependency(self):
        data = evidence()
        data["scenario"] = "reconnect"
        peer = data["peers"][1]
        peer["offline_client_ids"] = [data["plan"][0]["client_message_id"]]
        peer["history_recovered"] = deepcopy(peer["received"])
        result = compare(data)
        self.assertEqual(result["counts"]["history_rows"], 1)
        self.assertEqual(result["counts"]["history_recovered"], 0)
        self.assertEqual(result["counts"]["history_overlap_ws"], 1)
        self.assertEqual(
            result["issues"]["offline_batch_not_exclusively_history_recovered"], 1
        )

    def test_receipt_timeout_raises(self):
        data = evidence()
        for peer in data["peers"]:
            peer["offline_client_ids"] = []
        data["peers"][1]["received"] = []
        with self.assertRaisesRegex(EvidenceError, "live_receipt_timeout"):
            asyncio.run(wait_receipts(data, timeout=0.001))

    def test_reused_peer_evidence_fails(self):
        data = evidence()
        data["peers"][1]["id"] = data["peers"][0]["id"]
        self.assertEqual(compare(data)["issues"]["duplicate_peer_evidence"], 1)

    def test_normal(self):
        result = compare(evidence())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["counts"]["ws_received"], 2)

    def test_missing_ws_not_hidden_by_history(self):
        data = evidence()
        peer = data["peers"][1]
        peer["history_recovered"], peer["received"] = peer["received"], []
        self.assertEqual(compare(data)["issues"]["missing_live_receipt"], 1)

    def test_offline_history_is_separate(self):
        data = evidence()
        peer = data["peers"][1]
        peer["offline_client_ids"] = [data["plan"][0]["client_message_id"]]
        peer["history_recovered"], peer["received"] = peer["received"], []
        result = compare(data)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["counts"]["history_recovered"], 1)

    def test_missing_both_transports(self):
        data = evidence()
        data["peers"][1]["received"] = []
        self.assertEqual(compare(data)["issues"]["missing_receipt"], 1)

    def test_body_room_sender_seq_mismatch(self):
        for field, replacement in (
            ("text_sha256", "0" * 64),
            ("conversation_id", ACTOR),
            ("sender_id", ROOM),
            ("seq", "2"),
        ):
            with self.subTest(field=field):
                data = evidence()
                data["peers"][1]["received"][0][field] = replacement
                self.assertEqual(compare(data)["issues"]["ws_payload_mismatch"], 1)

    def test_duplicate_ws_is_not_double_counted_as_success(self):
        data = evidence()
        data["peers"][1]["received"] *= 2
        self.assertEqual(compare(data)["issues"]["duplicate_ws_delivery"], 1)

    def test_missing_or_failed_auth_check(self):
        for value in (False, None):
            data = evidence()
            data["checks"]["shared_session"] = value
            self.assertEqual(compare(data)["issues"]["check_shared_session"], 1)
        data = evidence()
        data["checks"] = {}
        self.assertEqual(compare(data)["status"], "fail")

    def test_unknown_and_incomplete_fail(self):
        data = evidence()
        data["attempts"][0]["outcome"] = "unknown"
        data["complete"] = False
        issues = compare(data)["issues"]
        self.assertEqual(issues["request_unknown"], 1)
        self.assertEqual(issues["incomplete"], 1)

    def test_malformed_event_rejected(self):
        with self.assertRaises(EvidenceError):
            event_message(
                {
                    "type": "message.created",
                    "schema_version": 1,
                    "event_id": ROOM,
                    "message": message(),
                }
            )

    def test_normalization_contains_hash_not_text(self):
        row = normalize(message())
        self.assertNotIn("text", row)
        self.assertEqual(row["text_sha256"], sha256(b"synthetic").hexdigest())

    def test_403_never_accepted_as_authenticated(self):
        with self.assertRaises(EvidenceError):
            require_status(403, 200)

    def test_history_sequence_hole_fails(self):
        http = AsyncMock()
        http.request.return_value = (
            200,
            {
                "data": [message()],
                "meta": {
                    "has_more": False,
                    "next_cursor": "1",
                    "snapshot_head_seq": "2",
                },
            },
            None,
        )
        with self.assertRaises(EvidenceError):
            asyncio.run(history(http, "not-a-real-cookie", "0"))

    def test_history_snapshot_changes_fail(self):
        http = AsyncMock()
        http.request.side_effect = [
            (
                200,
                {
                    "data": [message()],
                    "meta": {
                        "has_more": True,
                        "next_cursor": "1",
                        "snapshot_head_seq": "2",
                    },
                },
                None,
            ),
            (
                200,
                {
                    "data": [],
                    "meta": {
                        "has_more": False,
                        "next_cursor": "1",
                        "snapshot_head_seq": "3",
                    },
                },
                None,
            ),
        ]
        with self.assertRaises(EvidenceError):
            asyncio.run(history(http, "not-a-real-cookie", "0"))


if __name__ == "__main__":
    unittest.main()
