"""Bounded synthetic seven-profile roundtrip; only runs with --execute."""

import argparse
import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from http.client import HTTPConnection, HTTPException
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import NAMESPACE_URL, uuid5

from oracle import (
    PROFILES,
    EvidenceError,
    identifier,
    reconcile,
    unique_json_object,
    uuid_value,
)

SCENARIOS = {
    "normal": ("none", True, True, "accepted", 1),
    "rate_limit": ("rate_limit", True, True, "accepted", 1),
    "unavailable": ("unavailable", True, True, "accepted", 1),
    "reject": ("reject", True, True, "rejected", 0),
    "delay_after": ("delay_after", True, True, "accepted", 1),
    "unknown": ("delay_after", False, False, "unknown", 1),
    "duplicate": ("none", True, True, "accepted", 1),
    "restart": ("delay_after", True, True, "accepted", 1),
}


def digest(text: object) -> str:
    if not isinstance(text, str):
        raise EvidenceError("missing_observed_text")
    return sha256(text.encode("utf-8")).hexdigest()


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise EvidenceError("explicit_loopback_origin_required")
    return value


@dataclass(frozen=True)
class Config:
    run_id: str
    connection_ids: dict[str, str]
    chat_control_token: str = field(repr=False)
    mock_control_token: str = field(repr=False)
    chat_url: str = "http://127.0.0.1:18082"
    mock_url: str = "http://127.0.0.1:18087"
    browser_origin: str = "http://127.0.0.1:18083"
    deadline_seconds: int = 60

    def validate(self) -> None:
        identifier(self.run_id)
        if (
            not isinstance(self.connection_ids, dict)
            or set(self.connection_ids) != PROFILES
        ):
            raise EvidenceError("seven_configured_connections_required")
        for value in self.connection_ids.values():
            uuid_value(value)
        if len(set(self.connection_ids.values())) != 7:
            raise EvidenceError("unique_connections_required")
        for value in (self.chat_url, self.mock_url, self.browser_origin):
            origin(value)
        if self.chat_url == self.mock_url:
            raise EvidenceError("separate_services_required")
        tokens = (self.chat_control_token, self.mock_control_token)
        if any(
            len(token) < 24 or not token.isascii() or any(c.isspace() for c in token)
            for token in tokens
        ):
            raise EvidenceError("strong_control_tokens_required")
        if tokens[0] == tokens[1]:
            raise EvidenceError("distinct_control_tokens_required")
        if not 5 <= self.deadline_seconds <= 120:
            raise EvidenceError("invalid_deadline")


class Client:
    """No proxy, redirects, automatic retries, cookie persistence, or raw logging."""

    def __init__(self, config: Config):
        self.config = config
        self.deadline = time.monotonic() + config.deadline_seconds
        self.cookie: str | None = None

    def request(
        self,
        service: str,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        browser: bool = False,
        expected: tuple[int, ...] = (200,),
    ) -> dict:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EvidenceError("collection_deadline")
        base = self.config.chat_url if service == "chat" else self.config.mock_url
        parsed = urlsplit(base)
        headers = {"Accept": "application/json"}
        if browser:
            if service != "chat":
                raise EvidenceError("browser_credentials_scope")
            headers["Origin"] = self.config.browser_origin
            if self.cookie:
                headers["Cookie"] = self.cookie
        elif not path.startswith("/health/"):
            token = (
                self.config.chat_control_token
                if service == "chat"
                else self.config.mock_control_token
            )
            headers["Authorization"] = "Bearer " + token
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        connection = HTTPConnection(
            parsed.hostname, parsed.port, timeout=min(5, remaining)
        )
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            if response.status not in expected:
                raise EvidenceError(f"unexpected_http_status_{response.status}")
            media_type = response.getheader("Content-Type", "").split(";", 1)[0]
            if media_type != "application/json" and not (
                response.status >= 400 and media_type == "application/problem+json"
            ):
                raise EvidenceError("json_response_required")
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise EvidenceError("response_limit")
            if browser and path == "/v1/dev/session":
                cookie = SimpleCookie()
                cookie.load(response.getheader("Set-Cookie", ""))
                if "chat_session" not in cookie:
                    raise EvidenceError("session_cookie_missing")
                self.cookie = "chat_session=" + cookie["chat_session"].value
            result = json.loads(raw, object_pairs_hook=unique_json_object)
            if not isinstance(result, dict):
                raise EvidenceError("object_response_required")
            return result
        except EvidenceError:
            raise
        except (OSError, HTTPException, ValueError):
            raise EvidenceError("transport_or_json_failure") from None
        finally:
            connection.close()


