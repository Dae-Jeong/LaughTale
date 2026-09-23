import asyncio
import struct
import time
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fanout_growth import (
    GROUP,
    TOPIC,
    assignment,
    group_description,
    scale_once,
    schedule,
)
from fanout_oracle import compare, role_transition
from oracle import ACTOR, ROOM, EvidenceError, normalize
from test_growth import fixture as growth_fixture
from test_growth import pod, stamp

OLD = "00000000-0000-4000-8000-000000000500"
NEW = "00000000-0000-4000-8000-000000000501"


def kafka(when, members, end, committed):
    return {
        "observed_at": stamp(when),
        "state": "Stable",
        "group": GROUP,
        "members": [
            {"pod_uid": uid, "partitions": partitions} for uid, partitions in members
        ],
        "offsets": [
            {
                "partition": p,
                "committed": committed if p == 0 else 0,
                "end": end if p == 0 else 0,
                "lag": end - committed if p == 0 else 0,
            }
            for p in range(4)
        ],
    }


def fixture():
    initial = deepcopy(growth_fixture()["samples"][0])
    initial["pods"] = [row for row in initial["pods"] if row["uid"] != "fan-b"]
    next(row for row in initial["pods"] if row["app"] == "chat-fanout")["uid"] = OLD
    initial["pods"].append(pod("second-api"))
    final = deepcopy(initial)
    final["observed_at"] = stamp(40)
    final["pods"].append(pod(NEW, "chat-fanout", ready_since=stamp(13)))
    before = {
        "uid": "fanout-deployment",
        "replicas": 1,
        "ready": 1,
        "resource_version": "100",
    }
    result = {
        "complete": True,
        "monitoring_completed": True,
        "kafka_monitoring_completed": True,
        "session_entry_verified": True,
        "initial_deployment": before,
        "traffic_started_at": stamp(0),
        "samples": [initial, final],
        "kafka": [
            kafka(0, [(OLD, [0, 1, 2, 3])], 100, 100),
            kafka(14, [(OLD, [0, 1]), (NEW, [2, 3])], 130, 125),
            kafka(38, [(OLD, [0, 1]), (NEW, [2, 3])], 310, 310),
        ],
        "scale": {
            "success": True,
            "writes": 1,
            "before": before,
            "deployment_uid": "fanout-deployment",
            "resource_version_before": "100",
            "requested_at": stamp(10),
            "completed_at": stamp(10.1),
            "rollout_ready_observed_at": stamp(13.5),
        },
        "plan": [],
        "attempts": [],
        "peers": [],
    }
    messages = []
    for index, (when, rate) in enumerate(schedule()):
        message = normalize(
            {
                "message_id": f"00000000-0000-4000-8000-{index + 1000:012d}",
                "conversation_id": ROOM,
                "sender_id": ACTOR,
                "client_message_id": f"00000000-0000-4000-8000-{index + 2000:012d}",
                "seq": str(index + 1),
                "text": "synthetic",
            }
        )
        intent = {
            key: message[key]
            for key in (
                "client_message_id",
                "conversation_id",
                "sender_id",
                "text_sha256",
            )
        }
        intent.update(scheduled_seconds=when, stage_rps=rate)
        result["plan"].append(intent)
        result["attempts"].append(
            {
                **intent,
                "message": message,
                "outcome": "acknowledged",
                "started_at": stamp(when),
                "completed_at": stamp(when + 0.01),
                "generator_lag_seconds": 0,
                "latency_seconds": 0.01,
            }
        )
        messages.append(message)
    result["peers"] = [
        {
            "id": str(index),
            "target": "gateway_a" if index == 0 else "gateway_b",
            "received": deepcopy(messages),
            "alive_after_traffic": True,
        }
        for index in range(2)
    ]
    return result


