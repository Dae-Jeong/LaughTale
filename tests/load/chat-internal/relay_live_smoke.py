"""100-request deployed Relay smoke, scoped Kafka offset capture and DB proof."""

import asyncio
import hashlib
import json
from pathlib import Path

from aiokafka import AIOKafkaConsumer, TopicPartition
from lock_probe import HASH, run, sql

TOPIC = "chat.message-created.v1"


async def main():
    consumer = AIOKafkaConsumer(
        bootstrap_servers="127.0.0.1:19092", group_id=None, enable_auto_commit=False
    )
    try:
        await consumer.start()
        assert TOPIC in await consumer.topics()
        partitions = [TopicPartition(TOPIC, p) for p in range(4)]
        consumer.assign(partitions)
        starts = await consumer.end_offsets(partitions)
        for p in partitions:
            consumer.seek(p, starts[p])
        result = await run(10, 10, 1)
        folder = Path(result["run_dir"])
        if result["status"] != "pass":
            raise RuntimeError("HTTP_DB_smoke_failed")
        primary = json.loads((folder / "primary.json").read_text())
        expected = {r["mid"]: r for r in primary.values()}
        ids = ",".join(f"'{mid}'::uuid" for mid in expected)
        published_query = f"SELECT count(*) FROM chat.message_outbox WHERE published_at IS NOT NULL AND event_id IN ({ids});"
        events = []
        async with asyncio.timeout(25):
            while await asyncio.to_thread(sql, published_query) != 100:
                await asyncio.sleep(0.2)
            ends = await consumer.end_offsets(partitions)
            while True:
                if all([await consumer.position(p) >= ends[p] for p in partitions]):
                    break
                batches = await consumer.getmany(timeout_ms=500, max_records=500)
                for p, records in batches.items():
                    for record in records:
                        if record.offset >= ends[p]:
                            continue
                        event = json.loads(record.value)
                        message = event.get("message", {})
                        mid = message.get("message_id")
                        if mid not in expected:
                            continue
                        want = expected[mid]
                        assert record.key.decode() == want["room"]
                        assert int(message["seq"]) == want["seq"]
                        assert (
                            hashlib.sha256(message["text"].encode()).hexdigest() == HASH
                        )
                        events.append(
                            {
                                "id": mid,
                                "seq": int(message["seq"]),
                                "partition": p.partition,
                                "offset": record.offset,
                            }
                        )
        proof = {
            "events": events,
            "published": 100,
            "unique_events": len({e["id"] for e in events}),
            "ordered": [e["seq"] for e in events] == list(range(1, 101)),
            "offsets": [
                {"partition": p.partition, "start": starts[p], "end": ends[p]}
                for p in partitions
            ],
        }
        proof["pass"] = (
            len(events) == proof["unique_events"] == 100 and proof["ordered"]
        )
        (folder / "relay-kafka-proof.json").write_text(json.dumps(proof, indent=2))
        print(json.dumps({k: v for k, v in proof.items() if k != "events"}))
        assert proof["pass"]
    finally:
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(main())
