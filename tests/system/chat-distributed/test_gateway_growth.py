"""E2 oracle negative controls; no cluster, network or scaling execution."""

import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import gateway_growth as gateway


def stamp(seconds):
    return (datetime(2026, 9, 8, tzinfo=UTC) + timedelta(seconds=seconds)).isoformat()


def fixture():
    pods = [
        {
            "uid": str(uuid4()),
            "name": f"{app}-{number}",
            "app": app,
            "ready": True,
            "restarts": 0,
            "terminating": False,
        }
        for app, count in gateway.INITIAL.items()
        for number in range(count)
    ]
    old = next(p for p in pods if p["app"] == "chat-gateway")
    new = {**old, "uid": str(uuid4()), "name": "chat-gateway-new"}
    deployment = {
        "uid": str(uuid4()),
        "resource_version": "12345",
        "replicas": 1,
        "ready": 1,
    }
    e = {
        "complete": True,
        "monitoring_completed": True,
        "resource_error": False,
        "traffic_started_at": stamp(0),
        "initial_deployment": deployment,
        "samples": [],
        "plan": [],
        "attempts": [],
        "peers": [],
        "scale": {
            "before": deepcopy(deployment),
            "deployment_uid": deployment["uid"],
            "resource_version_before": "12345",
            "success": True,
            "writes": 1,
            "requested_at": stamp(10),
            "completed_at": stamp(10.2),
        },
        "new_gateway": {
            "uid": new["uid"],
            "name": new["name"],
            "ready_observed_at": stamp(11),
        },
    }
    for t, sample_pods in ((-1, pods), (11, pods + [new]), (40, pods + [new])):
        e["samples"].append(
            {
                "observed_at": stamp(t),
                "status": "ok",
                "pods": deepcopy(sample_pods),
                "containers": {
                    "postgres-primary": "stable",
                    "postgres-replica": "stable",
                    "kafka": "stable",
                    "redis": "stable",
                },
            }
        )
    for index, scheduled in enumerate(gateway.schedule()):
        plan = {
            "client_message_id": str(uuid4()),
            "conversation_id": gateway.ROOM,
            "sender_id": gateway.ACTOR,
            "text_sha256": sha256(f"synthetic-{index}".encode()).hexdigest(),
            "planned_at_seconds": scheduled,
        }
        message = {
            **{
                key: plan[key]
                for key in (
                    "client_message_id",
                    "conversation_id",
                    "sender_id",
                    "text_sha256",
                )
            },
            "message_id": str(uuid4()),
            "seq": str(index + 101),
        }
        e["plan"].append(plan)
        e["attempts"].append(
            {
                **plan,
                "attempt_id": index,
                "outcome": "acknowledged",
                "ack_status": 201,
                "http_status": 201,
                "started_at": stamp(scheduled),
                "completed_at": stamp(scheduled + 0.01),
                "latency_seconds": 0.01,
                "generator_lag_seconds": 0,
                "message": message,
            }
        )
    for identity, target, pod, baseline, joined in (
        ("existing", "gateway_a", old, 100, -0.1),
        ("new", "gateway_b", new, 120, 12),
    ):
        e["peers"].append(
            {
                "id": identity,
                "target": target,
                "pod_uid": pod["uid"],
                "instance_id": str(uuid4()),
                "baseline_head": str(baseline),
                "joined_at": stamp(joined),
                "alive_after_traffic": True,
                "error": None,
                "received": [
                    deepcopy(a["message"])
                    for a in e["attempts"]
                    if int(a["message"]["seq"]) > baseline
                ],
            }
        )
    return e


class GatewayGrowthTests(unittest.TestCase):
    def assert_failure(self, value):
        self.assertEqual(gateway.compare(value)["status"], "fail")

    def test_valid_210_ack_existing_all_new_after_head(self):
        result = gateway.compare(fixture())
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["acknowledged"], 210)
        self.assertEqual(result["cohorts"]["existing"]["received"], 210)
        self.assertEqual(result["cohorts"]["new"]["expected_after_subscription"], 190)

    def test_missing_receipt_on_either_cohort(self):
        for index in (0, 1):
            with self.subTest(cohort=index):
                e = fixture()
                e["peers"][index]["received"].pop()
                self.assert_failure(e)

    def test_same_pod_cannot_be_forged_as_two_cohorts(self):
        for key in ("pod_uid", "instance_id"):
            with self.subTest(key=key):
                e = fixture()
                e["peers"][1][key] = e["peers"][0][key]
                self.assert_failure(e)

    def test_traffic_gap_is_not_hidden(self):
        e = fixture()
        e["attempts"][8]["started_at"] = stamp(12)
        self.assert_failure(e)

    def test_scale_identity_and_resource_version_required(self):
        for key, value in (
            ("uid", str(uuid4())),
            ("resource_version", ""),
            ("replicas", 2),
            ("ready", 0),
        ):
            with self.subTest(key=key):
                e = fixture()
                e["scale"]["before"][key] = value
                self.assert_failure(e)

    def test_new_gateway_must_actually_exist_and_be_ready(self):
        e = fixture()
        for sample in e["samples"][1:]:
            sample["pods"] = deepcopy(e["samples"][0]["pods"])
        self.assert_failure(e)
        e = fixture()
        e["samples"][-1]["pods"][-1]["ready"] = False
        self.assert_failure(e)

    def test_ack_mark_without_message_cannot_reduce_expected_receipts(self):
        e = fixture()
        removed = e["attempts"][0]["message"]["message_id"]
        e["attempts"][0]["message"] = None
        for peer in e["peers"]:
            peer["received"] = [
                row for row in peer["received"] if row["message_id"] != removed
            ]
        self.assert_failure(e)

    def test_inflated_subscription_head_cannot_hide_later_messages(self):
        e = fixture()
        e["peers"][1]["baseline_head"] = "309"
        e["peers"][1]["received"] = e["peers"][1]["received"][-1:]
        self.assert_failure(e)

    def test_existing_cohort_must_receive_all_210(self):
        e = fixture()
        e["peers"][0]["baseline_head"] = "101"
        e["peers"][0]["received"].pop(0)
        self.assert_failure(e)

    def test_join_before_ready_or_after_all_traffic_is_invalid(self):
        for value in (stamp(10), stamp(60)):
            with self.subTest(value=value):
                e = fixture()
                e["peers"][1]["joined_at"] = value
                self.assert_failure(e)

    def test_duplicate_and_changed_payload_are_rejected(self):
        e = fixture()
        e["peers"][0]["received"].append(deepcopy(e["peers"][0]["received"][0]))
        self.assert_failure(e)
        e = fixture()
        e["peers"][1]["received"][0]["text_sha256"] = "f" * 64
        self.assert_failure(e)

    def test_ack_requires_successful_http_and_ack_status(self):
        for field in ("http_status", "ack_status"):
            with self.subTest(field=field):
                e = fixture()
                e["attempts"][0][field] = 500
                self.assert_failure(e)

    def test_gateway_instance_identity_must_be_present_and_valid(self):
        for value in (None, "", "not-a-uuid"):
            with self.subTest(value=value):
                e = fixture()
                e["peers"][1]["instance_id"] = value
                self.assert_failure(e)

    def test_incomplete_scale_and_resource_failure(self):
        for field, value in (
            ("complete", False),
            ("monitoring_completed", False),
            ("resource_error", True),
        ):
            with self.subTest(field=field):
                e = fixture()
                e[field] = value
                self.assert_failure(e)
        e = fixture()
        e["scale"]["success"] = False
        self.assert_failure(e)


if __name__ == "__main__":
    unittest.main()