def data(response: dict) -> object:
    if "data" not in response:
        raise EvidenceError("response_data_missing")
    return response["data"]


def history(client: Client, room_id: str) -> list[dict]:
    result, cursor, head = [], 0, None
    for _ in range(10):
        query = {"after_seq": str(cursor), "limit": "100"}
        if head is not None:
            query["snapshot_head_seq"] = str(head)
        response = client.request(
            "chat",
            "GET",
            f"/v1/external-conversations/{room_id}/messages?{urlencode(query)}",
            browser=True,
        )
        rows, meta = data(response), response.get("meta")
        if not isinstance(rows, list) or not isinstance(meta, dict):
            raise EvidenceError("history_shape")
        next_cursor, current_head = (
            int(meta["next_cursor"]),
            int(meta["snapshot_head_seq"]),
        )
        if (
            type(meta["has_more"]) is not bool
            or current_head < 0
            or next_cursor < cursor
            or next_cursor > current_head
            or (head is not None and current_head != head)
        ):
            raise EvidenceError("history_cursor_invalid")
        previous = cursor
        for message in rows:
            seq = int(message["seq"])
            if (
                message["conversation_id"] != room_id
                or not previous < seq <= current_head
            ):
                raise EvidenceError("history_identity_or_order")
            previous = seq
        if previous != next_cursor:
            raise EvidenceError("history_cursor_invalid")
        result.extend(rows)
        if not meta["has_more"]:
            if next_cursor != current_head:
                raise EvidenceError("history_truncated")
            return result
        if next_cursor == cursor:
            raise EvidenceError("history_cursor_stalled")
        cursor, head = next_cursor, current_head
    raise EvidenceError("history_page_limit")


def ledger(client: Client, run_id: str) -> list[dict]:
    records, cursor = [], 0
    for _ in range(100):
        response = client.request(
            "mock", "GET", f"/control/v1/runs/{run_id}/ledger?after={cursor}&limit=100"
        )
        rows, meta = data(response), response.get("meta")
        if not isinstance(rows, list) or not isinstance(meta, dict):
            raise EvidenceError("ledger_shape")
        for record in rows:
            if type(record["cursor"]) is not int or record["cursor"] != cursor + 1:
                raise EvidenceError("ledger_cursor_invalid")
            if record["kind"] not in {
                "event",
                "effect",
                "inbound_attempt",
                "outbound_attempt",
            }:
                raise EvidenceError("ledger_kind_unknown")
            cursor += 1
            records.append(record)
        if (
            type(meta["next_cursor"]) is not int
            or meta["next_cursor"] != cursor
            or type(meta["has_more"]) is not bool
            or type(meta["active"]) is not int
            or meta["active"] < 0
        ):
            raise EvidenceError("ledger_meta_invalid")
        if not meta["has_more"] and meta["active"] == 0:
            counts = {
                kind: sum(row["kind"] == kind for row in records)
                for kind in ("effect", "event", "inbound_attempt", "outbound_attempt")
            }
            if (
                meta["effects"] != counts["effect"]
                or meta["events"] != counts["event"]
                or meta["attempts"]
                != counts["inbound_attempt"] + counts["outbound_attempt"]
            ):
                raise EvidenceError("ledger_count_mismatch")
            return records
        if meta["has_more"] and not rows:
            raise EvidenceError("ledger_cursor_stalled")
        if not meta["has_more"]:
            time.sleep(0.1)
    raise EvidenceError("ledger_collection_limit")


