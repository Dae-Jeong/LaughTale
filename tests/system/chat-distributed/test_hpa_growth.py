import ast
import asyncio
import importlib.util
import json
import time
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from hpa_growth import final_ledger, observe, receive, safe_error, traffic_plan
from hpa_growth_oracle import COUNT, compare, cpu_millicores, idle_issues
from oracle import EvidenceError


def fixture(mode="hpa"):
    plan = traffic_plan(str(uuid4()))
    messages = [
        {
            key: value
            for key, value in row.items()
            if key
            in {"client_message_id", "conversation_id", "sender_id", "text_sha256"}
        }
        | {"message_id": str(uuid4()), "seq": str(index + 1)}
        for index, row in enumerate(plan)
    ]
    return {
        "mode": mode,
        "complete": True,
        "scale_writes": 0,
        "idle": [
            {
                "elapsed": index * 30,
                "metrics_timestamp": str(index),
                "replicas": 1,
                "ready": 1,
                "cpu_millicores": 30,
                "metrics_pod_count": 1,
            }
            for index in range(3)
        ],
        "plan": plan,
        "attempts": [
            row
            | {
                "outcome": "acknowledged",
                "attempt_id": index,
                "ack_status": 201,
                "response_message_id": messages[index]["message_id"],
                "response_seq": messages[index]["seq"],
                "message": messages[index],
                "generator_lag_seconds": 0.01,
                "pod_uid": "new" if mode == "hpa" and index > 300 else "old",
            }
            for index, row in enumerate(plan)
        ],
        "peers": [
            {
                "received": deepcopy(messages),
                "alive_after_traffic": True,
                "target": target,
            }
            for target in ("gateway_a", "gateway_b")
        ],
        "history": deepcopy(messages),
        "initial_api_uids": ["old"],
        "observed_api_uids": ["old", "new"] if mode == "hpa" else ["old"],
        "ready_api_uids": ["old", "new"] if mode == "hpa" else ["old"],
        "hpa_present": mode == "hpa",
        "hpa": {
            "target": 60,
            "cpu_desired_two": mode == "hpa",
            "successful_rescale": mode == "hpa",
        },
    }


