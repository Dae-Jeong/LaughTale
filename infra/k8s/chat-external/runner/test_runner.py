import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load("run")
oracle = load("reconcile")


class RunnerTests(unittest.TestCase):
    def connections(self):
        return [{"profile": profile, "connection_id": f"00000000-0000-4000-8000-{index:012}", "token": "synthetic-token-not-real-secret"}
                for index, profile in enumerate(sorted(runner.PROFILES), 1)]

    def test_seven_profiles_and_stable_replay(self):
        a = runner.make_events("run", "baseline", 14, self.connections())
        b = runner.make_events("run", "baseline", 14, self.connections())
        self.assertEqual(a, b)
        self.assertEqual({event["profile"] for event, _ in a}, runner.PROFILES)
        self.assertEqual(len({event["external_event_id"] for event, _ in a}), 14)

    def test_credentials_require_exact_profile_set(self):
        configured = {item["connection_id"]: {"profile": item["profile"], "token": item["token"]} for item in self.connections()}
        with patch.dict(os.environ, {"G4_CONNECTION_CREDENTIALS": json.dumps(configured)}):
            self.assertEqual(len(runner.credentials()), 7)
        configured.pop(next(iter(configured)))
        with patch.dict(os.environ, {"G4_CONNECTION_CREDENTIALS": json.dumps(configured)}):
            with self.assertRaises(ValueError):
                runner.credentials()

    def test_bounds_reject_before_network(self):
        for options in [("--events", "601"), ("--rate", "11"), ("--duration", "31")]:
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("run.py")), "--run-id", "test", "--phase", "baseline", *options], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("CAPS", result.stderr)

    def fixture(self):
        source = {"text_sha256": "a" * 64, "external_sender_id": "sender", "external_conversation_id": "external-room", "profile": "telegram"}
        row = {**source, "connection_id": "connection", "external_message_id": "message", "message_id": "stored", "conversation_id": "room", "seq": "1"}
        plan = [{**source, "attempt_id": index, "event_id": "event", "connection_id": "connection", "external_message_id": "message", "duplicate": bool(index)} for index in range(2)]
        records = [{**row, **item, "outcome": "acknowledged", "request_id": "request", "actual_start_seconds": float(index), "completed_seconds": index + 0.1} for index, item in enumerate(plan)]
        return {"summary": {"planned_unique_events": 1}, "plan": plan, "records": records}, row

    def test_oracle_detects_missing_and_duplicate_storage(self):
        evidence, row = self.fixture()
        self.assertEqual(oracle.reconcile(evidence, [row])["acknowledged_but_missing_or_changed"], 0)
        self.assertEqual(oracle.reconcile(evidence, [row, row])["duplicate_db_messages"], 1)
        self.assertEqual(oracle.reconcile(evidence, [])["planned_missing_in_db"], 1)
        self.assertGreater(oracle.reconcile(evidence, [{**row, "seq": "2"}])["acknowledged_but_missing_or_changed"], 0)

    def test_pod_distribution_is_joined_not_assumed(self):
        evidence, row = self.fixture()
        self.assertEqual(oracle.reconcile(evidence, [row])["pod_distribution_observed"], 0)
        logs = '[pod/chat-one/chat] ' + json.dumps({"app": {"work": {"id": "request"}}})
        self.assertEqual(oracle.reconcile(evidence, [row], logs)["pod_distribution_observed"], 1)
        self.assertTrue(oracle.reconcile(evidence, [row], logs)["all_acknowledged_requests_mapped_to_pods"])

    def test_incomplete_plan_cannot_pass_oracle(self):
        evidence, row = self.fixture()
        evidence["summary"]["planned_unique_events"] = 2
        with self.assertRaises(ValueError):
            oracle.reconcile(evidence, [row])

    def test_skipped_duplicate_fails_even_when_unique_data_is_stored(self):
        evidence, row = self.fixture()
        evidence["records"][1] = {**evidence["plan"][1], "outcome": "skipped_at_capacity"}
        result = oracle.reconcile(evidence, [row])
        self.assertEqual(result["planned_missing_in_db"], 0)
        self.assertFalse(result["execution_complete"])
        self.assertEqual(result["executed_duplicate_attempts"], 0)

    def test_missing_duplicate_record_fails(self):
        evidence, row = self.fixture()
        evidence["records"].pop()
        result = oracle.reconcile(evidence, [row])
        self.assertFalse(result["execution_complete"])
        self.assertEqual(result["missing_attempts"], 1)

    def test_observed_failure_is_complete_observation_but_not_success(self):
        evidence, row = self.fixture()
        evidence["records"][1]["outcome"] = "unknown"
        result = oracle.reconcile(evidence, [row])
        self.assertTrue(result["execution_complete"])
        self.assertEqual(result["observed_failed_attempts"], 1)

    def test_duplicate_record_id_cannot_replace_missing_execution(self):
        evidence, row = self.fixture()
        evidence["records"][1] = evidence["records"][0]
        self.assertFalse(oracle.reconcile(evidence, [row])["execution_complete"])

    def test_source_content_and_route_corruption_detected_even_for_unknown(self):
        evidence, row = self.fixture()
        for record in evidence["records"]:
            record["outcome"] = "unknown"
        self.assertEqual(oracle.reconcile(evidence, [row])["source_content_or_route_mismatches"], 0)
        for field in ("text_sha256", "external_sender_id", "external_conversation_id", "profile"):
            self.assertEqual(oracle.reconcile(evidence, [{**row, field: "changed"}])["source_content_or_route_mismatches"], 1)

    def test_source_plan_cannot_disagree_between_duplicate_attempts(self):
        evidence, row = self.fixture()
        evidence["plan"][1]["text_sha256"] = "b" * 64
        with self.assertRaises(ValueError):
            oracle.reconcile(evidence, [row])

    def test_collector_refuses_existing_artifact_before_kubectl(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing.json"
            target.write_text("original")
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("collect.py")), "--output", str(target)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ARTIFACT_EXISTS", result.stderr)
            self.assertEqual(target.read_text(), "original")


if __name__ == "__main__":
    unittest.main()