def collect(
    config: Config,
    client: Client,
    manifest: dict,
    chat: dict,
    mock: dict,
    *,
    scenario: str = "normal",
    checks: list | None = None,
    output_dir: Path | None = None,
) -> None:
    fault, idempotency, lookup, expected_state, expected_effects = SCENARIOS[scenario]
    checks = [] if checks is None else checks
    for service in ("chat", "mock"):
        if client.request(service, "GET", "/health/ready") != {"status": "ready"}:
            raise EvidenceError("service_not_ready")
    actor = data(
        client.request(
            "chat", "POST", "/v1/dev/session", {"user": "user_a"}, browser=True
        )
    )
    uuid_value(actor["user_id"])
    rooms, events, commands = {}, [], []
    for profile in sorted(PROFILES):
        route = {
            "profile": profile,
            "connection_id": config.connection_ids[profile],
            "external_conversation_id": "room-1",
        }
        seed = {
            **route,
            "run_id": config.run_id,
            "external_sender_id": "customer-1",
            "operator_user_id": actor["user_id"],
            "idempotency_supported": idempotency,
            "lookup_supported": lookup,
        }
        room = data(
            client.request(
                "chat", "POST", "/v1/dev/external-connections", seed, expected=(201,)
            )
        )
        uuid_value(room["conversation_id"])
        rooms[profile] = room["conversation_id"]
        text = f"Synthetic {profile} question {config.run_id}"
        event = {
            **route,
            "schema_version": 1,
            "run_id": config.run_id,
            "external_sender_id": "customer-1",
            "external_message_id": "message-1",
            "external_event_id": f"event-{profile}",
            "occurred_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "text": text,
        }
        events.append(event)
        reply = f"Synthetic {profile} reply {config.run_id}"
        commands.append(
            {
                "client_message_id": str(
                    uuid5(NAMESPACE_URL, f"{config.run_id}:{profile}:reply")
                ),
                "text": reply,
            }
        )
        manifest["inbound"].append(
            {
                **route,
                "external_message_id": "message-1",
                "external_sender_id": "customer-1",
                "text_sha256": digest(text),
            }
        )
        manifest["outbound"].append(
            {
                **route,
                "outbound_operation_id": None,
                "text_sha256": digest(reply),
                "expected_state": expected_state,
                "expected_effects": expected_effects,
            }
        )
    if len(set(rooms.values())) != 7:
        raise EvidenceError("seed_room_collision")
    run = data(
        client.request(
            "mock",
            "POST",
            "/control/v1/runs",
            {
                "run_id": config.run_id,
                "fault": fault,
                "idempotency_supported": idempotency,
                "lookup_supported": lookup,
                "delay_seconds": 10 if scenario == "restart" else 3,
                "max_events": 30,
                "max_attempts": 100,
                "max_effects": 14,
            },
            expected=(201,),
        )
    )
    if run["run_id"] != config.run_id or run["fault"] != fault:
        raise EvidenceError("mock_run_mismatch")
    for index, event in enumerate(events):
        registered = data(
            client.request(
                "mock",
                "POST",
                f"/control/v1/runs/{config.run_id}/events",
                event,
                expected=(201,),
            )
        )
        if registered != event:
            raise EvidenceError("event_registration_mismatch")
        copies = 2 if scenario == "duplicate" else 1
        for copy in range(copies):
            manifest["attempts"].append(
                {
                    "attempt_id": f"in-{index}-{copy}",
                    "direction": "inbound",
                    "intent_index": index,
                }
            )

        def deliver_once(_: int, event_id: str = event["external_event_id"]) -> dict:
            return data(
                client.request(
                    "mock",
                    "POST",
                    f"/control/v1/runs/{config.run_id}/events/{event_id}/deliver",
                )
            )

        if copies == 2:
            with ThreadPoolExecutor(max_workers=2) as pool:
                deliveries = list(pool.map(deliver_once, range(copies)))
        else:
            deliveries = [deliver_once(0)]
        for delivered in deliveries:
            if delivered.get("result") != "acknowledged" or delivered.get(
                "http_status"
            ) not in (200, 201):
                raise EvidenceError("inbound_delivery_not_acknowledged")
        if scenario == "duplicate":
            check_rejected_events(config, client, event, checks)
        room_id = rooms[event["profile"]]
        if len(history(client, room_id)) != 1:
            raise EvidenceError("initial_history_not_single_message")
        manifest["attempts"].append(
            {
                "attempt_id": f"out-{index}",
                "direction": "outbound",
                "intent_index": index,
            }
        )
        reply = data(
            client.request(
                "chat",
                "POST",
                f"/v1/external-conversations/{room_id}/messages",
                commands[index],
                browser=True,
                expected=(201,),
            )
        )
        uuid_value(reply["operation_id"])
        if (
            reply["client_message_id"] != commands[index]["client_message_id"]
            or reply["conversation_id"] != room_id
        ):
            raise EvidenceError("outbound_ack_identity")
        manifest["outbound"][index]["outbound_operation_id"] = reply["operation_id"]
        if scenario == "restart" and index == 0:
            if output_dir is None:
                raise EvidenceError("restart_artifact_path_required")
            restart_checkpoint(
                config, client, room_id, reply["operation_id"], output_dir, checks
            )
    manifest["complete"] = True

    while True:
        snapshots = {
            profile: history(client, room_id) for profile, room_id in rooms.items()
        }
        outgoing = [
            message
            for rows in snapshots.values()
            for message in rows
            if message["sender_kind"] == "operator"
        ]
        if len(outgoing) != 7:
            raise EvidenceError("outbound_history_count")
        states = {message["delivery_state"] for message in outgoing}
        if states <= {expected_state}:
            break
        if not states <= {"pending", "sending", expected_state}:
            raise EvidenceError("unexpected_outbound_terminal_state")
        time.sleep(0.1)

    actual_rooms = data(
        client.request("chat", "GET", "/v1/external-conversations", browser=True)
    )
    actual_rooms = [
        room
        for room in actual_rooms
        if room["connection_id"] in config.connection_ids.values()
    ]
    if len(actual_rooms) != 7:
        raise EvidenceError("room_observation_count")
    for room in actual_rooms:
        route = {
            name: room[name]
            for name in ("profile", "connection_id", "external_conversation_id")
        }
        if (
            rooms.get(room["profile"]) != room["conversation_id"]
            or config.connection_ids.get(room["profile"]) != room["connection_id"]
            or room["external_conversation_id"] != "room-1"
        ):
            raise EvidenceError("room_route_mismatch")
        for message in snapshots[room["profile"]]:
            if message["sender_kind"] == "customer":
                chat["inbound_messages"].append(
                    {
                        **route,
                        "external_message_id": message["external_message_id"],
                        "external_sender_id": message["external_sender_id"],
                        "text_sha256": digest(message["text"]),
                        "message_id": message["message_id"],
                        "seq": message["seq"],
                    }
                )
            elif message["sender_kind"] == "operator":
                chat["outbound_jobs"].append(
                    {
                        **route,
                        "outbound_operation_id": message["operation_id"],
                        "text_sha256": digest(message["text"]),
                        "state": message["delivery_state"],
                        "effect_id": message["effect_id"],
                    }
                )
            else:
                raise EvidenceError("unknown_sender_kind")
    chat["complete"] = True
    records = ledger(client, config.run_id)
    if output_dir is not None:
        safe_fields = {
            "cursor",
            "kind",
            "external_event_id",
            "outbound_operation_id",
            "attempt",
            "result",
            "http_status",
            "applied_fault",
            "elapsed_seconds",
            "injected_delay_seconds",
        }
        with (output_dir / "transport-attempts.json").open(
            "x", encoding="utf-8"
        ) as target:
            json.dump(
                [
                    {key: value for key, value in record.items() if key in safe_fields}
                    for record in records
                    if record["kind"].endswith("attempt")
                ],
                target,
                indent=2,
            )
    inspect_attempts(records, scenario, checks)
    for record in records:
        if record["kind"] == "effect":
            command, effect = record["command"], record["effect"]
            if (
                command["run_id"] != config.run_id
                or effect["state"] != "accepted"
                or effect["outbound_operation_id"] != command["outbound_operation_id"]
            ):
                raise EvidenceError("effect_record_mismatch")
            mock["effects"].append(
                {
                    **{
                        name: command[name]
                        for name in (
                            "profile",
                            "connection_id",
                            "external_conversation_id",
                            "outbound_operation_id",
                        )
                    },
                    "text_sha256": digest(command["text"]),
                    "effect_id": effect["effect_id"],
                }
            )
    mock["complete"] = True


