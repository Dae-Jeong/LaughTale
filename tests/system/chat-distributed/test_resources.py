import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from runner import ResourceGuard, run


class ResourceTests(unittest.TestCase):
    def test_first_sample_uses_distributed_and_failure_stops(self):
        with TemporaryDirectory() as directory:
            sample = Mock(
                return_value=({"status": "stop", "reason": "host_memory_low"}, {})
            )
            guard = ResourceGuard(Path(directory), sample)
            self.assertFalse(asyncio.run(guard.sample_once()))
            sample.assert_called_once_with(distributed=True)
            self.assertTrue((Path(directory) / "STOP").exists())

    def test_sample_error_is_incomplete_without_exception_text(self):
        with TemporaryDirectory() as directory:
            guard = ResourceGuard(
                Path(directory), Mock(side_effect=ValueError("private details"))
            )
            self.assertFalse(asyncio.run(guard.sample_once()))
            raw = (Path(directory) / "resources.jsonl").read_text()
            self.assertNotIn("private details", raw)
            self.assertEqual(json.loads(raw)["status"], "incomplete")

    def test_changed_identity_stops(self):
        with TemporaryDirectory() as directory:
            sample = Mock(
                side_effect=[
                    ({"status": "ok"}, {"pod": "a"}),
                    ({"status": "ok"}, {"pod": "b"}),
                ]
            )
            guard = ResourceGuard(Path(directory), sample)
            self.assertTrue(asyncio.run(guard.sample_once()))
            self.assertFalse(asyncio.run(guard.sample_once()))
            self.assertTrue((Path(directory) / "STOP").exists())

    def test_completion_takes_final_sample(self):
        with TemporaryDirectory() as directory:
            sample = Mock(return_value=({"status": "ok"}, {"pod": "a"}))
            guard = ResourceGuard(Path(directory), sample)
            guard.done.set()
            asyncio.run(guard.monitor())
            self.assertEqual(sample.call_count, 1)

    def test_failed_preflight_never_starts_http_exercise(self):
        with TemporaryDirectory() as directory:
            exercise = AsyncMock()
            with (
                patch(
                    "runner.resource_sampler",
                    return_value=Mock(side_effect=ValueError()),
                ),
                patch("runner.exercise", exercise),
                redirect_stdout(io.StringIO()),
            ):
                result = asyncio.run(run("smoke", Path(directory), True))
            self.assertEqual(result, 1)
            exercise.assert_not_awaited()
            evidence = json.loads((Path(directory) / "evidence.json").read_text())
            self.assertEqual(evidence["attempts"], [])
            self.assertEqual(evidence["error"], "resource_preflight_failed")


if __name__ == "__main__":
    unittest.main()
