"""Collector contract tests use in-memory HTTP responses, never running services."""

import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from collect import Client, Config, history, inspect_attempts, restart_checkpoint, run
from oracle import PROFILES, EvidenceError


def uid(value: str) -> str:
    return str(uuid5(NAMESPACE_URL, value))


def config() -> Config:
    return Config(
        "collector-synthetic-1",
        {profile: uid(profile) for profile in PROFILES},
        "chat-control-synthetic-value",
        "mock-control-synthetic-value",
    )


class ResponseClient:
    """Small response fixture, not an alternative implementation of Chat or Mock."""

    def __init__(self):
        self.routes = {}
        self.messages = {}
        self.events = {}
        self.records = []
        self.calls = []

    def record(self, kind, **values):
        self.records.append({"cursor": len(self.records) + 1, "kind": kind, **values})

    def request(
        self, service, method, path, body=None, *, browser=False, expected=(200,)
    ):
        self.calls.append((service, method, path))
        if len(self.calls) > 150:
            raise EvidenceError("collection_deadline")
        if path == "/health/ready":
            return {"status": "ready"}
        if path == "/v1/dev/session":
            return {
                "data": {
                    "user_id": "00000000-0000-4000-8000-000000000001",
                    "display_name": "synthetic",
                }
            }
        if path == "/v1/dev/external-connections":
            room_id = uid("room-" + body["profile"])
            self.routes[room_id] = {
                name: body[name]
                for name in ("profile", "connection_id", "external_conversation_id")
            }
            self.messages[room_id] = []
            return {"data": {"conversation_id": room_id}}
        if path == "/control/v1/runs":
            return {"data": body}
        if path.endswith("/events"):
            self.events[body["external_event_id"]] = body
            self.record("event", event=body)
            return {"data": body}
        if path.endswith("/deliver"):
            event_id = path.split("/")[-2]
            event = self.events[event_id]
            room_id = uid("room-" + event["profile"])
            self.messages[room_id].append(
                {
                    "message_id": uid(event_id),
                    "conversation_id": room_id,
                    "sender_kind": "customer",
                    "sender_id": uid("participant"),
                    "external_sender_id": event["external_sender_id"],
                    "external_message_id": event["external_message_id"],
                    "seq": "1",
                    "text": event["text"],
                    "operation_id": None,
                    "effect_id": None,
                    "delivery_state": None,
                }
            )
            self.record(
                "inbound_attempt",
                external_event_id=event_id,
                attempt=1,
                result="acknowledged",
                http_status=201,
            )
            return {
                "data": {"result": "acknowledged", "http_status": 201, "attempt": 1}
            }
        if path.startswith("/v1/external-conversations/"):
            room_id = path.split("/")[3]
            if method == "POST":
                operation, effect_id = (
                    uid("operation-" + room_id),
                    uid("effect-" + room_id),
                )
                message = {
                    "message_id": uid("reply-" + room_id),
                    "conversation_id": room_id,
                    "sender_kind": "operator",
                    "sender_id": "00000000-0000-4000-8000-000000000001",
                    "external_sender_id": None,
                    "external_message_id": None,
                    "seq": "2",
                    "text": body["text"],
                    "client_message_id": body["client_message_id"],
                    "operation_id": operation,
                    "effect_id": effect_id,
                    "delivery_state": "accepted",
                }
                self.messages[room_id].append(message)
                self.record(
                    "effect",
                    command={
                        **self.routes[room_id],
                        "run_id": config().run_id,
                        "outbound_operation_id": operation,
                        "text": body["text"],
                    },
                    effect={
                        "state": "accepted",
                        "effect_id": effect_id,
                        "outbound_operation_id": operation,
                    },
                )
                self.record(
                    "outbound_attempt",
                    outbound_operation_id=operation,
                    attempt=1,
                    result="effect_created",
                )
                return {"data": deepcopy(message)}
            rows = deepcopy(self.messages[room_id])
            return {
                "data": rows,
                "meta": {
                    "next_cursor": str(len(rows)),
                    "snapshot_head_seq": str(len(rows)),
                    "has_more": False,
                },
            }
        if path == "/v1/external-conversations":
            return {
                "data": [
                    {
                        **route,
                        "conversation_id": room_id,
                        "title": "synthetic",
                        "head_seq": str(len(self.messages[room_id])),
                    }
                    for room_id, route in self.routes.items()
                ]
            }
        if "/ledger?" in path:
            return {
                "data": deepcopy(self.records),
                "meta": {
                    "next_cursor": len(self.records),
                    "has_more": False,
                    "active": 0,
                    "events": sum(row["kind"] == "event" for row in self.records),
                    "effects": sum(row["kind"] == "effect" for row in self.records),
                    "attempts": sum(
                        row["kind"].endswith("attempt") for row in self.records
                    ),
                },
            }
        raise AssertionError("Unexpected fixture request")


