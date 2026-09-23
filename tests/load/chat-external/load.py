"""Bounded open-loop local inbound experiment; no infrastructure management."""

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import urlsplit

SYSTEM_TESTS = Path(__file__).resolve().parents[2] / "system" / "chat-external"
sys.path.insert(0, str(SYSTEM_TESTS))

from collect import Client, Config, data, digest, history, ledger, origin
from oracle import PROFILES, EvidenceError, reconcile, uuid_value


def schedule(seconds: int, distribution: str) -> list[dict]:
    if (
        type(seconds) is not int
        or not 1 <= seconds <= 30
        or distribution not in {"distributed", "hot"}
    ):
        raise EvidenceError("invalid_load_plan")
    items = []
    for phase, rate in enumerate((1, 5, 10)):
        for number in range(rate * seconds):
            index = len(items)
            room_index = (
                index % 14
                if distribution == "distributed"
                else (0 if index % 5 else 1 + index % 13)
            )
            items.append(
                {
                    "index": index,
                    "rate": rate,
                    "offset_s": phase * seconds + number / rate,
                    "room_index": room_index,
                }
            )
    if len(items) > 600:
        raise EvidenceError("event_limit")
    return items


def percentile(values: list[float], percentile_value: float) -> float | None:
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * percentile_value) - 1)]


def run_schedule(
    items: list[dict],
    deliver,
    *,
    stop_requested=lambda: False,
    clock=time.monotonic,
    sleep=time.sleep,
    executor_factory=ThreadPoolExecutor,
) -> tuple[list[dict], str | None]:
    start = clock()
    results, pending = [], {}
    reason = None
    lag_bad_since = None

    def perform(item):
        began = clock()
        acknowledged, error = False, None
        try:
            acknowledged = deliver(item)
            if acknowledged is not True:
                error = "not_acknowledged"
        except EvidenceError as exc:
            error = str(exc)
        except (KeyError, ValueError, TypeError, AttributeError, OSError):
            error = "invalid_delivery_evidence"
        ended = clock()
        return {
            "index": item["index"],
            "rate": item["rate"],
            "scheduled_s": item["offset_s"],
            "started_s": began - start,
            "finished_s": ended - start,
            "lag_ms": max(0, (began - start - item["offset_s"]) * 1000),
            "duration_ms": (ended - began) * 1000,
            "acknowledged": acknowledged is True,
            "error": error,
        }

    with executor_factory(max_workers=8) as executor:
        for item in items:
            while clock() < start + item["offset_s"]:
                if stop_requested():
                    reason = "operator_stop"
                    break
                sleep(min(0.05, start + item["offset_s"] - clock()))
            for future in list(pending):
                if future.done():
                    results.append(future.result())
                    del pending[future]
            if reason or stop_requested():
                reason = "operator_stop"
                break
            if any(not result["acknowledged"] for result in results):
                reason = "delivery_failure"
                break
            recent = [
                result["lag_ms"]
                for result in results
                if clock() - start - result["started_s"] <= 10
            ]
            if recent and percentile(recent, 0.95) > 100:
                lag_bad_since = clock() if lag_bad_since is None else lag_bad_since
                if clock() - lag_bad_since >= 10:
                    reason = "generator_lag"
                    break
            else:
                lag_bad_since = None
            if len(pending) >= 8:
                reason = "generator_concurrency_limit"
                break
            if pending and clock() - min(pending.values()) > 30:
                reason = "oldest_request_limit"
                break
            pending[executor.submit(perform, item)] = clock()
        results.extend(future.result() for future in pending)
    if reason is None and any(not row["acknowledged"] for row in results):
        reason = "delivery_failure"
    return sorted(results, key=lambda row: row["index"]), reason


def observe(
    config: Config, client: Client, rooms: list[dict], chat: dict, mock: dict
) -> None:
    actual = data(
        client.request("chat", "GET", "/v1/external-conversations", browser=True)
    )
    actual = [
        room
        for room in actual
        if room["connection_id"] in config.connection_ids.values()
    ]
    expected = {room["conversation_id"]: room for room in rooms}
    if len(actual) != 14 or {room["conversation_id"] for room in actual} != set(
        expected
    ):
        raise EvidenceError("load_room_observation_mismatch")
    for room in actual:
        route = {
            name: room[name]
            for name in ("profile", "connection_id", "external_conversation_id")
        }
        if route != {name: expected[room["conversation_id"]][name] for name in route}:
            raise EvidenceError("load_room_route_mismatch")
        for message in history(client, room["conversation_id"]):
            if message["sender_kind"] != "customer":
                raise EvidenceError("unexpected_load_operator_message")
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
    chat["complete"] = True
    for record in ledger(client, config.run_id):
        if record["kind"] == "effect":
            command, effect = record["command"], record["effect"]
            if (
                command["run_id"] != config.run_id
                or effect["outbound_operation_id"] != command["outbound_operation_id"]
            ):
                raise EvidenceError("load_effect_identity_mismatch")
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