class FanoutOracleTests(unittest.TestCase):
    def test_final_unstable_or_stale_snapshot_fails(self):
        data = fixture()
        data["kafka"][-1]["state"] = "PreparingRebalance"
        self.assertIn("stable_boundary_evidence_missing", compare(data)["issues"])
        data = fixture()
        data["kafka"][-1]["observed_at"] = stamp(33)
        self.assertIn(
            "final_kafka_sample_predates_workload_end", compare(data)["issues"]
        )

    def test_new_member_participates_without_hot_partition_claim(self):
        result = compare(fixture())
        self.assertEqual(result["status"], "pass", result)
        self.assertFalse(result["new_consumer_owns_hot_partition_at_end"])
        self.assertEqual(result["counts"]["ws_receipts"], 420)

    def test_new_member_must_map_to_new_pod(self):
        data = fixture()
        data["kafka"][-1]["members"][1]["pod_uid"] = "unobserved-pod"
        self.assertIn("kafka_member_pod_identity_invalid", compare(data)["issues"])

    def test_duplicate_or_missing_partition_assignment_fails(self):
        for partitions in ([0, 3], [2]):
            data = fixture()
            data["kafka"][-1]["members"][1]["partitions"] = partitions
            self.assertIn("invalid_kafka_assignment", compare(data)["issues"])

    def test_no_rebalance_fails(self):
        data = fixture()
        for sample in data["kafka"]:
            sample["members"] = [{"pod_uid": OLD, "partitions": [0, 1, 2, 3]}]
        self.assertIn("assignment_did_not_change", compare(data)["issues"])

    def test_lag_or_commit_regression_fails(self):
        data = fixture()
        data["kafka"][-1]["offsets"][0].update(committed=309, lag=1)
        self.assertIn("initial_or_final_lag_not_zero", compare(data)["issues"])
        data = fixture()
        data["kafka"][1]["offsets"][0].update(committed=99, lag=31)
        self.assertIn("commit_regressed", compare(data)["issues"])

    def test_fake_zero_lag_fails(self):
        data = fixture()
        data["kafka"][-1]["offsets"][0]["committed"] = 200
        self.assertIn("invalid_commit_bounds", compare(data)["issues"])

    def test_wrong_role_or_restarted_baseline_fails(self):
        data = fixture()
        baseline, final = data["samples"]
        self.assertEqual(role_transition(baseline, final, "chat-fanout", True), [])
        self.assertIn(
            "unapproved_pod_added",
            role_transition(baseline, final, "chat-gateway", True),
        )
        final["pods"][0]["restarts"] = 1
        self.assertIn("pod_restart_or_termination", compare(data)["issues"])

    def test_missing_or_modified_ws_fails(self):
        data = fixture()
        data["peers"][1]["received"].pop()
        self.assertEqual(compare(data)["issues"]["missing_ws"], 1)
        data = fixture()
        data["peers"][0]["received"][0]["text_sha256"] = "0" * 64
        self.assertIn("ws_payload_mismatch", compare(data)["issues"])

    def test_ramp_continuity_lag_and_gap_budget(self):
        data = fixture()
        self.assertEqual(len(data["plan"]), 210)
        data["attempts"][9]["generator_lag_seconds"] = 1.1
        self.assertIn("generator_lag_exceeded", compare(data)["issues"])
        data = fixture()
        data["attempts"][10]["started_at"] = stamp(12.1)
        self.assertIn("planned_gap_budget_exceeded", compare(data)["issues"])


def encoded_assignment(partitions):
    topic = TOPIC.encode()
    return (
        struct.pack(">hih", 0, 1, len(topic))
        + topic
        + struct.pack(">i", len(partitions))
        + b"".join(struct.pack(">i", partition) for partition in partitions)
        + struct.pack(">i", -1)
    )


class KafkaWireTests(unittest.TestCase):
    def test_assignment_and_member_identity(self):
        raw = encoded_assignment([0, 1])
        self.assertEqual(assignment(raw), [0, 1])
        response = SimpleNamespace(
            groups=[
                (
                    0,
                    GROUP,
                    "Stable",
                    "consumer",
                    "roundrobin",
                    [("member", "chat-fanout-" + NEW, "/lab", b"", raw)],
                )
            ]
        )
        state, members = group_description([response])
        self.assertEqual(state, "Stable")
        self.assertEqual(members[0]["pod_uid"], NEW)
        self.assertNotIn("client_host", members[0])

    def test_duplicate_partition_and_default_client_id_rejected(self):
        with self.assertRaises(EvidenceError):
            assignment(encoded_assignment([0, 0]))
        response = SimpleNamespace(
            groups=[
                (
                    0,
                    GROUP,
                    "Stable",
                    "consumer",
                    "roundrobin",
                    [("member", "aiokafka-0.14.0", "/lab", b"", b"")],
                )
            ]
        )
        with self.assertRaises(EvidenceError):
            group_description([response])


class FanoutScaleTests(unittest.TestCase):
    def test_exact_one_fanout_only_preconditioned_scale(self):
        before = {
            "uid": "fanout-deployment",
            "replicas": 1,
            "ready": 1,
            "resource_version": "100",
        }
        after = {
            "uid": "fanout-deployment",
            "replicas": 2,
            "ready": 2,
            "generation": 2,
            "observed_generation": 2,
        }
        command = AsyncMock(return_value="scaled")
        with (
            TemporaryDirectory() as directory,
            patch("fanout_growth.deployment", AsyncMock(side_effect=[before, after])),
            patch("fanout_growth.kube_command", command),
        ):
            data = {"initial_deployment": before}
            asyncio.run(scale_once(data, Path(directory), time.monotonic() - 10))
        command.assert_awaited_once_with(
            "scale",
            "deployment/chat-fanout",
            "--replicas=2",
            "--current-replicas=1",
            "--resource-version=100",
        )

    def test_unknown_scale_is_not_retried(self):
        before = {
            "uid": "fanout-deployment",
            "replicas": 1,
            "ready": 1,
            "resource_version": "100",
        }
        command = AsyncMock(side_effect=TimeoutError())
        with (
            TemporaryDirectory() as directory,
            patch("fanout_growth.deployment", AsyncMock(return_value=before)),
            patch("fanout_growth.kube_command", command),
        ):
            data = {"initial_deployment": before}
            asyncio.run(scale_once(data, Path(directory), time.monotonic() - 10))
            self.assertTrue((Path(directory) / "STOP").exists())
        self.assertEqual(command.await_count, 1)


if __name__ == "__main__":
    unittest.main()