class CollectorTests(unittest.TestCase):
    def collect(self, client, scenario="normal") -> tuple[dict, dict]:
        with tempfile.TemporaryDirectory(prefix="chat-collector-") as directory:
            output = Path(directory) / "run"
            result = run(config(), output, client, scenario=scenario)
            artifacts = {
                path.stem: json.loads(path.read_text())
                for path in output.glob("*.json")
            }
            return result, artifacts

    def test_seven_profile_roundtrip_from_independent_responses(self):
        client = ResponseClient()
        result, artifacts = self.collect(client)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["counts"]["inbound_intents"], 7)
        self.assertEqual(result["counts"]["provider_effects"], 7)
        serialized = json.dumps(artifacts)
        self.assertNotIn(config().chat_control_token, serialized)
        self.assertNotIn(config().mock_control_token, serialized)
        self.assertNotIn("Synthetic telegram question", serialized)

    def test_http_200_with_nested_403_delivery_is_not_success(self):
        client = ResponseClient()
        request = client.request

        def rejected(service, method, path, *args, **kwargs):
            if path.endswith("/deliver"):
                return {
                    "data": {"result": "rejected", "http_status": 403, "attempt": 1}
                }
            return request(service, method, path, *args, **kwargs)

        client.request = rejected
        result, artifacts = self.collect(client)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("inbound_delivery_not_acknowledged", result["issues"])
        self.assertFalse(artifacts["chat"]["complete"])

    def test_missing_sender_field_cannot_be_filled_from_manifest(self):
        client = ResponseClient()
        request = client.request

        def missing(service, method, path, *args, **kwargs):
            response = request(service, method, path, *args, **kwargs)
            if "/messages?" in path:
                for message in response["data"]:
                    message.pop("external_sender_id", None)
            return response

        client.request = missing
        result, _ = self.collect(client)
        self.assertEqual(result["status"], "incomplete")

    def test_effect_body_corruption_is_detected(self):
        client = ResponseClient()
        request = client.request

        def corrupt(service, method, path, *args, **kwargs):
            response = request(service, method, path, *args, **kwargs)
            if "/ledger?" in path:
                for record in response["data"]:
                    if record["kind"] == "effect":
                        record["command"]["text"] = "synthetic corruption"
                        break
            return response

        client.request = corrupt
        result, _ = self.collect(client)
        self.assertEqual(result["status"], "failed")

    def test_ledger_wrong_cursor_cannot_pass(self):
        client = ResponseClient()
        request = client.request

        def corrupt(service, method, path, *args, **kwargs):
            response = request(service, method, path, *args, **kwargs)
            if "/ledger?" in path:
                response["data"][0]["cursor"] = 2
            return response

        client.request = corrupt
        result, _ = self.collect(client)
        self.assertEqual(result["status"], "incomplete")

    def test_forbidden_unauthorized_and_redirect_are_errors(self):
        for status in (401, 403, 302, 307, 503):
            with (
                self.subTest(status=status),
                patch("collect.HTTPConnection") as connection,
            ):
                connection.return_value.getresponse.return_value.status = status
                with self.assertRaisesRegex(
                    EvidenceError, f"unexpected_http_status_{status}"
                ):
                    Client(config()).request(
                        "chat", "POST", "/v1/dev/external-connections", {}
                    )
                connection.return_value.close.assert_called_once()

    def test_auth_token_and_browser_cookie_are_separate(self):
        client = Client(config())
        client.cookie = "chat_session=synthetic-cookie"
        with patch("collect.HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.getheader.return_value = "application/json"
            response.read.return_value = b'{"data": []}'
            client.request("mock", "GET", "/control/v1/runs/run/ledger")
            headers = connection.return_value.request.call_args.kwargs["headers"]
            self.assertNotIn("Cookie", headers)
            self.assertNotIn("Origin", headers)
            client.request("chat", "GET", "/v1/external-conversations", browser=True)
            headers = connection.return_value.request.call_args.kwargs["headers"]
            self.assertNotIn("Authorization", headers)
            self.assertEqual(headers["Cookie"], client.cookie)

    def test_history_truncated_complete_page_is_incomplete(self):
        client = ResponseClient()
        client.request = lambda *args, **kwargs: {
            "data": [],
            "meta": {"next_cursor": "0", "snapshot_head_seq": "1", "has_more": False},
        }
        with self.assertRaisesRegex(EvidenceError, "history_truncated"):
            history(client, uid("room"))

    def test_missing_configuration_prevents_any_requests(self):
        bad = replace(config(), connection_ids={})
        client = ResponseClient()
        with (
            tempfile.TemporaryDirectory(prefix="chat-collector-") as directory,
            self.assertRaises(EvidenceError),
        ):
            run(bad, Path(directory) / "run", client)
        self.assertEqual(client.calls, [])

    def test_existing_output_directory_prevents_writes_and_requests(self):
        client = ResponseClient()
        with (
            tempfile.TemporaryDirectory(prefix="chat-collector-") as directory,
            self.assertRaises(FileExistsError),
        ):
            run(config(), Path(directory), client)
        self.assertEqual(client.calls, [])

    def test_fault_scenarios_have_explicit_terminal_expectations(self):
        for scenario, state in (
            ("rate_limit", "accepted"),
            ("unavailable", "accepted"),
            ("delay_after", "accepted"),
            ("unknown", "unknown"),
            ("reject", "rejected"),
        ):
            with self.subTest(scenario=scenario):
                client = ResponseClient()
                original = client.request

                def response(
                    service,
                    method,
                    path,
                    *args,
                    state=state,
                    scenario=scenario,
                    original=original,
                    client=client,
                    **kwargs,
                ):
                    result = original(service, method, path, *args, **kwargs)
                    if (
                        path.startswith("/v1/external-conversations/")
                        and method == "POST"
                    ):
                        room_id = path.split("/")[3]
                        message = client.messages[room_id][-1]
                        message["delivery_state"] = state
                        if state != "accepted":
                            message["effect_id"] = None
                        if scenario in {"rate_limit", "unavailable"}:
                            client.records[-1]["attempt"] = 2
                            client.records.insert(
                                len(client.records) - 2,
                                {
                                    "cursor": 0,
                                    "kind": "outbound_attempt",
                                    "outbound_operation_id": message["operation_id"],
                                    "attempt": 1,
                                    "result": "INJECTED_FAILURE",
                                    "applied_fault": scenario,
                                    "elapsed_seconds": 0.01,
                                },
                            )
                        else:
                            client.records[-1].update(
                                applied_fault="reject"
                                if state == "rejected"
                                else "delay_after",
                                elapsed_seconds=3.0,
                            )
                            if state == "rejected":
                                client.records[-1]["result"] = "INJECTED_FAILURE"
                        if state == "rejected":
                            client.records = [
                                row
                                for row in client.records
                                if not (
                                    row["kind"] == "effect"
                                    and row["command"]["outbound_operation_id"]
                                    == message["operation_id"]
                                )
                            ]
                        for index, row in enumerate(client.records, 1):
                            row["cursor"] = index
                        return {"data": deepcopy(message)}
                    return result

                client.request = response
                result, artifacts = self.collect(client, scenario)
                self.assertEqual(result["status"], "pass")
                self.assertEqual(
                    artifacts["manifest"]["outbound"][0]["expected_state"], state
                )

    def test_duplicate_and_negative_checks_keep_one_logical_message(self):
        client = ResponseClient()
        original, seen = client.request, set()

        def response(service, method, path, *args, **kwargs):
            if path.endswith("/deliver"):
                event_id = path.split("/")[-2]
                expected = (
                    409
                    if event_id.endswith("payload_conflict")
                    else 403
                    if event_id.endswith("different_connection_profile")
                    else None
                )
                if expected is not None or event_id in seen:
                    status = expected or 200
                    result = "rejected" if expected else "acknowledged"
                    client.record(
                        "inbound_attempt",
                        external_event_id=event_id,
                        attempt=2,
                        result=result,
                        http_status=status,
                    )
                    return {
                        "data": {"result": result, "http_status": status, "attempt": 2}
                    }
                seen.add(event_id)
            return original(service, method, path, *args, **kwargs)

        client.request = response
        result, artifacts = self.collect(client, "duplicate")
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["counts"]["inbound_intents"], 7)
        self.assertEqual(result["counts"]["attempts"], 21)
        self.assertEqual(len(artifacts["checks"]["checks"]), 15)
        self.assertTrue(all(check["passed"] for check in artifacts["checks"]["checks"]))

    def test_restart_handshake_requires_old_session_rejection(self):
        client = ResponseClient()
        operation = uid("restart-operation")
        checks = []

        def request(service, method, path, *args, **kwargs):
            if "/ledger?" in path:
                return {
                    "data": [
                        {
                            "kind": "effect",
                            "command": {"outbound_operation_id": operation},
                        }
                    ],
                    "meta": {"active": 1},
                }
            if path == "/v1/session":
                self.assertEqual(kwargs["expected"], (401,))
                return {"code": "UNAUTHENTICATED"}
            if path == "/v1/dev/session":
                return {"data": {"user_id": uid("user")}}
            raise AssertionError("Unexpected request")

        client.request = request
        with tempfile.TemporaryDirectory(prefix="chat-restart-test-") as directory:
            output = Path(directory)

            def signal_resume(_):
                self.assertTrue((output / "pause-ready.json").exists())
                (output / "resume.json").write_text('{"restart_confirmed":true}')

            with (
                patch(
                    "collect.history",
                    return_value=[
                        {"operation_id": operation, "delivery_state": "sending"}
                    ],
                ),
                patch("collect.time.sleep", side_effect=signal_resume),
            ):
                restart_checkpoint(
                    config(), client, uid("room"), operation, output, checks
                )
            self.assertTrue(checks[0]["old_session_rejected"])

    def test_fault_success_without_fault_evidence_is_incomplete(self):
        rows = [{"kind": "inbound_attempt"} for _ in range(7)] + [
            {"kind": "outbound_attempt", "result": "effect_created"} for _ in range(7)
        ]
        with self.assertRaisesRegex(EvidenceError, "transport_fault_evidence_mismatch"):
            inspect_attempts(rows, "delay_after", [])

    def test_fault_label_without_real_delay_is_not_proof(self):
        rows = [{"kind": "inbound_attempt"} for _ in range(7)] + [
            {
                "kind": "outbound_attempt",
                "result": "effect_created",
                "applied_fault": "delay_after",
                "elapsed_seconds": 0.01,
            }
            for _ in range(7)
        ]
        with self.assertRaisesRegex(EvidenceError, "transport_fault_evidence_mismatch"):
            inspect_attempts(rows, "delay_after", [])


if __name__ == "__main__":
    unittest.main()