def inspect_attempts(records: list, scenario: str, checks: list) -> None:
    incoming = [record for record in records if record["kind"] == "inbound_attempt"]
    outgoing = [record for record in records if record["kind"] == "outbound_attempt"]
    fault = SCENARIOS[scenario][0]
    fault_rows = (
        [record for record in outgoing if record.get("applied_fault") == fault]
        if fault != "none"
        else []
    )
    expected_inbound = 28 if scenario == "duplicate" else 7
    expected_outbound = 14 if scenario in {"rate_limit", "unavailable"} else 7
    passed = len(incoming) == expected_inbound and len(outgoing) == expected_outbound
    operations = Counter(record.get("outbound_operation_id") for record in outgoing)
    per_operation = 2 if scenario in {"rate_limit", "unavailable"} else 1
    passed = (
        passed
        and len(operations) == 7
        and None not in operations
        and all(count == per_operation for count in operations.values())
    )
    if fault != "none":
        passed = passed and len(fault_rows) == 7
        covered = Counter(record.get("outbound_operation_id") for record in fault_rows)
        passed = (
            passed
            and set(covered) == set(operations)
            and all(count == 1 for count in covered.values())
            and all(record.get("attempt") == 1 for record in fault_rows)
        )
        if fault in {"rate_limit", "unavailable", "reject"}:
            passed = passed and all(
                record["result"] == "INJECTED_FAILURE" for record in fault_rows
            )
        if fault == "delay_after":
            minimum = 9.5 if scenario == "restart" else 2.5
            passed = passed and all(
                isinstance(record.get("elapsed_seconds"), (int, float))
                and record["elapsed_seconds"] >= minimum
                for record in fault_rows
            )
    checks.append(
        {
            "case": "transport_attempts",
            "actual_inbound_attempts": len(incoming),
            "actual_outbound_attempts": len(outgoing),
            "expected_inbound_attempts": expected_inbound,
            "expected_outbound_attempts": expected_outbound,
            "observed_fault_attempts": len(fault_rows),
            "passed": passed,
        }
    )
    if not passed:
        raise EvidenceError("transport_fault_evidence_mismatch")