class HPATests(unittest.TestCase):
    def test_final_ledger_complete_is_independent_of_hpa_trigger(self):
        evidence = fixture("control")
        evidence.update(run_id=str(uuid4()), mode="hpa", hpa_present=True)
        result = compare(evidence)
        self.assertEqual(result["status"], "not_triggered")
        ledger = final_ledger(evidence, result)
        self.assertTrue(ledger["complete"])
        self.assertEqual(ledger["planned_count"], 540)
        self.assertEqual(ledger["hpa_status"], "not_triggered")
        self.assertEqual(result["auto_down_status"], "not_tested")
        path = Path(__file__).resolve().parents[2] / "load/chat-internal/verify.py"
        spec = importlib.util.spec_from_file_location("hpa_independent_verify", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(len(module.validate_ledger(ledger)), 540)

    def test_only_known_static_error_codes_survive(self):
        self.assertEqual(
            safe_error(EvidenceError("api_metrics_stale")), "api_metrics_stale"
        )
        self.assertEqual(
            safe_error(EvidenceError("synthetic-secret-do-not-log")),
            "evidence_validation_failed",
        )
        self.assertEqual(
            safe_error(RuntimeError("api_metrics_stale")), "evidence_validation_failed"
        )

    def test_observer_binds_hpa_cpu_decision_to_controller_event(self):
        async def scenario():
            current = datetime.now(timezone.utc)
            evidence = {
                "mode": "hpa",
                "started": time.monotonic(),
                "traffic_started_at": (current - timedelta(seconds=10)).isoformat(),
                "hpa": {"target": 60},
            }
            deployment = {
                "metadata": {"uid": "deployment"},
                "spec": {
                    "replicas": 2,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "resources": {"requests": {"cpu": "100m"}},
                                    "env": [
                                        {"name": "DB_POOL_SIZE", "value": "2"},
                                        {"name": "DB_POOL_MAX_OVERFLOW", "value": "0"},
                                    ],
                                }
                            ]
                        }
                    },
                },
                "status": {"readyReplicas": 2},
            }
            metrics = {
                "items": [
                    {
                        "metadata": {"labels": {"app": "chat"}},
                        "timestamp": current.isoformat(),
                        "containers": [{"usage": {"cpu": "80m"}}],
                    }
                ]
            }
            hpa = {
                "metadata": {"name": "chat-growth", "uid": "hpa"},
                "spec": {
                    "scaleTargetRef": {"name": "chat"},
                    "minReplicas": 1,
                    "maxReplicas": 2,
                    "metrics": [
                        {
                            "type": "Resource",
                            "resource": {
                                "name": "cpu",
                                "target": {
                                    "type": "Utilization",
                                    "averageUtilization": 60,
                                },
                            },
                        }
                    ],
                },
                "status": {
                    "desiredReplicas": 2,
                    "currentMetrics": [
                        {
                            "resource": {
                                "name": "cpu",
                                "current": {"averageUtilization": 80},
                            }
                        }
                    ],
                },
            }
            event = {
                "metadata": {"uid": "event"},
                "involvedObject": {"uid": "hpa"},
                "lastTimestamp": current.isoformat(),
                "reason": "SuccessfulRescale",
                "message": "New size: 2; reason: cpu resource utilization above target",
                "source": {"component": "horizontal-pod-autoscaler"},
            }

            async def command(*args):
                assert args[0] == "get"
                if args[1] == "deployment/chat":
                    return json.dumps(deployment)
                if args[1] == "--raw":
                    return json.dumps(metrics)
                return json.dumps({"items": [hpa if args[1] == "hpa" else event]})

            with patch("hpa_growth.kube_command", command):
                sample = await observe(evidence)
                self.assertEqual(sample["cpu_millicores"], 80)
                self.assertTrue(evidence["hpa"]["cpu_desired_two"])
                self.assertTrue(evidence["hpa"]["successful_rescale"])
                evidence["hpa"].pop("successful_rescale")
                event["source"]["component"] = "manual-test"
                await observe(evidence)
                self.assertNotIn("successful_rescale", evidence["hpa"])
                hpa["spec"]["metrics"][0]["resource"]["target"][
                    "averageUtilization"
                ] = 10
                with self.assertRaises(ValueError):
                    await observe(evidence)

        asyncio.run(scenario())

    def test_plan_budget_and_shape_are_fixed(self):
        plan = traffic_plan("synthetic")
        self.assertEqual(COUNT, 540)
        self.assertEqual(len(plan), 540)
        self.assertEqual(plan[0]["scheduled_seconds"], 0)
        self.assertEqual(plan[15]["scheduled_seconds"], 15)
        self.assertEqual(plan[90]["scheduled_seconds"], 30)
        self.assertAlmostEqual(plan[-1]["scheduled_seconds"], 74.9)
        self.assertEqual(len({row["client_message_id"] for row in plan}), 540)

    def test_cpu_units_and_invalid_values(self):
        for raw in ("0.054", "54m", "54000u", "54000000n"):
            self.assertEqual(cpu_millicores(raw), 54)
        for raw in ("NaN", "Infinity", "-1m"):
            with self.assertRaises(ValueError):
                cpu_millicores(raw)

    def test_idle_does_not_accept_threshold_lowering_or_stale_samples(self):
        rows = fixture()["idle"]
        self.assertEqual(idle_issues(rows), [])
        rows[1]["cpu_millicores"] = 54
        self.assertIn("idle_not_below_fixed_threshold_margin", idle_issues(rows))
        for row in rows:
            row["metrics_timestamp"] = "unchanged"
        self.assertIn("idle_distinct_metrics_missing", idle_issues(rows))

    def test_valid_control_and_hpa_and_honest_not_triggered(self):
        self.assertEqual(compare(fixture("control"))["status"], "pass")
        self.assertEqual(compare(fixture())["status"], "pass")
        evidence = fixture("control")
        evidence.update(mode="hpa", hpa_present=True)
        self.assertEqual(compare(evidence)["status"], "not_triggered")

    def test_scale_without_cpu_controller_evidence_does_not_pass(self):
        for key in ("cpu_desired_two", "successful_rescale"):
            evidence = fixture()
            evidence["hpa"][key] = False
            self.assertEqual(compare(evidence)["status"], "fail")
        evidence = fixture()
        evidence["scale_writes"] = 1
        self.assertIn("runner_scale_write_forbidden", compare(evidence)["issues"])

    def test_desired_ready_and_real_handling_are_distinct(self):
        evidence = fixture()
        evidence["ready_api_uids"] = ["old"]
        self.assertIn("desired_two_without_new_ready_api", compare(evidence)["issues"])
        evidence = fixture()
        for attempt in evidence["attempts"]:
            attempt["pod_uid"] = "old"
        self.assertIn("new_api_write_unproven", compare(evidence)["issues"])

    def test_missing_receipt_history_unknown_and_duplicate_fail(self):
        for edit in (
            lambda e: e["peers"][0]["received"].pop(),
            lambda e: e["history"].pop(),
            lambda e: e["attempts"][0].update(outcome="unknown"),
            lambda e: e["peers"][0]["received"].append(e["peers"][0]["received"][0]),
        ):
            evidence = fixture()
            edit(evidence)
            self.assertEqual(compare(evidence)["status"], "fail")

    def test_runner_has_only_get_kubectl_calls(self):
        tree = ast.parse(Path(__file__).with_name("hpa_growth.py").read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "kube_command"
        ]
        self.assertTrue(calls)
        for call in calls:
            self.assertIsInstance(call.args[0], ast.Constant)
            self.assertEqual(call.args[0].value, "get")

    def test_receiver_stops_at_600_without_using_existing_256_cap(self):
        class Socket:
            async def frames(self):
                for _ in range(601):
                    identity = str(uuid4())
                    yield json.dumps(
                        {
                            "type": "message.created",
                            "schema_version": 1,
                            "event_id": identity,
                            "message": {
                                "message_id": identity,
                                "conversation_id": str(uuid4()),
                                "sender_id": str(uuid4()),
                                "client_message_id": str(uuid4()),
                                "seq": "1",
                                "text": "synthetic",
                            },
                        }
                    )

            def __aiter__(self):
                return self.frames()

        async def scenario(directory):
            peer = {"target": "gateway_a", "received": [], "closed": asyncio.Event()}
            await receive(Socket(), peer, directory)
            self.assertEqual(len(peer["received"]), 600)
            self.assertTrue(peer["closed"].is_set())
            self.assertEqual(peer["error"], "ws_closed_or_invalid")

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))


if __name__ == "__main__":
    unittest.main()
