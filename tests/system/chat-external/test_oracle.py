"""Synthetic offline tests, including deliberately corrupted observations."""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from io import StringIO
from pathlib import Path
from uuid import UUID

from oracle import main, reconcile


def uid(number: int) -> str:
    return str(UUID(int=number))


def fixture() -> tuple[dict, dict, dict]:
    common = {
        "schema_version": 1,
        "run_id": "oracle-synthetic-1",
        "synthetic": True,
        "complete": True,
    }
    route = {
        "profile": "telegram",
        "connection_id": uid(1),
        "external_conversation_id": "room-1",
    }
    inbound = {
        **route,
        "external_message_id": "message-1",
        "external_sender_id": "customer-1",
        "text_sha256": sha256("합성 문의".encode()).hexdigest(),
    }
    outbound = {
        **route,
        "outbound_operation_id": uid(2),
        "text_sha256": sha256(b"synthetic reply").hexdigest(),
    }
    manifest = {
        **common,
        "inbound": [inbound],
        "outbound": [{**outbound, "expected_state": "accepted", "expected_effects": 1}],
        "attempts": [
            {"attempt_id": "in-1", "direction": "inbound", "intent_index": 0},
            {"attempt_id": "out-1", "direction": "outbound", "intent_index": 0},
        ],
    }
    chat = {
        **common,
        "inbound_messages": [{**inbound, "message_id": uid(3), "seq": "1"}],
        "outbound_jobs": [{**outbound, "state": "accepted", "effect_id": uid(4)}],
    }
    mock = {**common, "effects": [{**outbound, "effect_id": uid(4)}]}
    return manifest, chat, mock