def check_rejected_events(
    config: Config, client: Client, event: dict, checks: list
) -> None:
    for case, expected_status, altered in (
        ("payload_conflict", 409, {"text": "Synthetic conflicting payload"}),
        (
            "different_connection_profile",
            403,
            {
                "profile": next(
                    profile
                    for profile in sorted(PROFILES)
                    if profile != event["profile"]
                )
            },
        ),
    ):
        negative = {
            **event,
            **altered,
            "external_event_id": event["external_event_id"] + "-" + case,
        }
        registered = data(
            client.request(
                "mock",
                "POST",
                f"/control/v1/runs/{config.run_id}/events",
                negative,
                expected=(201,),
            )
        )
        if registered != negative:
            raise EvidenceError("negative_registration_mismatch")
        observed = data(
            client.request(
                "mock",
                "POST",
                f"/control/v1/runs/{config.run_id}/events/{negative['external_event_id']}/deliver",
            )
        )
        passed = (
            observed.get("result") == "rejected"
            and observed.get("http_status") == expected_status
        )
        checks.append(
            {
                "case": case,
                "expected_http_status": expected_status,
                "actual_http_status": observed.get("http_status"),
                "passed": passed,
            }
        )
        if not passed:
            raise EvidenceError("negative_event_not_rejected")


