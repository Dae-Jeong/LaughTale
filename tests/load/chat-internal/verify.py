"""Independent read-only DB/outbox/Kafka oracle; not a WS/fan-out verifier.

Consumer API: https://aiokafka.readthedocs.io/en/stable/consumer.html
Manual assign, group_id=None, no offset commits; captured end offsets bound a run.
"""

import argparse
import asyncio
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID

ROOM = "00000000-0000-4000-8000-000000000010"
ACTOR = "00000000-0000-4000-8000-000000000001"
TOPIC = "chat.message-created.v1"
MAX_EVENTS = 10000


class EvidenceError(ValueError):
    """Only static error codes; never attach payloads, SQL parameters or secrets."""


def uuid_text(value: object) -> str:
    if not isinstance(value, str):
        raise EvidenceError("uuid_required")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise EvidenceError("canonical_uuid_required") from None
    return value


def seq_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[1-9][0-9]{0,18}", value)
        or int(value) > 2**63 - 1
    ):
        raise EvidenceError("positive_bigint_seq_required")
    return value


def instant(value: object) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EvidenceError("aware_timestamp_required")
    return value.astimezone(timezone.utc).isoformat()


def text_hash(value: object) -> str:
    if not isinstance(value, str):
        raise EvidenceError("text_required")
    return sha256(value.encode("utf-8")).hexdigest()


def unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("duplicate_json_field")
        result[key] = value
    return result


def normalize_event(payload: dict) -> dict:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"type", "event_id", "schema_version", "message"}
        or payload["type"] != "message.created"
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
    ):
        raise EvidenceError("invalid_event_envelope")
    message = payload["message"]
    names = {
        "message_id",
        "conversation_id",
        "sender_id",
        "client_message_id",
        "seq",
        "text",
        "created_at",
    }
    if not isinstance(message, dict) or set(message) != names:
        raise EvidenceError("invalid_event_message")
    normalized = {
        name: uuid_text(message[name])
        for name in ("message_id", "conversation_id", "sender_id", "client_message_id")
    }
    normalized.update(
        event_id=uuid_text(payload["event_id"]),
        seq=seq_text(message["seq"]),
        text_sha256=text_hash(message["text"]),
        created_at=instant(message["created_at"]),
    )
    if normalized["event_id"] != normalized["message_id"]:
        raise EvidenceError("event_message_id_mismatch")
    normalized["payload_sha256"] = sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return normalized


