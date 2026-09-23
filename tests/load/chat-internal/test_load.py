import argparse
import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = load("run")
dashboard = load("dashboard")


class LoadTests(unittest.TestCase):
    def test_plan_bounds_unique_and_hash(self):
        plan = run.make_plan("test")
        self.assertEqual(len(plan), 2625)
        self.assertEqual(len({p["client_message_id"] for p in plan}), 2625)
        self.assertLess(plan[-1]["scheduled_seconds"], 60)
        self.assertEqual(len(run.make_plan("test", 1)), 175)
        for invalid in (0, 16):
            with self.assertRaises(ValueError):
                run.make_plan("test", invalid)

    def test_endpoint_restriction(self):
        self.assertEqual(
            run.endpoint("http://127.0.0.1:18092"), "http://127.0.0.1:18092"
        )
        for value in (
            "http://localhost:18092",
            "http://127.0.0.1:18082",
            "http://127.0.0.1:18092/x",
            "https://127.0.0.1:18092",
            "http://user@127.0.0.1:18092",
        ):
            with self.assertRaises(ValueError):
                run.endpoint(value)

    def test_ack_contract_and_changed_body(self):
        item = run.make_plan("test", 1)[0]
        data = {
            "message_id": str(uuid4()),
            "conversation_id": run.ROOM,
            "sender_id": run.SENDER,
            "client_message_id": item["client_message_id"],
            "text": run.text_for("test", item["attempt_id"]),
            "seq": "1",
            "created_at": "2026-09-08T00:00:00Z",
            "state": "stored",
        }
        self.assertEqual(run.validate_ack({"data": data}, item)["response_seq"], "1")
        for field, value in (
            ("text", "changed"),
            ("client_message_id", str(uuid4())),
            ("seq", "0"),
            ("state", "pending"),
        ):
            with self.assertRaises(ValueError):
                run.validate_ack({"data": {**data, field: value}}, item)
        without_state = {key: value for key, value in data.items() if key != "state"}
        with self.assertRaises(ValueError):
            run.validate_ack({"data": without_state}, item)

    def test_stop_never_complete_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new"
            evidence = run.Evidence(path, [run.make_plan("test", 1)[0]], "test")
            evidence.stop_reason = "operator_stop"
            evidence.record({"outcome": "not_attempted"})
            result = evidence.finish()
            self.assertFalse(result["complete"])
            self.assertFalse(result["passed"])
            self.assertEqual(
                json.loads((path / "live/status.json").read_text())["status"], "stopped"
            )
            with self.assertRaises(FileExistsError):
                run.Evidence(path, [], "test")

    def test_routes_no_filesystem_browsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.assertIsNone(dashboard.resource("/manifest.json", path))
            self.assertIsNone(dashboard.resource("/../requests.jsonl", path))
            self.assertIsNone(dashboard.resource("/live/status.json?start=1", path))
            self.assertIsNotNone(dashboard.resource("/live/status.json", path))
            self.assertEqual(
                dashboard.resource("/resources.jsonl", path)[0],
                path / "resources.jsonl",
            )
            self.assertEqual(
                dashboard.resource("/verification.json", path)[0],
                path / "verified.json",
            )
            self.assertIsNone(dashboard.resource("/verified.json", path))
            self.assertIsNone(dashboard.resource("/requests.jsonl", path))

    def test_verification_exposes_only_summary(self):
        raw = {
            "status": "incomplete",
            "counts": {"ack": 5},
            "issues": [],
            "scope": "run",
            "attempts": [{"secret": "hidden"}],
            "credentials": "hidden",
        }
        result = json.loads(
            dashboard.public_body("/verification.json", json.dumps(raw).encode())
        )
        self.assertEqual(set(result), {"status", "counts", "issues", "scope"})
        self.assertNotIn("hidden", json.dumps(result))
        with self.assertRaises(TypeError):
            dashboard.public_body("/verification.json", b"[]")

    def test_public_routes_reject_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "verified.json").symlink_to(path / "sensitive")
            (path / "resources.jsonl").symlink_to(path / "sensitive")
            self.assertIsNone(dashboard.resource("/verification.json", path))
            self.assertIsNone(dashboard.resource("/resources.jsonl", path))

    def test_percentile(self):
        self.assertIsNone(run.percentile([], 0.95))
        self.assertEqual(run.percentile(list(range(1, 101)), 0.99), 99)


class ExecuteTests(unittest.TestCase):
    def exercise(self, stop=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run"
            make_plan = run.make_plan

            def simultaneous(run_id, seconds):
                return [
                    {**item, "scheduled_seconds": 0}
                    for item in make_plan(run_id, seconds)[:40]
                ]

            async def fake_request(client, route, body=None):
                if route == "/v1/dev/session":
                    if stop:
                        (path / "STOP").touch()
                    return 200, {"data": {"user_id": run.SENDER, "display_name": "A"}}
                if route == "/v1/internal-conversations":
                    return 200, {
                        "data": [
                            {
                                "conversation_id": run.ROOM,
                                "kind": "dm",
                                "title": "DM",
                                "head_seq": "0",
                            }
                        ]
                    }
                if stop:
                    self.fail("STOP 이후 요청이 전송되었습니다")
                await asyncio.sleep(0.01)
                return 503, {"error": "synthetic failure"}

            args = argparse.Namespace(
                run_dir=path,
                stage_seconds=1,
                max_requests=4000,
                api_origin="http://127.0.0.1:18092",
            )
            with (
                patch.object(run, "request", fake_request),
                patch.object(run, "make_plan", simultaneous),
            ):
                result = asyncio.run(run.execute(args))
            self.assertNotIn('"text":', (path / "requests.jsonl").read_text())
            return result

    def test_saturation_and_503_unknown(self):
        result = self.exercise()
        self.assertTrue(result["complete"])
        self.assertFalse(result["passed"])
        self.assertEqual(result["counts"], {"unknown": 32, "not_attempted": 8})
        self.assertEqual(len(result["attempts"]), 40)

    def test_stop_during_preflight_dispatches_nothing(self):
        result = self.exercise(stop=True)
        self.assertFalse(result["complete"])
        self.assertEqual(result["stop_reason"], "operator_stop")
        self.assertEqual(result["counts"], {"not_attempted": 40})


if __name__ == "__main__":
    unittest.main()
