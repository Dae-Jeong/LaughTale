import asyncio
import json
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from verify import (
    ACTOR,
    ROOM,
    TOPIC,
    EvidenceError,
    capture_kafka,
    normalize_event,
    reconcile,
    validate_ledger,
)


def uid(number):
    return str(UUID(int=number))


def fixture():
    text = "Synthetic internal message"
    payload = {
        "type": "message.created",
        "event_id": uid(1),
        "schema_version": 1,
        "message": {
            "message_id": uid(1),
            "conversation_id": ROOM,
            "sender_id": ACTOR,
            "client_message_id": uid(2),
            "seq": "5",
            "text": text,
            "created_at": datetime(2026, 9, 8, tzinfo=timezone.utc).isoformat(),
        },
    }
    normalized = normalize_event(payload)
    message = {
        name: normalized[name]
        for name in (
            "message_id",
            "conversation_id",
            "sender_id",
            "client_message_id",
            "seq",
            "text_sha256",
            "created_at",
        )
    }
    outbox = {**normalized, "published": True}
    kafka = {**normalized, "key": ROOM, "partition": 1, "offset": 0}
    ledger = {
        "run_id": "synthetic-verify",
        "complete": True,
        "planned_count": 1,
        "attempts": [
            {
                "attempt_id": 0,
                "client_message_id": uid(2),
                "conversation_id": ROOM,
                "sender_id": ACTOR,
                "text_sha256": sha256(text.encode()).hexdigest(),
                "ack_status": 201,
                "outcome": "acknowledged",
                "response_message_id": uid(1),
                "response_seq": "5",
            }
        ],
    }
    return ledger, [message], [outbox], [kafka], {"complete": True}