def validate_ledger(ledger: dict) -> dict[str, list]:
    if (
        not isinstance(ledger, dict)
        or type(ledger.get("complete")) is not bool
        or not isinstance(ledger.get("run_id"), str)
    ):
        raise EvidenceError("ledger_envelope_missing")
    attempts = ledger.get("attempts")
    if (
        type(ledger.get("planned_count")) is not int
        or not 1 <= ledger["planned_count"] <= MAX_EVENTS
    ):
        raise EvidenceError("bounded_plan_count_required")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= MAX_EVENTS:
        raise EvidenceError("bounded_attempts_required")
    intents, attempt_ids = defaultdict(list), set()
    for row in attempts:
        if not isinstance(row, dict):
            raise EvidenceError("attempt_object_required")
        identity = uuid_text(row["client_message_id"])
        if row["conversation_id"] != ROOM or row["sender_id"] != ACTOR:
            raise EvidenceError("unexpected_workload_scope")
        if not isinstance(row["text_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", row["text_sha256"]
        ):
            raise EvidenceError("digest_required")
        attempt_id = row["attempt_id"]
        if (
            not isinstance(attempt_id, (str, int))
            or isinstance(attempt_id, bool)
            or attempt_id in attempt_ids
        ):
            raise EvidenceError("unique_attempt_id_required")
        attempt_ids.add(attempt_id)
        outcome, status = row["outcome"], row["ack_status"]
        if outcome not in {"acknowledged", "unknown", "rejected", "not_attempted"}:
            raise EvidenceError("invalid_attempt_outcome")
        if outcome == "acknowledged":
            if type(status) is not int or status not in (200, 201):
                raise EvidenceError("ack_status_required")
            uuid_text(row["response_message_id"])
            seq_text(row["response_seq"])
        elif status is not None:
            raise EvidenceError("unexpected_ack_status")
        if (
            intents[identity]
            and row["text_sha256"] != intents[identity][0]["text_sha256"]
        ):
            raise EvidenceError("conflicting_logical_payload")
        intents[identity].append(row)
    return dict(intents)


def reconcile(
    ledger: dict,
    messages: list[dict],
    outbox: list[dict],
    events: list[dict],
    capture: dict,
) -> dict:
    try:
        intents = validate_ledger(ledger)
    except (EvidenceError, KeyError, TypeError, ValueError):
        return {"status": "incomplete", "issues": {"invalid_request_ledger": 1}}
    issues = Counter()
    stored, boxes, received = defaultdict(list), defaultdict(list), defaultdict(list)
    for message in messages:
        stored[message["client_message_id"]].append(message)
    for record in outbox:
        boxes[record["event_id"]].append(record)
    relevant = []
    known_event_ids = {message["message_id"] for message in messages}
    for event in events:
        if event.get("invalid_event"):
            issues["invalid_kafka_event"] += 1
        elif (
            event["client_message_id"] in intents
            or event["event_id"] in known_event_ids
        ):
            received[event["event_id"]].append(event)
            relevant.append(event)
    seen_sequences, counts = set(), Counter()
    for identity, attempts in intents.items():
        acked = [row for row in attempts if row["outcome"] == "acknowledged"]
        unknown = any(row["outcome"] == "unknown" for row in attempts)
        rows = stored.get(identity, [])
        counts["acknowledged_intents"] += bool(acked)
        if not acked and unknown:
            counts["unknown_stored" if rows else "unknown_absent"] += 1
        if len(rows) > 1 or (acked and len(rows) != 1):
            issues["message_count_mismatch"] += 1
        if not rows:
            continue
        if not acked and not unknown:
            issues["nonaccepted_request_stored"] += 1
        for message in rows:
            if (
                message["conversation_id"] != ROOM
                or message["sender_id"] != ACTOR
                or message["text_sha256"] != attempts[0]["text_sha256"]
            ):
                issues["message_scope_or_payload_mismatch"] += 1
            position = (message["conversation_id"], message["seq"])
            if position in seen_sequences:
                issues["duplicate_room_seq"] += 1
            seen_sequences.add(position)
            for ack in acked:
                if (
                    ack["response_message_id"] != message["message_id"]
                    or ack["response_seq"] != message["seq"]
                ):
                    issues["ack_message_mismatch"] += 1
            records = boxes.get(message["message_id"], [])
            if len(records) != 1:
                issues["outbox_count_mismatch"] += 1
                continue
            record = records[0]
            if record["published"] is not True:
                issues["outbox_not_published"] += 1
            if record.get("invalid_event"):
                issues["outbox_payload_invalid"] += 1
                continue
            compared = (
                "message_id",
                "conversation_id",
                "sender_id",
                "client_message_id",
                "seq",
                "text_sha256",
                "created_at",
            )
            if any(record[name] != message[name] for name in compared):
                issues["outbox_message_payload_mismatch"] += 1
            delivered = received.get(message["message_id"], [])
            if not delivered:
                issues["kafka_event_missing"] += 1
            counts["duplicate_kafka_events"] += max(0, len(delivered) - 1)
            for event in delivered:
                if (
                    event["key"] != ROOM
                    or event["payload_sha256"] != record["payload_sha256"]
                    or any(event[name] != message[name] for name in compared)
                ):
                    issues["kafka_payload_or_key_mismatch"] += 1
    if set(stored) - set(intents):
        issues["unexpected_database_identity"] += len(set(stored) - set(intents))
    if set(boxes) - known_event_ids:
        issues["orphan_outbox_evidence"] += len(set(boxes) - known_event_ids)
    if set(received) - known_event_ids:
        issues["kafka_without_matching_message"] += len(set(received) - known_event_ids)
    incomplete = (
        ledger["complete"] is not True
        or ledger.get("planned_count") != len(ledger["attempts"])
        or any(row["outcome"] == "not_attempted" for row in ledger["attempts"])
        or capture.get("complete") is not True
    )
    return {
        "status": "incomplete" if incomplete else "failed" if issues else "pass",
        "issues": dict(+issues),
        "counts": {
            "attempts": len(ledger["attempts"]),
            "logical_intents": len(intents),
            "stored_messages": len(messages),
            "outbox_rows": len(outbox),
            "published_outbox_rows": sum(row["published"] for row in outbox),
            "relevant_kafka_records": len(relevant),
            **dict(counts),
        },
        "capture": capture,
        "scope": "HTTP ACK to DB/outbox/Kafka receipt only; WS/full fanout unverified",
        "db_evidence": messages,
        "outbox_evidence": outbox,
        "kafka_evidence": relevant,
    }


async def read_database(url: str, identities: list[str]) -> tuple[list, list]:
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    target = make_url(url)
    if (
        target.drivername != "postgresql+asyncpg"
        or target.database != "laughtale_chat"
        or target.query
    ):
        raise EvidenceError("explicit_development_database_required")
    if (target.host, target.port) not in {("127.0.0.1", 5440), ("localhost", 5440)}:
        raise EvidenceError("explicit_primary_endpoint_required")
    engine = create_async_engine(
        target,
        poolclass=NullPool,
        hide_parameters=True,
        echo=False,
        connect_args={"timeout": 3},
    )
    try:
        async with engine.connect() as connection:
            connection = await connection.execution_options(
                isolation_level="REPEATABLE READ"
            )
            async with connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                await connection.execute(text("SET LOCAL statement_timeout = '3000ms'"))
                await connection.execute(text("SET LOCAL lock_timeout = '500ms'"))
                rows = (
                    (
                        await connection.execute(
                            text(
                                "SELECT id, conversation_id, sender_id, client_message_id, seq, text, created_at FROM chat.messages WHERE client_message_id = ANY(CAST(:ids AS uuid[]))"
                            ),
                            {"ids": identities},
                        )
                    )
                    .mappings()
                    .all()
                )
                boxes = (
                    (
                        await connection.execute(
                            text(
                                "SELECT event_id, payload, published_at FROM chat.message_outbox WHERE event_id IN (SELECT id FROM chat.messages WHERE client_message_id = ANY(CAST(:ids AS uuid[])))"
                            ),
                            {"ids": identities},
                        )
                    )
                    .mappings()
                    .all()
                )
        messages = [
            {
                "message_id": str(row["id"]),
                "conversation_id": str(row["conversation_id"]),
                "sender_id": str(row["sender_id"]),
                "client_message_id": str(row["client_message_id"]),
                "seq": str(row["seq"]),
                "text_sha256": text_hash(row["text"]),
                "created_at": instant(row["created_at"]),
            }
            for row in rows
        ]
        normalized = []
        for row in boxes:
            try:
                event = normalize_event(row["payload"])
            except (EvidenceError, ValueError, TypeError):
                event = {"invalid_event": True}
            normalized.append(
                {
                    **event,
                    "event_id": str(row["event_id"]),
                    "published": row["published_at"] is not None,
                }
            )
        return messages, normalized
    finally:
        await engine.dispose()


async def capture_kafka(
    bootstrap: str, seconds: int, maximum: int
) -> tuple[list, dict]:
    from aiokafka import AIOKafkaConsumer, TopicPartition

    if bootstrap not in {"127.0.0.1:19092", "localhost:19092"}:
        raise EvidenceError("explicit_local_kafka_endpoint_required")
    consumer = AIOKafkaConsumer(
        bootstrap_servers=bootstrap,
        group_id=None,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        request_timeout_ms=5000,
        fetch_max_bytes=1_048_576,
        max_partition_fetch_bytes=262_144,
    )
    records, summary = (
        [],
        {"complete": False, "topic": TOPIC, "partitions": [], "scanned_records": 0},
    )
    try:
        async with asyncio.timeout(seconds):
            await consumer.start()
            topics = await consumer.topics()
            if TOPIC not in topics:
                raise EvidenceError("expected_four_partition_topic_missing")
            partitions = [TopicPartition(TOPIC, number) for number in range(4)]
            consumer.assign(partitions)
            # topics() returns separate metadata; it does not populate the
            # consumer cache. Assignment registers the topic, and this public
            # offset lookup waits for usable partition metadata (aiokafka 0.14).
            beginning = await consumer.beginning_offsets(partitions)
            if consumer.partitions_for_topic(TOPIC) != {0, 1, 2, 3}:
                raise EvidenceError("expected_four_partition_topic_missing")
            endings = await consumer.end_offsets(partitions)
            for partition in partitions:
                consumer.seek(partition, beginning[partition])
            summary["partitions"] = [
                {
                    "partition": partition.partition,
                    "beginning": beginning[partition],
                    "captured_end": endings[partition],
                    "position": beginning[partition],
                }
                for partition in partitions
            ]
            if sum(endings[p] - beginning[p] for p in partitions) > maximum:
                raise EvidenceError("snapshot_exceeds_event_limit")
            while True:
                positions = {
                    partition: await consumer.position(partition)
                    for partition in partitions
                }
                for state in summary["partitions"]:
                    state["position"] = positions[partitions[state["partition"]]]
                if all(positions[p] >= endings[p] for p in partitions):
                    summary["complete"] = True
                    break
                if len(records) >= maximum:
                    raise EvidenceError("event_limit")
                batch = await consumer.getmany(
                    timeout_ms=250, max_records=min(500, maximum - len(records))
                )
                for partition, messages in batch.items():
                    for message in messages:
                        if message.offset >= endings[partition]:
                            continue
                        try:
                            if message.value is None or len(message.value) > 16384:
                                raise EvidenceError("event_size")
                            event = normalize_event(
                                json.loads(
                                    message.value, object_pairs_hook=unique_object
                                )
                            )
                            event["key"] = (
                                message.key.decode("utf-8")
                                if message.key is not None
                                else None
                            )
                        except (EvidenceError, ValueError, TypeError, UnicodeError):
                            event = {"invalid_event": True}
                        records.append(
                            {
                                **event,
                                "partition": message.partition,
                                "offset": message.offset,
                            }
                        )
                        summary["scanned_records"] += 1
    except (TimeoutError, EvidenceError) as exc:
        summary["error"] = (
            "capture_timeout" if isinstance(exc, TimeoutError) else str(exc)
        )
    finally:
        try:
            async with asyncio.timeout(3):
                await consumer.stop()
        except TimeoutError:
            summary["complete"] = False
            summary["error"] = "consumer_cleanup_timeout"
    return records, summary


async def verify(
    ledger: dict, db_url: str, bootstrap: str, seconds: int, maximum: int
) -> dict:
    identities = list(validate_ledger(ledger))
    async with asyncio.timeout(10):
        messages, outbox = await read_database(db_url, identities)
    # Caller runs after relay drain; a not-yet-published row is a real failed check, not silently skipped.
    events, capture = await capture_kafka(bootstrap, seconds, maximum)
    return reconcile(ledger, messages, outbox, events, capture)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--db-url-env", default="DB_PRIMARY_URL")
    parser.add_argument("--kafka-bootstrap-env", default="KAFKA_BOOTSTRAP_SERVERS")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--max-events", type=int, default=MAX_EVENTS)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout_seconds <= 30 or not 1 <= args.max_events <= MAX_EVENTS:
        parser.error("Use timeout 1..30 seconds and max-events 1..10000")
    if args.output.exists():
        parser.error("Output already exists; refusing overwrite")
    try:
        ledger = json.loads(args.ledger.read_text(), object_pairs_hook=unique_object)
        result = asyncio.run(
            verify(
                ledger,
                os.environ[args.db_url_env],
                os.environ[args.kafka_bootstrap_env],
                args.timeout_seconds,
                args.max_events,
            )
        )
    except Exception:  # noqa: BLE001 - CLI boundary must redact driver/SQL details.
        # CLI boundary: SQL/driver exceptions may contain credentials or payloads; never print them.
        result = {
            "status": "incomplete",
            "issues": {"verification_input_or_collection_failed": 1},
        }
    try:
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(result, output, indent=2, sort_keys=True)
            output.write("\n")
    except OSError:
        print('{"status":"incomplete","issues":{"output_unwritable":1}}')
        return 2
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if not key.endswith("_evidence")
            },
            sort_keys=True,
        )
    )
    return {"pass": 0, "failed": 1, "incomplete": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