class OracleTests(unittest.TestCase):
    def test_normal_roundtrip(self) -> None:
        result = reconcile(*fixture())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["counts"]["provider_effects"], 1)

    def test_repeated_intent_and_attempt_are_not_duplicate_effects(self) -> None:
        manifest, chat, mock = fixture()
        manifest["inbound"].append(deepcopy(manifest["inbound"][0]))
        manifest["attempts"].append(
            {"attempt_id": "in-2", "direction": "inbound", "intent_index": 1}
        )
        result = reconcile(manifest, chat, mock)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["counts"]["inbound_intents"], 1)
        self.assertEqual(result["counts"]["attempts"], 3)

    def test_missing_observation_is_failure_when_snapshot_is_complete(self) -> None:
        for source, collection in (
            (1, "inbound_messages"),
            (1, "outbound_jobs"),
            (2, "effects"),
        ):
            with self.subTest(collection=collection):
                evidence = fixture()
                evidence[source][collection].clear()
                self.assertEqual(reconcile(*evidence)["status"], "failed")

    def test_duplicate_observation_is_failure(self) -> None:
        for source, collection in (
            (1, "inbound_messages"),
            (1, "outbound_jobs"),
            (2, "effects"),
        ):
            with self.subTest(collection=collection):
                evidence = fixture()
                evidence[source][collection].append(
                    deepcopy(evidence[source][collection][0])
                )
                self.assertEqual(reconcile(*evidence)["status"], "failed")

    def test_same_count_but_wrong_route_sender_or_body_fails(self) -> None:
        for source, collection, names in (
            (
                1,
                "inbound_messages",
                (
                    "profile",
                    "connection_id",
                    "external_conversation_id",
                    "external_sender_id",
                    "text_sha256",
                ),
            ),
            (
                1,
                "outbound_jobs",
                ("profile", "connection_id", "external_conversation_id", "text_sha256"),
            ),
            (
                2,
                "effects",
                ("profile", "connection_id", "external_conversation_id", "text_sha256"),
            ),
        ):
            for name in names:
                with self.subTest(collection=collection, name=name):
                    evidence = fixture()
                    replacement = {
                        "profile": "line",
                        "connection_id": uid(99),
                        "text_sha256": "a" * 64,
                    }.get(name, "wrong-target")
                    evidence[source][collection][0][name] = replacement
                    self.assertEqual(reconcile(*evidence)["status"], "failed")

    def test_separate_profile_account_and_room_do_not_collapse(self) -> None:
        for name, value in (
            ("profile", "line"),
            ("connection_id", uid(99)),
            ("external_conversation_id", "room-2"),
        ):
            with self.subTest(name=name):
                manifest, chat, mock = fixture()
                manifest["inbound"].append({**manifest["inbound"][0], name: value})
                chat["inbound_messages"].append(
                    {**chat["inbound_messages"][0], name: value, "message_id": uid(100)}
                )
                manifest["attempts"].append(
                    {"attempt_id": "in-2", "direction": "inbound", "intent_index": 1}
                )
                self.assertEqual(reconcile(manifest, chat, mock)["status"], "pass")

    def test_unknown_requires_declared_state_and_exact_effect_expectation(self) -> None:
        for effects in (0, 1):
            with self.subTest(effects=effects):
                manifest, chat, mock = fixture()
                manifest["outbound"][0].update(
                    expected_state="unknown", expected_effects=effects
                )
                chat["outbound_jobs"][0].update(state="unknown", effect_id=None)
                if not effects:
                    mock["effects"].clear()
                result = reconcile(manifest, chat, mock)
                self.assertEqual(result["status"], "pass")
                self.assertEqual(result["counts"]["expected_unknown"], 1)

    def test_unexpected_unknown_is_not_success(self) -> None:
        manifest, chat, mock = fixture()
        chat["outbound_jobs"][0].update(state="unknown", effect_id=None)
        self.assertEqual(reconcile(manifest, chat, mock)["status"], "failed")

    def test_unknown_does_not_hide_effect_loss_or_duplication(self) -> None:
        for actual_effects in (0, 2):
            with self.subTest(actual_effects=actual_effects):
                manifest, chat, mock = fixture()
                manifest["outbound"][0].update(expected_state="unknown")
                chat["outbound_jobs"][0].update(state="unknown", effect_id=None)
                mock["effects"] *= actual_effects
                self.assertEqual(reconcile(manifest, chat, mock)["status"], "failed")

    def test_rejected_has_no_effect(self) -> None:
        manifest, chat, mock = fixture()
        manifest["outbound"][0].update(expected_state="rejected", expected_effects=0)
        chat["outbound_jobs"][0].update(state="rejected", effect_id=None)
        mock["effects"].clear()
        self.assertEqual(reconcile(manifest, chat, mock)["status"], "pass")

    def test_pending_or_sending_at_completion_is_failure(self) -> None:
        for state in ("pending", "sending"):
            manifest, chat, mock = fixture()
            chat["outbound_jobs"][0].update(state=state, effect_id=None)
            self.assertEqual(reconcile(manifest, chat, mock)["status"], "failed")

    def test_wrong_accepted_effect_reference(self) -> None:
        manifest, chat, mock = fixture()
        chat["outbound_jobs"][0]["effect_id"] = uid(50)
        self.assertEqual(reconcile(manifest, chat, mock)["status"], "failed")

    def test_different_operations_cannot_share_effect_id(self) -> None:
        manifest, chat, mock = fixture()
        manifest["outbound"].append(
            {**manifest["outbound"][0], "outbound_operation_id": uid(50)}
        )
        manifest["attempts"].append(
            {"attempt_id": "out-2", "direction": "outbound", "intent_index": 1}
        )
        chat["outbound_jobs"].append(
            {**chat["outbound_jobs"][0], "outbound_operation_id": uid(50)}
        )
        mock["effects"].append({**mock["effects"][0], "outbound_operation_id": uid(50)})
        result = reconcile(manifest, chat, mock)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["issues"]["effect_id_reused"], 1)

    def test_incomplete_evidence_never_passes(self) -> None:
        for source in range(3):
            for field in ("complete", "synthetic"):
                evidence = fixture()
                evidence[source][field] = False
                self.assertEqual(reconcile(*evidence)["status"], "incomplete")

    def test_wrong_run_is_incomplete(self) -> None:
        evidence = fixture()
        evidence[2]["run_id"] = "another-run"
        self.assertEqual(reconcile(*evidence)["status"], "incomplete")

    def test_missing_collection_is_incomplete(self) -> None:
        evidence = fixture()
        del evidence[2]["effects"]
        self.assertEqual(reconcile(*evidence)["status"], "incomplete")

    def test_duplicate_attempt_id_is_incomplete(self) -> None:
        evidence = fixture()
        evidence[0]["attempts"].append(evidence[0]["attempts"][0])
        self.assertEqual(reconcile(*evidence)["status"], "incomplete")

    def test_missing_attempt_is_incomplete(self) -> None:
        evidence = fixture()
        evidence[0]["attempts"].clear()
        self.assertEqual(reconcile(*evidence)["status"], "incomplete")

    def test_conflicting_duplicate_intent_is_incomplete(self) -> None:
        evidence = fixture()
        evidence[0]["inbound"].append(
            {**evidence[0]["inbound"][0], "text_sha256": "a" * 64}
        )
        self.assertEqual(reconcile(*evidence)["status"], "incomplete")

    def test_raw_text_and_secret_fields_are_not_accepted_or_echoed(self) -> None:
        evidence = fixture()
        evidence[2]["effects"][0]["token"] = "synthetic-private-marker"
        result = reconcile(*evidence)
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("synthetic-private-marker", json.dumps(result))

    def test_missing_unknown_effect_expectation_is_incomplete(self) -> None:
        evidence = fixture()
        evidence[0]["outbound"][0]["expected_state"] = "unknown"
        del evidence[0]["outbound"][0]["expected_effects"]
        self.assertEqual(reconcile(*evidence)["status"], "incomplete")

    def test_invalid_shapes_fail_closed(self) -> None:
        for value in (None, [], 1, "unexpected"):
            self.assertEqual(reconcile(value, {}, {})["status"], "incomplete")

    def test_cli_report_and_nonoverwrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="chat-oracle-") as directory:
            paths = [
                Path(directory) / f"{name}.json"
                for name in ("manifest", "chat", "mock")
            ]
            for path, value in zip(paths, fixture()):
                path.write_text(json.dumps(value), encoding="utf-8")
            output = Path(directory) / "result.json"
            args = [
                "--manifest",
                str(paths[0]),
                "--chat",
                str(paths[1]),
                "--mock",
                str(paths[2]),
                "--output",
                str(output),
            ]
            with redirect_stdout(StringIO()):
                self.assertEqual(main(args), 0)
                saved = output.read_text()
                self.assertEqual(main(args), 2)
            self.assertEqual(output.read_text(), saved)
            self.assertEqual(json.loads(saved)["status"], "pass")

    def test_cli_duplicate_json_fields_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="chat-oracle-") as directory:
            path = Path(directory) / "evidence.json"
            path.write_text('{"complete":true,"complete":false}', encoding="utf-8")
            args = ["--manifest", str(path), "--chat", str(path), "--mock", str(path)]
            with redirect_stdout(StringIO()):
                self.assertEqual(main(args), 2)
                path.unlink()
                self.assertEqual(main(args), 2)


if __name__ == "__main__":
    unittest.main()