def execute(config: Config, output: Path, seconds: int, distribution: str) -> dict:
    config.validate()
    items = schedule(seconds, distribution)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    common = {
        "schema_version": 1,
        "run_id": config.run_id,
        "synthetic": True,
        "complete": False,
    }
    manifest = {**common, "inbound": [], "outbound": [], "attempts": []}
    chat = {**common, "inbound_messages": [], "outbound_jobs": []}
    mock = {**common, "effects": []}
    timing, stop_reason, result = (
        [],
        None,
        {"status": "incomplete", "issues": {"not_started": 1}},
    )
    client = Client(config)
    try:
        for service in ("chat", "mock"):
            if client.request(service, "GET", "/health/ready") != {"status": "ready"}:
                raise EvidenceError("service_not_ready")
        actor = data(
            client.request(
                "chat", "POST", "/v1/dev/session", {"user": "user_a"}, browser=True
            )
        )
        uuid_value(actor["user_id"])
        rooms = []
        for profile in sorted(PROFILES):
            for number in range(2):
                route = {
                    "profile": profile,
                    "connection_id": config.connection_ids[profile],
                    "external_conversation_id": f"room-{number}",
                }
                seed = {
                    **route,
                    "run_id": config.run_id,
                    "external_sender_id": "customer-1",
                    "operator_user_id": actor["user_id"],
                    "idempotency_supported": True,
                    "lookup_supported": True,
                }
                room = data(
                    client.request(
                        "chat",
                        "POST",
                        "/v1/dev/external-connections",
                        seed,
                        expected=(201,),
                    )
                )
                uuid_value(room["conversation_id"])
                rooms.append({**route, "conversation_id": room["conversation_id"]})
        client.request(
            "mock",
            "POST",
            "/control/v1/runs",
            {
                "run_id": config.run_id,
                "fault": "none",
                "max_events": len(items),
                "max_attempts": len(items) + 20,
                "max_effects": 1,
            },
            expected=(201,),
        )
        for item in items:
            route = {
                name: rooms[item["room_index"]][name]
                for name in ("profile", "connection_id", "external_conversation_id")
            }
            text = (f"Synthetic load {config.run_id} {item['index']} ").ljust(256, ".")
            event = {
                **route,
                "schema_version": 1,
                "run_id": config.run_id,
                "external_event_id": f"load-event-{item['index']}",
                "external_message_id": f"load-message-{item['index']}",
                "external_sender_id": "customer-1",
                "occurred_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "text": text,
            }
            manifest["inbound"].append(
                {
                    **route,
                    "external_message_id": event["external_message_id"],
                    "external_sender_id": event["external_sender_id"],
                    "text_sha256": digest(text),
                }
            )
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
                raise EvidenceError("load_event_registration_mismatch")
        # Preparation is outside the measured arrival window. No unlimited overall retry.
        client.deadline = time.monotonic() + 3 * seconds + 30

        def deliver(item):
            response = data(
                client.request(
                    "mock",
                    "POST",
                    f"/control/v1/runs/{config.run_id}/events/load-event-{item['index']}/deliver",
                )
            )
            return response.get("result") == "acknowledged" and response.get(
                "http_status"
            ) in (200, 201)

        timing, stop_reason = run_schedule(
            items, deliver, stop_requested=lambda: (output / "STOP").exists()
        )
        manifest["attempts"] = [
            {
                "attempt_id": f"load-{row['index']}",
                "direction": "inbound",
                "intent_index": row["index"],
            }
            for row in timing
        ]
        manifest["complete"] = len(timing) == len(items) and stop_reason is None
        client.deadline = time.monotonic() + 30
        observe(config, client, rooms, chat, mock)
        result = reconcile(manifest, chat, mock)
    except EvidenceError as exc:
        result = {"schema_version": 1, "status": "incomplete", "issues": {str(exc): 1}}
    except (KeyError, TypeError, ValueError, AttributeError):
        result = {
            "schema_version": 1,
            "status": "incomplete",
            "issues": {"invalid_load_evidence": 1},
        }
    summary = {
        "schema_version": 1,
        "distribution": distribution,
        "seconds_per_rate": seconds,
        "planned": len(items),
        "started": len(timing),
        "acknowledged": sum(row["acknowledged"] for row in timing),
        "stop_reason": stop_reason,
        "correctness": result["status"],
        "performance_slo": "not_evaluated",
        "resource_metrics": "collect_separately",
        "phases": [],
    }
    for rate in (1, 5, 10):
        rows = [row for row in timing if row["rate"] == rate]
        summary["phases"].append(
            {
                "rate": rate,
                "samples": len(rows),
                "lag_p95_ms": percentile([row["lag_ms"] for row in rows], 0.95),
                "latency_p95_ms": percentile(
                    [row["duration_ms"] for row in rows], 0.95
                ),
                "latency_p99_ms": percentile(
                    [row["duration_ms"] for row in rows], 0.99
                ),
            }
        )
    for name, value in (
        ("manifest", manifest),
        ("chat", chat),
        ("mock", mock),
        ("result", result),
        ("timing", timing),
        ("summary", summary),
    ):
        with (output / f"{name}.json").open("x", encoding="utf-8") as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.write("\n")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument(
        "--sink-only",
        action="store_true",
        help="Calibrate generator against the separate no-DB acknowledgement sink",
    )
    parser.add_argument("--sink-url", default="http://127.0.0.1:18089")
    parser.add_argument(
        "--distribution", choices=("distributed", "hot"), default="distributed"
    )
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("--execute is required for bounded synthetic writes")
    try:
        if args.sink_only:
            summary = baseline(args.output_dir, args.seconds, args.sink_url)
            print(json.dumps(summary, sort_keys=True))
            return 0 if summary["generator_baseline"] == "pass" else 2
        config = Config(
            args.run_id,
            json.loads(os.environ["CHAT_TEST_CONNECTION_IDS"]),
            os.environ["CHAT_CONTROL_TOKEN"],
            os.environ["MOCK_CONTROL_TOKEN"],
            os.environ.get("CHAT_BASE_URL", "http://127.0.0.1:18082"),
            os.environ.get("MOCK_BASE_URL", "http://127.0.0.1:18087"),
            os.environ.get("CHAT_TEST_ORIGIN", "http://127.0.0.1:18083"),
            120,
        )
        summary = execute(config, args.output_dir, args.seconds, args.distribution)
    except (EvidenceError, OSError, ValueError, KeyError):
        print('{"correctness":"incomplete","issue":"configuration_or_artifact_error"}')
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["correctness"] == "pass" else 2


