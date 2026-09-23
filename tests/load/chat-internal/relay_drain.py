"""Bounded real-image Relay drain, isolated test schema; no live table writes."""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from aiokafka import AIOKafkaConsumer, TopicPartition
from chat_service.adapters.kafka_publisher import TOPIC, KafkaPublisher
from chat_service.contracts.chat import ChatMessage
from chat_service.contracts.events import message_created
from chat_service.models.chat import Conversation, Member, Message, MessageOutbox, User
from chat_service.services.relay import OutboxRelay
from sqlalchemy import insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BODY = "x" * 1024
HASH = hashlib.sha256(BODY.encode()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


async def main():
    variant = os.environ["RELAY_DRAIN_VARIANT"]
    if variant not in {"before", "after"}:
        raise ValueError("variant")
    url = make_url(os.environ["CHAT_TEST_DATABASE_URL"])
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.port != 5440
        or url.database != "laughtale_chat_test"
        or url.username != "chat_test"
        or not url.password
        or url.query
    ):
        raise ValueError("test_endpoint_required")
    schema = "run_" + uuid4().hex
    folder = Path("/results/relay-drain") / (variant + "-" + schema)
    folder.mkdir(parents=True)
    result = {
        "variant": variant,
        "schema": schema,
        "target": 9000,
        "cpu": 0.2,
        "memory_mib": 192,
        "budget_seconds": 600,
        "status": "preparing",
        "schema_retained": True,
    }
    save(folder / "result.json", result)
    engine = create_async_engine(
        url.set(host="host.docker.internal"),
        pool_size=1,
        max_overflow=0,
        pool_timeout=2,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "timeout": 3,
            "server_settings": {
                "statement_timeout": "5000",
                "lock_timeout": "1000",
                "application_name": "relay-drain-" + variant,
            },
        },
    )
    mapped = engine.execution_options(schema_translate_map={"chat": schema})
    consumer = AIOKafkaConsumer(
        TOPIC,
        bootstrap_servers="kafka:9092",
        group_id=None,
        enable_auto_commit=False,
        auto_offset_reset="latest",
    )
    publisher = KafkaPublisher("kafka:9092")
    expected = {}
    try:
        async with engine.begin() as conn:
            identity = (
                await conn.execute(
                    text("SELECT current_database(), current_user, pg_is_in_recovery()")
                )
            ).one()
            if tuple(identity) != ("laughtale_chat_test", "chat_test", False):
                raise ValueError("test_identity")
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        tables = [
            c.__table__ for c in (User, Conversation, Member, Message, MessageOutbox)
        ]
        async with mapped.begin() as conn:
            await conn.run_sync(lambda c: Message.metadata.create_all(c, tables=tables))
        actor, now = uuid4(), datetime.now(UTC)
        async with mapped.begin() as conn:
            await conn.execute(
                insert(User), {"id": actor, "display_name": "drain-test"}
            )
        for _ in range(10):
            room = uuid4()
            async with mapped.begin() as conn:
                await conn.execute(insert(Conversation), {"id": room, "last_seq": 1000})
                await conn.execute(
                    insert(Member), {"conversation_id": room, "user_id": actor}
                )
            for start in range(1, 1001, 100):
                messages, outbox = [], []
                for seq in range(start, start + 100):
                    mid, cid = uuid4(), uuid4()
                    event = message_created(
                        ChatMessage(mid, room, actor, cid, seq, BODY, now)
                    )
                    messages.append(
                        {
                            "id": mid,
                            "conversation_id": room,
                            "sender_id": actor,
                            "client_message_id": cid,
                            "seq": seq,
                            "text": BODY,
                            "payload_version": 1,
                            "payload_hash": HASH,
                            "created_at": now,
                        }
                    )
                    outbox.append(
                        {
                            "event_id": mid,
                            "payload": event,
                            "created_at": now,
                            "published_at": now if seq <= 100 else None,
                        }
                    )
                    if seq > 100:
                        expected[str(mid)] = (str(room), seq)
                async with mapped.begin() as conn:
                    await conn.execute(insert(Message), messages)
                    await conn.execute(insert(MessageOutbox), outbox)
        async with engine.begin() as conn:
            for name in ("messages", "message_outbox"):
                await conn.execute(text(f'ANALYZE "{schema}"."{name}"'))
        await consumer.start()
        partitions = consumer.partitions_for_topic(TOPIC)
        if not partitions:
            raise ValueError("topic_metadata")
        consumer.unsubscribe()
        tps = [TopicPartition(TOPIC, p) for p in sorted(partitions)]
        consumer.assign(tps)
        begin = await consumer.end_offsets(tps)
        for tp, offset in begin.items():
            consumer.seek(tp, offset)
        relay = OutboxRelay(
            async_sessionmaker(mapped, expire_on_commit=False), publisher
        )
        counts, errors, checkpoints = Counter(), Counter(), []
        started, last = time.monotonic(), 0
        result["status"] = "running"
        print(
            json.dumps({"event": "started", "variant": variant, "folder": str(folder)}),
            flush=True,
        )
        while counts["published"] < 9000 and time.monotonic() - started < 600:
            try:
                outcome = await relay.once()
            except Exception as exc:  # noqa: BLE001 — match live retry; count error types
                errors[type(exc).__name__] += 1
                outcome = "database_retry"
            counts[outcome] += 1
            if outcome in {"idle", "database_retry"}:
                await asyncio.sleep(0.5)
            elapsed = time.monotonic() - started
            if elapsed - last >= 30:
                point = {"elapsed": elapsed, "published": counts["published"]}
                checkpoints.append(point)
                print(json.dumps(point), flush=True)
                last = elapsed
                save(
                    folder / "progress.json",
                    {"checkpoints": checkpoints, "errors": errors},
                )
        elapsed = time.monotonic() - started
        await publisher.close()
        end = await consumer.end_offsets(tps)
        seen, sequences, records = Counter(), defaultdict(list), []
        bad = 0
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if all([await consumer.position(tp) >= end[tp] for tp in tps]):
                break
            batch = await consumer.getmany(timeout_ms=500, max_records=500)
            for tp, items in batch.items():
                for item in items:
                    if item.offset >= end[tp]:
                        continue
                    event = json.loads(item.value)
                    mid = event.get("event_id")
                    if mid not in expected:
                        continue
                    room, seq = expected[mid]
                    msg = event["message"]
                    valid = (
                        item.key == room.encode()
                        and msg["conversation_id"] == room
                        and msg["message_id"] == mid
                        and int(msg["seq"]) == seq
                        and hashlib.sha256(msg["text"].encode()).hexdigest() == HASH
                    )
                    bad += not valid
                    seen[mid] += 1
                    sequences[room].append(seq)
                    records.append(
                        {
                            "id": mid,
                            "seq": seq,
                            "partition": tp.partition,
                            "offset": item.offset,
                        }
                    )
        async with mapped.connect() as conn:
            published = {
                str(row[0])
                for row in (
                    await conn.execute(
                        select(MessageOutbox.event_id).where(
                            MessageOutbox.published_at.is_not(None)
                        )
                    )
                )
                if str(row[0]) in expected
            }
        consumed = all([await consumer.position(tp) >= end[tp] for tp in tps])
        duplicates = sum(n - 1 for n in seen.values())
        ordered = all(s == sorted(set(s)) for s in sequences.values())
        correct = (
            consumed
            and not bad
            and not duplicates
            and ordered
            and set(seen) == published
        )
        result.update(
            status="complete" if len(published) == 9000 else "budget_exceeded",
            elapsed_seconds=elapsed,
            db_published=len(published),
            messages_per_second=len(published) / elapsed,
            outcomes=counts,
            errors=errors,
            kafka_unique=len(seen),
            duplicates=duplicates,
            invalid_events=bad,
            room_ordered=ordered,
            consumed_window=consumed,
            partial_reconciliation=correct,
            pass_all=correct and len(published) == 9000,
            checkpoints=checkpoints,
            offsets={str(tp.partition): [begin[tp], end[tp]] for tp in tps},
        )
        save(folder / "kafka-records.json", records)
        save(folder / "result.json", result)
        print(json.dumps(result), flush=True)
    finally:
        await publisher.close()
        await consumer.stop()
        await engine.dispose()


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001 — never expose DB URLs or SQL parameters
        print(json.dumps({"event": "failed", "type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