def restart_checkpoint(
    config: Config,
    client: Client,
    room_id: str,
    operation: str,
    output_dir: Path,
    checks: list,
) -> None:
    ready, resume = output_dir / "pause-ready.json", output_dir / "resume.json"
    if ready.exists() or resume.exists():
        raise EvidenceError("restart_signal_already_exists")
    for _ in range(50):
        snapshot = history(client, room_id)
        job = next(
            (message for message in snapshot if message["operation_id"] == operation),
            None,
        )
        records = client.request(
            "mock", "GET", f"/control/v1/runs/{config.run_id}/ledger?after=0&limit=100"
        )
        effect_present = any(
            record["kind"] == "effect"
            and record["command"]["outbound_operation_id"] == operation
            for record in data(records)
        )
        if (
            job
            and job["delivery_state"] == "sending"
            and effect_present
            and records["meta"]["active"] > 0
        ):
            with ready.open("x", encoding="utf-8") as target:
                json.dump(
                    {
                        "run_id": config.run_id,
                        "operation_id": operation,
                        "observed_state": "sending",
                        "mock_effect_present": True,
                        "ready_at": datetime.now(timezone.utc).isoformat(),
                    },
                    target,
                )
            break
        if job and job["delivery_state"] in {"accepted", "rejected", "unknown"}:
            raise EvidenceError("restart_window_missed")
        time.sleep(0.05)
    else:
        raise EvidenceError("restart_checkpoint_unavailable")
    until = time.monotonic() + 120
    while not resume.exists():
        if time.monotonic() >= until:
            raise EvidenceError("restart_resume_deadline")
        time.sleep(0.1)
    marker = json.loads(resume.read_text(), object_pairs_hook=unique_json_object)
    if marker != {"restart_confirmed": True}:
        raise EvidenceError("invalid_restart_confirmation")
    client.deadline = time.monotonic() + config.deadline_seconds
    # 이전 세션이 살아 있으면 재시작을 입증하지 못했습니다. 예외를 성공으로 바꾸지 않습니다.
    client.request("chat", "GET", "/v1/session", browser=True, expected=(401,))
    client.cookie = None
    actor = data(
        client.request(
            "chat", "POST", "/v1/dev/session", {"user": "user_a"}, browser=True
        )
    )
    uuid_value(actor["user_id"])
    checks.append(
        {
            "case": "chat_restart",
            "old_session_rejected": True,
            "new_session_created": True,
            "passed": True,
        }
    )


def run(
    config: Config,
    output_dir: Path,
    client: Client | None = None,
    *,
    scenario: str = "normal",
) -> dict:
    config.validate()
    if scenario not in SCENARIOS:
        raise EvidenceError("unknown_scenario")
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    common = {
        "schema_version": 1,
        "run_id": config.run_id,
        "synthetic": True,
        "complete": False,
    }
    manifest = {**common, "inbound": [], "outbound": [], "attempts": []}
    chat = {**common, "inbound_messages": [], "outbound_jobs": []}
    mock = {**common, "effects": []}
    checks = []
    try:
        collect(
            config,
            client or Client(config),
            manifest,
            chat,
            mock,
            scenario=scenario,
            checks=checks,
            output_dir=output_dir,
        )
        result = reconcile(manifest, chat, mock)
    except EvidenceError as exc:
        result = {"schema_version": 1, "status": "incomplete", "issues": {str(exc): 1}}
    except (KeyError, TypeError, ValueError, AttributeError):
        result = {
            "schema_version": 1,
            "status": "incomplete",
            "issues": {"invalid_service_evidence": 1},
        }
    for name, value in (
        ("manifest", manifest),
        ("chat", chat),
        ("mock", mock),
        ("result", result),
        ("checks", {"scenario": scenario, "checks": checks}),
    ):
        with (output_dir / f"{name}.json").open("x", encoding="utf-8") as target:
            json.dump(value, target, indent=2, sort_keys=True)
            target.write("\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Authorize these bounded synthetic writes to already-running local services",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument(
        "--output-dir", required=True, type=Path, help="New directory only"
    )
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("--execute is required; no services are started by this tool")
    try:
        connection_ids = (
            json.loads(
                os.environ["CHAT_TEST_CONNECTION_IDS"],
                object_pairs_hook=unique_json_object,
            )
            if "CHAT_TEST_CONNECTION_IDS" in os.environ
            else {
                profile: str(
                    uuid5(
                        NAMESPACE_URL,
                        f"laughtale:external-smoke:{args.run_id}:{profile}",
                    )
                )
                for profile in PROFILES
            }
        )
        config = Config(
            args.run_id,
            connection_ids,
            os.environ["CHAT_CONTROL_TOKEN"],
            os.environ["MOCK_CONTROL_TOKEN"],
            os.environ.get("CHAT_BASE_URL", "http://127.0.0.1:18082"),
            os.environ.get("MOCK_BASE_URL", "http://127.0.0.1:18087"),
            os.environ.get("CHAT_TEST_ORIGIN", "http://127.0.0.1:18083"),
        )
        result = run(config, args.output_dir, scenario=args.scenario)
    except (EvidenceError, KeyError, ValueError, OSError):
        result = {
            "schema_version": 1,
            "status": "incomplete",
            "issues": {"configuration_or_artifact_error": 1},
        }
    print(json.dumps(result, sort_keys=True))
    return {"pass": 0, "failed": 1, "incomplete": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