class VerifyTests(unittest.TestCase):
    def test_exact_ack_db_outbox_and_kafka_match(self):
        result = reconcile(*fixture())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["counts"]["acknowledged_intents"], 1)
        self.assertNotIn("Synthetic internal message", json.dumps(result))

    def test_lost_message_outbox_or_event_fails(self):
        for index in (1, 2, 3):
            with self.subTest(index=index):
                evidence = fixture()
                evidence[index].clear()
                self.assertEqual(reconcile(*evidence)["status"], "failed")

    def test_duplicate_database_records_fail_but_identical_kafka_duplicates_pass(self):
        for index in (1, 2):
            evidence = fixture()
            evidence[index].append(deepcopy(evidence[index][0]))
            self.assertEqual(reconcile(*evidence)["status"], "failed")
        evidence = fixture()
        evidence[3].append({**evidence[3][0], "offset": 1})
        result = reconcile(*evidence)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["counts"]["duplicate_kafka_events"], 1)

    def test_not_published_is_failure(self):
        evidence = fixture()
        evidence[2][0]["published"] = False
        self.assertEqual(reconcile(*evidence)["status"], "failed")

    def test_wrong_kafka_key_hash_or_identity_fails(self):
        for field, value in (
            ("key", uid(9)),
            ("payload_sha256", "a" * 64),
            ("text_sha256", "b" * 64),
            ("sender_id", uid(10)),
            ("seq", "6"),
        ):
            with self.subTest(field=field):
                evidence = fixture()
                evidence[3][0][field] = value
                self.assertEqual(reconcile(*evidence)["status"], "failed")

    def test_conflicting_kafka_duplicate_cannot_hide_behind_good_copy(self):
        evidence = fixture()
        evidence[3].append({**evidence[3][0], "payload_sha256": "a" * 64, "offset": 1})
        self.assertEqual(reconcile(*evidence)["status"], "failed")

    def test_outbox_payload_must_match_database(self):
        evidence = fixture()
        evidence[2][0]["text_sha256"] = "a" * 64
        evidence[3][0]["text_sha256"] = "a" * 64
        self.assertEqual(reconcile(*evidence)["status"], "failed")

    def test_unknown_absent_and_unknown_stored_are_distinguished(self):
        for stored in (True, False):
            evidence = fixture()
            evidence[0]["attempts"][0].update(
                outcome="unknown",
                ack_status=None,
                response_message_id=None,
                response_seq=None,
            )
            if not stored:
                for index in (1, 2, 3):
                    evidence[index].clear()
            result = reconcile(*evidence)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(
                result["counts"]["unknown_stored" if stored else "unknown_absent"], 1
            )

    def test_unknown_stored_still_requires_outbox_and_kafka(self):
        evidence = fixture()
        evidence[0]["attempts"][0].update(outcome="unknown", ack_status=None)
        evidence[3].clear()
        self.assertEqual(reconcile(*evidence)["status"], "failed")

    def test_partial_ledger_or_capture_is_never_pass(self):
        for index in (0, 4):
            evidence = fixture()
            evidence[index]["complete"] = False
            self.assertEqual(reconcile(*evidence)["status"], "incomplete")

    def test_retry_200_reuses_same_logical_message(self):
        evidence = fixture()
        ledger = evidence[0]
        ledger["attempts"].append(
            {**ledger["attempts"][0], "attempt_id": 1, "ack_status": 200}
        )
        ledger["planned_count"] = 2
        result = reconcile(*evidence)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["counts"]["logical_intents"], 1)

    def test_different_payload_with_same_client_key_is_bad_evidence(self):
        ledger = fixture()[0]
        ledger["attempts"].append(
            {**ledger["attempts"][0], "attempt_id": 1, "text_sha256": "a" * 64}
        )
        with self.assertRaises(EvidenceError):
            validate_ledger(ledger)

    def test_unrelated_historical_kafka_event_is_not_counted(self):
        evidence = fixture()
        evidence[3].append(
            {
                **evidence[3][0],
                "client_message_id": uid(99),
                "event_id": uid(98),
                "message_id": uid(98),
            }
        )
        result = reconcile(*evidence)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["counts"]["relevant_kafka_records"], 1)

    def test_event_parser_is_independent_and_strict(self):
        with self.assertRaises(EvidenceError):
            normalize_event({"schema_version": 1})

    def test_duplicate_seq_with_distinct_message_ids_is_detected(self):
        evidence = fixture()
        second_attempt = {
            **evidence[0]["attempts"][0],
            "attempt_id": 1,
            "client_message_id": uid(30),
            "response_message_id": uid(31),
        }
        evidence[0]["attempts"].append(second_attempt)
        evidence[0]["planned_count"] = 2
        for index in (1, 2, 3):
            second = {
                **evidence[index][0],
                "client_message_id": uid(30),
                "message_id": uid(31),
            }
            if index in (2, 3):
                second["event_id"] = uid(31)
            evidence[index].append(second)
        self.assertEqual(reconcile(*evidence)["issues"]["duplicate_room_seq"], 1)


class TopicPartition(tuple):
    def __new__(cls, topic, partition):
        return super().__new__(cls, (topic, partition))

    @property
    def partition(self):
        return self[1]


class FakeConsumer:
    instance = None
    limit_end = 0
    partition_ids = frozenset({0, 1, 2, 3})

    def __init__(self, **kwargs):
        type(self).instance = self
        self.kwargs, self.positions, self.stopped = kwargs, {}, False
        self.metadata_ready = False

    async def start(self):
        pass

    async def stop(self):
        self.stopped = True

    async def topics(self):
        return {TOPIC}

    def partitions_for_topic(self, topic):
        return self.partition_ids if self.metadata_ready else None

    def assign(self, partitions):
        self.assigned = partitions

    async def beginning_offsets(self, partitions):
        assert self.assigned == partitions
        self.metadata_ready = True
        return {partition: 0 for partition in partitions}

    async def end_offsets(self, partitions):
        return {
            partition: self.limit_end if partition.partition == 0 else 0
            for partition in partitions
        }

    def seek(self, partition, value):
        self.positions[partition] = value

    async def position(self, partition):
        return self.positions[partition]