def sink_delivery(url: str) -> bool:
    parsed = urlsplit(origin(url))
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        connection.request(
            "POST",
            "/sink",
            headers={"Content-Length": "0", "Accept": "application/json"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise EvidenceError(f"sink_http_status_{response.status}")
        if (
            response.getheader("Content-Type", "").split(";", 1)[0]
            != "application/json"
        ):
            raise EvidenceError("sink_json_required")
        raw = response.read(1025)
        if len(raw) > 1024:
            raise EvidenceError("sink_response_limit")
        return json.loads(raw) == {
            "data": {"result": "acknowledged", "http_status": 201}
        }
    except (OSError, HTTPException, ValueError):
        raise EvidenceError("sink_transport_failure") from None
    finally:
        connection.close()


def baseline(output: Path, seconds: int, url: str) -> dict:
    origin(url)
    items = schedule(seconds, "distributed")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    timing, reason = run_schedule(
        items,
        lambda _: sink_delivery(url),
        stop_requested=lambda: (output / "STOP").exists(),
    )
    lag = percentile([row["lag_ms"] for row in timing], 0.95)
    summary = {
        "generator_baseline": "pass"
        if reason is None
        and len(timing) == len(items)
        and lag is not None
        and lag <= 100
        else "incomplete",
        "planned": len(items),
        "started": len(timing),
        "stop_reason": reason,
        "lag_p95_ms": lag,
        "latency_p95_ms": percentile([row["duration_ms"] for row in timing], 0.95),
        "chat_correctness": "not_evaluated",
        "performance_slo": "not_evaluated",
    }
    for name, value in (("timing", timing), ("summary", summary)):
        with (output / f"{name}.json").open("x", encoding="utf-8") as target:
            json.dump(value, target, indent=2, sort_keys=True)
            target.write("\n")
    return summary


if __name__ == "__main__":
    raise SystemExit(main())
