import asyncio
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import replica_experiment as experiment
from oracle import ACTOR, ROOM, EvidenceError


class FakeAdmin:
    def __init__(self, pause_unknown=False):
        self.paused = False
        self.pause_unknown = pause_unknown
        self.calls = []

    async def __call__(self, sql):
        self.calls.append(sql)
        if "pg_is_in_recovery" in sql:
            return "t\nf"
        if "pg_wal_replay_pause()" in sql:
            self.paused = True
            if self.pause_unknown:
                raise EvidenceError("synthetic_pause_reply_unknown")
            return ""
        if "pg_get_wal_replay_pause_state" in sql:
            return "paused"
        if "pg_wal_replay_resume" in sql:
            self.paused = False
            return ""
        if "pg_is_wal_replay_paused" in sql:
            return "t" if self.paused else "f"
        raise AssertionError("unexpected_fake_sql")


class HTTP:
    async def request(self, *args, **kwargs):
        return 200, {}, "synthetic-cookie-never-persisted"


def intents():
    attempts, reference = [], []
    for index in range(20):
        message = {
            "message_id": str(uuid4()),
            "conversation_id": ROOM,
            "sender_id": ACTOR,
            "client_message_id": str(uuid4()),
            "seq": str(index + 1),
            "text_sha256": "a" * 64,
        }
        attempts.append(message | {"outcome": "acknowledged", "message": message})
        reference.append(message | {"seq": index + 1})
    return attempts, reference


class ReplicaExperimentTests(unittest.TestCase):
    def test_first_post_failure_and_unknown_pause_reply_both_resume_owned_pause(self):
        async def scenario(directory, pause_unknown, cancelled):
            admin = FakeAdmin(pause_unknown)
            with (
                patch.object(experiment, "admin", admin),
                patch.object(
                    experiment, "resource_snapshot", return_value={"status": "ok"}
                ),
                patch.object(experiment.history, "admin_identity", return_value={}),
                patch.object(experiment, "LabHTTP", HTTP),
                patch.object(
                    experiment,
                    "entry_request",
                    side_effect=asyncio.CancelledError()
                    if cancelled
                    else EvidenceError("synthetic_post_failure"),
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(await experiment.run(directory), 1)
            self.assertFalse(admin.paused)
            self.assertIn("SELECT pg_wal_replay_resume();", admin.calls)
            result = json.loads((directory / "result.json").read_text())
            self.assertTrue(result["resumed"])
            self.assertFalse(result["recovery_required"])
            ledger = json.loads((directory / "final.json").read_text())
            self.assertFalse(ledger["complete"])
            if cancelled:
                self.assertEqual(result["error"], "experiment_cancelled")
            self.assertNotIn("synthetic-cookie", (directory / "final.json").read_text())

        for unknown, cancelled in ((False, False), (True, False), (False, True)):
            with TemporaryDirectory() as directory:
                asyncio.run(scenario(Path(directory), unknown, cancelled))

    def test_already_paused_replica_is_not_owned_or_resumed(self):
        async def scenario(directory):
            calls = []

            async def admin(sql):
                calls.append(sql)
                return "t\nt"

            with (
                patch.object(experiment, "admin", admin),
                patch.object(
                    experiment, "resource_snapshot", return_value={"status": "ok"}
                ),
                patch.object(experiment.history, "admin_identity", return_value={}),
                patch("builtins.print"),
            ):
                self.assertEqual(await experiment.run(directory), 1)
            self.assertEqual(len(calls), 1)
            result = json.loads((directory / "result.json").read_text())
            self.assertFalse(result["pause_requested"])
            self.assertFalse(result["recovery_required"])

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_repeated_cancellation_during_resume_does_not_cancel_recovery(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            paused = True

            async def admin(sql):
                nonlocal paused
                if "pg_wal_replay_resume" in sql:
                    entered.set()
                    await release.wait()
                    paused = False
                    return ""
                return "f"

            result = {"resumed": False}
            with patch.object(experiment, "admin", admin):
                task = asyncio.create_task(experiment.resume_owned_pause(result))
                await entered.wait()
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(paused)
            self.assertTrue(result["resumed"])
            self.assertFalse(result["recovery_required"])

        asyncio.run(scenario())

    def test_unreachable_database_records_recovery_required_with_bounded_attempts(self):
        async def scenario():
            calls = []

            async def admin(sql):
                calls.append(sql)
                raise OSError("synthetic")

            result = {"resumed": False}
            with patch.object(experiment, "admin", admin):
                await experiment.resume_owned_pause(result)
            self.assertEqual(len(calls), 3)
            self.assertFalse(result["resumed"])
            self.assertTrue(result["recovery_required"])
            self.assertLess(result["recovery_seconds"], 15)

        asyncio.run(scenario())

    def test_recovery_deadline_records_unconfirmed_instead_of_claiming_success(self):
        async def scenario():
            original_timeout = asyncio.timeout

            async def admin(sql):
                await asyncio.Event().wait()

            result = {"resumed": False}
            with (
                patch.object(experiment, "admin", admin),
                patch.object(
                    experiment.asyncio,
                    "timeout",
                    side_effect=lambda seconds: original_timeout(
                        0.01 if seconds == 13 else seconds
                    ),
                ),
            ):
                await experiment.resume_owned_pause(result)
            self.assertFalse(result["resumed"])
            self.assertTrue(result["recovery_required"])
            self.assertEqual(result["recovery_error"], "resume_deadline_exceeded")

        asyncio.run(scenario())

    def test_reference_must_be_exactly_this_runs_twenty_intents(self):
        attempts, reference = intents()
        experiment.bind_reference(attempts, reference)
        for change in (
            lambda rows: rows[0].update(client_message_id=str(uuid4())),
            lambda rows: rows[0].update(text_sha256="b" * 64),
            lambda rows: rows[0].update(seq=21),
            lambda rows: rows.pop(),
        ):
            changed = deepcopy(reference)
            change(changed)
            with self.assertRaises(EvidenceError):
                experiment.bind_reference(attempts, changed)


if __name__ == "__main__":
    unittest.main()