class ConsumerTests(unittest.TestCase):
    def test_topics_does_not_populate_partition_cache(self):
        consumer = FakeConsumer()
        self.assertIn(TOPIC, asyncio.run(consumer.topics()))
        self.assertIsNone(consumer.partitions_for_topic(TOPIC))
        module = SimpleNamespace(
            AIOKafkaConsumer=FakeConsumer, TopicPartition=TopicPartition
        )
        with patch.dict(sys.modules, {"aiokafka": module}):
            _, capture = asyncio.run(capture_kafka("127.0.0.1:19092", 1, 100))
        self.assertTrue(capture["complete"])

    def test_wrong_partition_count_still_fails_after_refresh(self):
        module = SimpleNamespace(
            AIOKafkaConsumer=FakeConsumer, TopicPartition=TopicPartition
        )
        with (
            patch.dict(sys.modules, {"aiokafka": module}),
            patch.object(FakeConsumer, "partition_ids", {0, 1, 2, 3, 4}),
        ):
            _, capture = asyncio.run(capture_kafka("127.0.0.1:19092", 1, 100))
        self.assertFalse(capture["complete"])
        self.assertEqual(capture["error"], "expected_four_partition_topic_missing")

    def test_manual_assignment_no_group_no_commits_and_captured_highwaters(self):
        module = SimpleNamespace(
            AIOKafkaConsumer=FakeConsumer, TopicPartition=TopicPartition
        )
        with patch.dict(sys.modules, {"aiokafka": module}):
            events, capture = asyncio.run(capture_kafka("127.0.0.1:19092", 1, 100))
        consumer = FakeConsumer.instance
        self.assertFalse(consumer.kwargs["enable_auto_commit"])
        self.assertIsNone(consumer.kwargs["group_id"])
        self.assertEqual(len(consumer.assigned), 4)
        self.assertTrue(consumer.stopped)
        self.assertTrue(capture["complete"])
        self.assertEqual(events, [])

    def test_oversized_snapshot_is_incomplete_without_consuming(self):
        module = SimpleNamespace(
            AIOKafkaConsumer=FakeConsumer, TopicPartition=TopicPartition
        )
        with (
            patch.dict(sys.modules, {"aiokafka": module}),
            patch.object(FakeConsumer, "limit_end", 101),
        ):
            events, capture = asyncio.run(capture_kafka("127.0.0.1:19092", 1, 100))
        self.assertFalse(capture["complete"])
        self.assertEqual(capture["error"], "snapshot_exceeds_event_limit")
        self.assertEqual(events, [])
        self.assertTrue(FakeConsumer.instance.stopped)

    def test_only_records_before_captured_highwater_are_compared(self):
        class OneRecord(FakeConsumer):
            limit_end = 1

            async def getmany(self, **kwargs):
                self.positions[self.assigned[0]] = 2
                payload = {
                    "type": "message.created",
                    "schema_version": 1,
                    "event_id": uid(1),
                    "message": {
                        "message_id": uid(1),
                        "conversation_id": ROOM,
                        "sender_id": ACTOR,
                        "client_message_id": uid(2),
                        "seq": "5",
                        "text": "Synthetic internal message",
                        "created_at": "2026-09-08T00:00:00+00:00",
                    },
                }
                return {
                    self.assigned[0]: [
                        SimpleNamespace(
                            partition=0,
                            offset=offset,
                            key=ROOM.encode(),
                            value=json.dumps(payload).encode(),
                        )
                        for offset in (0, 1)
                    ]
                }

        module = SimpleNamespace(
            AIOKafkaConsumer=OneRecord, TopicPartition=TopicPartition
        )
        with patch.dict(sys.modules, {"aiokafka": module}):
            events, capture = asyncio.run(capture_kafka("127.0.0.1:19092", 1, 100))
        self.assertTrue(capture["complete"])
        self.assertEqual(capture["scanned_records"], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["offset"], 0)
        self.assertNotIn("text", events[0])


if __name__ == "__main__":
    unittest.main()
