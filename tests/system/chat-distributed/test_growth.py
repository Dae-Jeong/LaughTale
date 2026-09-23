import asyncio
import time
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from growth import scale_once
from growth_oracle import compare_growth, transition_issues
from oracle import ACTOR, ROOM, normalize


def stamp(seconds):
    return f"2026-09-08T00:00:{seconds:06.3f}+00:00"


def pod(uid, app="chat", ready=True, ready_since=None):
    return {
        "uid": uid,
        "app": app,
        "ready": ready,
        "ready_since": ready_since or stamp(0),
        "restarts": 0,
        "terminating": False,
    }


def fixture():
    initial = {
        "observed_at": stamp(0),
        "status": "ok",
        "containers": {"db": ["container-a", 0]},
        "pods": [
            pod("old"),
            pod("gw-a", "chat-gateway"),
            pod("gw-b", "chat-gateway"),
            pod("fan-a", "chat-fanout"),
            pod("fan-b", "chat-fanout"),
            pod("relay", "chat-relay"),
            pod("proxy", "chat-growth-proxy"),
        ],
    }
    final = deepcopy(initial)
    final["observed_at"] = stamp(25)
    final["pods"].append(pod("new", ready_since=stamp(10.4)))
    data = {
        "complete": True,
        "session_entry_verified": True,
        "traffic_started_at": stamp(9),
        "initial_deployment": {
            "uid": "deployment-a",
            "replicas": 1,
            "ready": 1,
            "resource_version": "90",
        },
        "samples": [initial, final],
        "plan": [],
        "attempts": [],
        "peers": [],
        "scale": {
            "success": True,
            "writes": 1,
            "replicas_before": 1,
            "replicas_after": 2,
            "resource_version_before": "100",
            "requested_at": stamp(10),
            "completed_at": stamp(10.1),
            "rollout_ready_observed_at": stamp(10.5),
            "deployment_uid": "deployment-a",
            "before": {
                "uid": "deployment-a",
                "replicas": 1,
                "ready": 1,
                "resource_version": "100",
            },
        },
    }
    messages = []
    for index, (when, uid) in enumerate(((9.8, "old"), (10.2, "old"), (10.6, "new"))):
        message = normalize(
            {
                "message_id": f"00000000-0000-4000-8000-{index + 100:012d}",
                "conversation_id": ROOM,
                "sender_id": ACTOR,
                "client_message_id": f"00000000-0000-4000-8000-{index + 200:012d}",
                "seq": str(index + 1),
                "text": "synthetic",
            }
        )
        messages.append(message)
        intent = {
            key: message[key]
            for key in (
                "client_message_id",
                "conversation_id",
                "sender_id",
                "text_sha256",
            )
        }
        data["plan"].append(intent)
        data["attempts"].append(
            {
                **intent,
                "outcome": "acknowledged",
                "message": message,
                "started_at": stamp(when),
                "completed_at": stamp(when),
                "pod_uid": uid,
                "latency_seconds": 0.01,
                "generator_lag_seconds": 0.002,
            }
        )
    data["peers"] = [
        {
            "id": str(index),
            "target": "gateway_a" if index == 0 else "gateway_b",
            "received": deepcopy(messages),
            "alive_after_traffic": True,
        }
        for index in range(2)
    ]
    return data


