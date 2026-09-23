"""격리 K8s machine API의 bounded 합성 수신 발생기입니다. DB 증빙은 별도입니다."""

import argparse
import concurrent.futures
import json
import math
import hashlib
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
from uuid import UUID

PROFILES = {"telegram", "line", "instagram", "facebook", "whatsapp", "wechat", "kakao-bizgo"}
CHAT = "http://chat:18082"
MAX_BODY = 16384


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(path: str, body: dict, token: str) -> dict:
    """요청당 새 연결로 Service 분산을 관찰하며 token·본문은 결과에 넣지 않습니다."""
    started = time.monotonic()
    encoded = json.dumps(body, ensure_ascii=False).encode()
    if len(encoded) > MAX_BODY:
        raise ValueError("BODY_LIMIT")
    request = urllib.request.Request(CHAT + path, data=encoded, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token,
        "Connection": "close",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    result = {"http_status": None, "request_id": None, "outcome": "unknown"}
    try:
        with opener.open(request, timeout=2) as response:
            raw = response.read(MAX_BODY + 1)
            result.update(http_status=response.status, request_id=response.headers.get("x-request-id"))
            if len(raw) > MAX_BODY:
                result["outcome"] = "response_too_large"
            else:
                payload = json.loads(raw)
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                    result["outcome"] = "invalid_response"
                else:
                    result["outcome"] = "acknowledged" if response.status in (200, 201) else "unknown"
                    data = payload["data"]
                    for field in ("message_id", "conversation_id", "seq", "external_message_id"):
                        if field in data:
                            result[field] = data[field]
    except urllib.error.HTTPError as error:
        result.update(http_status=error.code, request_id=error.headers.get("x-request-id"), outcome="rejected" if 400 <= error.code < 500 else "unknown")
    except (OSError, ValueError, urllib.error.URLError):
        pass
    result["latency_seconds"] = time.monotonic() - started
    return result


def credentials() -> list[dict]:
    raw = json.loads(os.environ["G4_CONNECTION_CREDENTIALS"])
    if not isinstance(raw, dict) or len(raw) != 7:
        raise ValueError("EXACTLY_SEVEN_CONNECTIONS_REQUIRED")
    items = []
    for key, value in raw.items():
        if str(UUID(key)) != key or not isinstance(value, dict):
            raise ValueError("INVALID_CONNECTION")
        token = value["token"]
        if not isinstance(token, str) or not 16 <= len(token) <= 256 or not all(33 <= ord(char) <= 126 for char in token):
            raise ValueError("INVALID_CONNECTION_TOKEN")
        items.append({"connection_id": key, "profile": value["profile"], "token": token})
    if {item["profile"] for item in items} != PROFILES:
        raise ValueError("SEVEN_PROFILES_REQUIRED")
    return sorted(items, key=lambda item: item["profile"])


def make_events(run_id: str, phase: str, count: int, connections: list[dict]) -> list[tuple[dict, str]]:
    events = []
    for index in range(count):
        connection = connections[index % len(connections)]
        event = {
            "schema_version": 1, "run_id": run_id, "profile": connection["profile"],
            "connection_id": connection["connection_id"], "external_conversation_id": "g4-room",
            "external_event_id": f"{phase}-event-{index}", "external_message_id": f"{phase}-message-{index}",
            "external_sender_id": "g4-sender", "occurred_at": "2026-09-08T00:00:00Z",
            "text": f"Synthetic G4 {phase} message {index}",
        }
        events.append((event, connection["token"]))
    return events


def summarize(records: list[dict], expected: list[tuple[dict, str]]) -> dict:
    acknowledgements: dict[str, set[tuple]] = {}
    for record in records:
        if record["outcome"] == "acknowledged":
            acknowledgements.setdefault(record["event_id"], set()).add((record.get("message_id"), record.get("conversation_id"), record.get("seq")))
    unique = {event["external_event_id"] for event, _ in expected}
    return {
        "planned_unique_events": len(unique), "attempt_records": len(records),
        "acknowledged_unique_events": len(acknowledgements),
        "events_without_ack": sorted(unique - acknowledgements.keys()),
        "conflicting_ack_identities": sum(len(values) != 1 for values in acknowledgements.values()),
        "skipped_at_capacity": sum(record["outcome"] == "skipped_at_capacity" for record in records),
        "unknown_attempts": sum(record["outcome"] == "unknown" for record in records),
        "db_loss_validation": "pending_root_db_reconciliation",
        "pod_distribution_validation": "pending_request_id_to_pod_log_join",
    }


def audit_attempts(plan: list[dict], records: list[dict]) -> dict:
    """계획 원장과 실제 시도를 대조합니다. 고유 이벤트 성공으로 replay 누락을 숨기지 않습니다."""
    planned = {item["attempt_id"]: item for item in plan}
    seen = set()
    mismatched = 0
    duplicate_executed = 0
    observed_failures = 0
    for record in records:
        attempt_id = record.get("attempt_id")
        item = planned.get(attempt_id)
        if item is None or attempt_id in seen or any(record.get(key) != item[key] for key in ("event_id", "connection_id", "external_message_id", "duplicate", "text_sha256", "external_sender_id", "external_conversation_id", "profile")):
            mismatched += 1
            continue
        seen.add(attempt_id)
        actual, completed = record.get("actual_start_seconds"), record.get("completed_seconds")
        observed = record.get("outcome") in {"acknowledged", "unknown", "rejected", "invalid_response", "response_too_large"}
        completed_attempt = observed and isinstance(actual, (int, float)) and isinstance(completed, (int, float)) and math.isfinite(actual) and math.isfinite(completed) and 0 <= actual <= completed
        if not completed_attempt:
            mismatched += 1
        elif item["duplicate"]:
            duplicate_executed += 1
        if record.get("outcome") != "acknowledged":
            observed_failures += 1
    duplicate_planned = sum(item["duplicate"] for item in plan)
    complete = bool(plan) and len(plan) == len(planned) and len(seen) == len(plan) and len(records) == len(plan) and mismatched == 0 and duplicate_planned > 0 and duplicate_executed == duplicate_planned
    return {"planned_attempts": len(plan), "recorded_attempts": len(records), "missing_attempts": len(set(planned) - seen),
        "invalid_attempt_records": mismatched, "planned_duplicate_attempts": duplicate_planned,
        "executed_duplicate_attempts": duplicate_executed, "observed_failed_attempts": observed_failures,
        "execution_complete": complete}


def run(args: argparse.Namespace) -> dict:
    connections = credentials()
    if not args.skip_seed:
        control = os.environ["G4_CONTROL_TOKEN"]
        for connection in connections:
            result = post("/v1/dev/external-connections", {
                "run_id": args.run_id, "profile": connection["profile"], "connection_id": connection["connection_id"],
                "external_conversation_id": "g4-room", "external_sender_id": "g4-sender",
                "operator_user_id": os.environ.get("G4_OPERATOR_USER_ID", "00000000-0000-4000-8000-000000000001"),
                "idempotency_supported": True, "lookup_supported": True,
            }, control)
            if result["outcome"] != "acknowledged":
                raise ValueError("CONNECTION_SEED_FAILED")
            time.sleep(1 / args.rate)
    events = make_events(args.run_id, args.phase, args.events, connections)
    attempts = list(events) + events[::5]
    if len(attempts) > int(args.rate * args.duration) or len(attempts) > 300:
        raise ValueError("ATTEMPTS_EXCEED_TIME_BUDGET")
    records = []
    plan = [{"attempt_id": index, "event_id": event["external_event_id"], "external_message_id": event["external_message_id"],
             "connection_id": event["connection_id"], "duplicate": index >= len(events),
             "text_sha256": hashlib.sha256(event["text"].encode("utf-8")).hexdigest(),
             "external_sender_id": event["external_sender_id"], "external_conversation_id": event["external_conversation_id"],
             "profile": event["profile"]} for index, (event, _) in enumerate(attempts)]
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        outstanding = []
        all_futures = []
        last_dispatch = started - 1 / args.rate
        for index, (event, token) in enumerate(attempts):
            planned = index / args.rate
            remaining = max(started + planned, last_dispatch + 1 / args.rate) - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            last_dispatch = time.monotonic()
            outstanding = [future for future in outstanding if not future.done()]
            metadata = {**plan[index], "profile": event["profile"], "planned_seconds": planned}
            if len(outstanding) >= 4 or time.monotonic() - started >= args.duration:
                records.append({**metadata, "outcome": "skipped_at_capacity"})
                continue
            def execute(item=event, bearer=token, info=metadata):
                actual = time.monotonic() - started
                result = post("/v1/external-events", item, bearer)
                if result["outcome"] == "acknowledged" and (result.get("external_message_id") != item["external_message_id"] or not isinstance(result.get("seq"), str) or not re.fullmatch(r"[1-9][0-9]{0,18}", result["seq"]) or int(result["seq"]) > 9223372036854775807):
                    result["outcome"] = "invalid_response"
                if result["outcome"] == "acknowledged":
                    try:
                        UUID(result.get("message_id", ""))
                        UUID(result.get("conversation_id", ""))
                    except (ValueError, TypeError, AttributeError):
                        result["outcome"] = "invalid_response"
                records.append({**info, "actual_start_seconds": actual, **result, "completed_seconds": time.monotonic() - started})
            future = executor.submit(execute)
            outstanding.append(future)
            all_futures.append(future)
        for future in all_futures:
            future.result()
    records.sort(key=lambda record: record["planned_seconds"])
    return {"run_id": args.run_id, "phase": args.phase, "duration_seconds": time.monotonic() - started,
        "limits": {"http_rate": args.rate, "launch_duration": args.duration, "concurrency": 4, "request_timeout_seconds": 2},
        "summary": {**summarize(records, events), **audit_attempts(plan, records)}, "plan": plan, "records": records}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--events", type=int, default=70)
    parser.add_argument("--rate", type=float, default=5)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument("--output", default="/artifacts/result.json")
    args = parser.parse_args()
    if not 7 <= args.events <= 600 or not 0 < args.rate <= 10 or not 0 < args.duration <= 30:
        parser.error("CAPS: 7..600 events, <=10 HTTP/s, <=30 launch seconds")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", args.run_id) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,39}", args.phase):
        parser.error("Invalid run or phase identifier")
    return args


if __name__ == "__main__":
    try:
        arguments_value = arguments()
        result_value = run(arguments_value)
        target = Path(arguments_value.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result_value, indent=2), encoding="utf-8")
        # Completed Job containers cannot be exec'd for files. Preserve the same
        # secret-free evidence on stdout for the read-only collector.
        print(json.dumps(result_value))
        summary = result_value["summary"]
        raise SystemExit(1 if not summary["execution_complete"] or summary["observed_failed_attempts"] or summary["events_without_ack"] or summary["conflicting_ack_identities"] else 0)
    except (KeyError, ValueError, OSError):
        raise SystemExit("G4_RUN_FAILED: check configuration and target; secrets are not printed") from None