class GrowthOracleTests(unittest.TestCase):
    def test_cross_phase_gap_budget_fails_even_with_each_phase_present(self):
        data = fixture()
        data["attempts"][0].update(started_at=stamp(8), completed_at=stamp(8))
        result = compare_growth(data)
        self.assertIn("global_start_gap_budget_exceeded", result["issues"])
        self.assertTrue(all(row["requests"] for row in result["phases"].values()))

    def test_generator_lag_budget_fails_independently_of_latency(self):
        data = fixture()
        data["attempts"][1]["generator_lag_seconds"] = 1.001
        self.assertIn("generator_lag_budget_exceeded", compare_growth(data)["issues"])

    def test_one_second_budget_boundary_passes(self):
        data = fixture()
        data["attempts"][0].update(started_at=stamp(9.2), completed_at=stamp(9.2))
        data["attempts"][1]["generator_lag_seconds"] = 1.0
        self.assertEqual(compare_growth(data)["status"], "pass")

    def test_wrong_scale_identity_or_resource_version_fails(self):
        data = fixture()
        data["scale"]["deployment_uid"] = "another-deployment"
        self.assertIn(
            "scale_deployment_identity_mismatch", compare_growth(data)["issues"]
        )
        for value in (None, "", " \t", "101"):
            data = fixture()
            data["scale"]["resource_version_before"] = value
            self.assertIn(
                "scale_resource_version_invalid_or_unbound",
                compare_growth(data)["issues"],
            )

    def test_scale_time_order_is_required(self):
        data = fixture()
        data["scale"]["completed_at"] = stamp(0)
        self.assertIn("scale_time_order_invalid", compare_growth(data)["issues"])
        data = fixture()
        del data["scale"]["rollout_ready_observed_at"]
        self.assertIn("scale_time_evidence_invalid", compare_growth(data)["issues"])

    def test_missing_fixed_components_even_in_all_samples_fails(self):
        data = fixture()
        for sample in data["samples"]:
            sample["pods"] = [
                row
                for row in sample["pods"]
                if row["app"] not in ("chat-fanout", "chat-growth-proxy")
            ]
        self.assertIn("initial_fixed_topology_invalid", compare_growth(data)["issues"])

    def test_two_connections_to_same_gateway_fail(self):
        data = fixture()
        data["peers"][1]["target"] = "gateway_a"
        self.assertIn("both_gateway_targets_required", compare_growth(data)["issues"])

    def test_valid_transition_has_all_three_phases(self):
        result = compare_growth(fixture())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            [row["requests"] for row in result["phases"].values()], [1, 1, 1]
        )

    def test_new_api_not_handling_requests_fails(self):
        data = fixture()
        data["attempts"][-1]["pod_uid"] = "old"
        self.assertIn(
            "both_api_pods_must_handle_requests", compare_growth(data)["issues"]
        )

    def test_missing_or_forged_pod_header_fails(self):
        for uid in (None, "not-observed"):
            data = fixture()
            data["attempts"][-1]["pod_uid"] = uid
            self.assertIn("unverified_handling_pod_uid", compare_growth(data)["issues"])

    def test_new_pod_before_ready_fails(self):
        data = fixture()
        data["attempts"][1]["pod_uid"] = "new"
        self.assertIn(
            "new_pod_handled_before_scale_or_ready", compare_growth(data)["issues"]
        )

    def test_baseline_pod_replacement_fails(self):
        data = fixture()
        data["samples"][-1]["pods"][1]["uid"] = "replacement-gateway"
        self.assertIn("baseline_pod_missing", compare_growth(data)["issues"])
        self.assertIn("unapproved_pod_added", compare_growth(data)["issues"])

    def test_restart_or_second_added_pod_fails(self):
        data = fixture()
        data["samples"][-1]["pods"][0]["restarts"] = 1
        self.assertIn("pod_restart_or_termination", compare_growth(data)["issues"])
        data = fixture()
        data["samples"][-1]["pods"].append(pod("extra"))
        self.assertIn("unexpected_api_uid_transition", compare_growth(data)["issues"])

    def test_container_restart_fails(self):
        data = fixture()
        data["samples"][-1]["containers"]["db"][1] = 1
        self.assertIn(
            "container_identity_or_restart_changed", compare_growth(data)["issues"]
        )

    def test_missing_scale_or_retry_fails(self):
        for writes in (0, 2):
            data = fixture()
            data["scale"]["writes"] = writes
            self.assertIn("scale_not_confirmed_once", compare_growth(data)["issues"])

    def test_ws_missing_duplicate_and_changed_payload_fail(self):
        data = fixture()
        data["peers"][1]["received"].pop()
        self.assertIn("missing_ws_receipt", compare_growth(data)["issues"])
        data = fixture()
        data["peers"][1]["received"].append(deepcopy(data["peers"][1]["received"][0]))
        self.assertIn("duplicate_ws_receipt", compare_growth(data)["issues"])
        data = fixture()
        data["peers"][1]["received"][0]["text_sha256"] = "0" * 64
        self.assertIn("ws_payload_mismatch", compare_growth(data)["issues"])

    def test_new_pending_api_allowed_only_during_transition(self):
        data = fixture()
        baseline, current = data["samples"]
        current["pods"][-1]["ready"] = False
        self.assertEqual(transition_issues(baseline, current, True), [])
        self.assertIn(
            "unapproved_pod_added", transition_issues(baseline, current, False)
        )
        self.assertIn("final_two_ready_apis_missing", compare_growth(data)["issues"])

    def test_unknown_request_never_passes(self):
        data = fixture()
        data["attempts"][1]["outcome"] = "unknown"
        self.assertIn("request_unknown", compare_growth(data)["issues"])


class ScaleCommandTests(unittest.TestCase):
    def test_exact_one_preconditioned_write(self):
        before = {
            "uid": "deployment-a",
            "replicas": 1,
            "ready": 1,
            "resource_version": "123",
        }
        after = {
            "uid": "deployment-a",
            "replicas": 2,
            "ready": 2,
            "generation": 2,
            "observed_generation": 2,
        }
        evidence = {"initial_deployment": before}
        command = AsyncMock(return_value="scaled")
        with (
            TemporaryDirectory() as directory,
            patch("growth.deployment", AsyncMock(side_effect=[before, after])),
            patch("growth.kube_command", command),
        ):
            asyncio.run(scale_once(evidence, Path(directory), time.monotonic() - 10))
        command.assert_awaited_once_with(
            "scale",
            "deployment/chat",
            "--replicas=2",
            "--current-replicas=1",
            "--resource-version=123",
        )
        self.assertTrue(evidence["scale"]["success"])

    def test_wrong_replica_precondition_does_not_write(self):
        before = {
            "uid": "deployment-a",
            "replicas": 2,
            "ready": 2,
            "resource_version": "123",
        }
        command = AsyncMock()
        with (
            TemporaryDirectory() as directory,
            patch("growth.deployment", AsyncMock(return_value=before)),
            patch("growth.kube_command", command),
        ):
            evidence = {"initial_deployment": before}
            asyncio.run(scale_once(evidence, Path(directory), time.monotonic() - 10))
            self.assertTrue((Path(directory) / "STOP").exists())
        command.assert_not_awaited()

    def test_write_timeout_is_not_retried_or_rolled_back(self):
        before = {
            "uid": "deployment-a",
            "replicas": 1,
            "ready": 1,
            "resource_version": "123",
        }
        command = AsyncMock(side_effect=TimeoutError())
        with (
            TemporaryDirectory() as directory,
            patch("growth.deployment", AsyncMock(return_value=before)),
            patch("growth.kube_command", command),
        ):
            evidence = {"initial_deployment": before}
            asyncio.run(scale_once(evidence, Path(directory), time.monotonic() - 10))
            self.assertTrue((Path(directory) / "STOP").exists())
        self.assertEqual(command.await_count, 1)
        self.assertFalse(evidence["scale"]["success"])


if __name__ == "__main__":
    unittest.main()
