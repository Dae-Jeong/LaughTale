"""In-cluster bounded open-loop chunk; the coordinator grants a short safety lease."""

import argparse
import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from uuid import UUID

import asyncpg
import httpx2 as httpx
from aiokafka import AIOKafkaConsumer, TopicPartition
from ledger import Ledger, digest
from websockets.asyncio.client import connect

ACTOR = "00000000-0000-4000-8000-000000000001"
ORIGIN = "http://127.0.0.1:18083"
BODY = "x" * 1024


async def execute(args):
    folder = Path("/evidence") / str(UUID(args.run_id))
    folder.mkdir()  # Never reuse an incomplete run or overwrite its evidence.
    lease = folder / "lease"
    lease.touch()
    ledger = Ledger(folder / "ledger.sqlite", args.run_id, args.count)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="evidence")

    async def call(method, *values):
        return await asyncio.get_running_loop().run_in_executor(
            executor, partial(method, *values)
        )

    async def query(statement, parameters=()):
        return await call(ledger.query, statement, parameters)

    stop = asyncio.Event()
    reasons = []
    outbox_ok = True
    sequence_errors = 0
    tasks = []
    evidence_queue = asyncio.Queue(maxsize=4096)
    evidence_failed = False
    last_commit = time.monotonic()
    result = {
        "run_id": args.run_id,
        "planned": args.count,
        "rate": args.rate,
        "status": "incomplete",
    }
    consumer = AIOKafkaConsumer(
        "chat.message-created.v1",
        bootstrap_servers="kafka:9092",
        group_id=None,
        enable_auto_commit=False,
        max_poll_records=500,
        fetch_max_bytes=2 * 1024 * 1024,
        max_partition_fetch_bytes=1024 * 1024,
    )

    def halt(reason):
        if reason not in reasons:
            reasons.append(reason)
        stop.set()

    def record_event(method, *values):
        nonlocal evidence_failed
        if evidence_failed:
            return False
        try:
            evidence_queue.put_nowait((method, values))
            return True
        except asyncio.QueueFull:
            evidence_failed = True
            halt("evidence_queue_full")
            return False

    def write(statement, parameters=()):
        return record_event(ledger.query, statement, parameters)

    def apply_batch(batch):
        nonlocal last_commit
        for method, values in batch:
            method(*values)
        if time.monotonic() - last_commit >= 1:
            ledger.db.commit()
            last_commit = time.monotonic()

    async def evidence_writer():
        nonlocal evidence_failed
        try:
            while True:
                first = await evidence_queue.get()
                batch = [first]
                while not evidence_queue.empty() and len(batch) < 500:
                    batch.append(evidence_queue.get_nowait())
                try:
                    await call(apply_batch, batch)
                finally:
                    for _ in batch:
                        evidence_queue.task_done()
        except Exception as error:  # noqa: BLE001 - fail closed without leaking evidence/SDK data
            evidence_failed = True
            halt("evidence_writer_" + type(error).__name__)
            while not evidence_queue.empty():
                evidence_queue.get_nowait()
                evidence_queue.task_done()

    def valid(message):
        if message["conversation_id"] != args.run_id or message["sender_id"] != ACTOR:
            raise ValueError("Unexpected event identity")
        return message

    async def maintenance():
        while not stop.is_set():
            if (folder / "STOP").exists() or time.time() - lease.stat().st_mtime > 60:
                halt("coordinator_lease_expired_or_stop")
            space = os.statvfs(folder)
            if space.f_bavail * space.f_frsize < 100 * 1024**3:
                halt("guest_disk_floor")
            await asyncio.sleep(0.25)

    async def kafka_reader():
        try:
            while not stop.is_set():
                batches = await consumer.getmany(timeout_ms=200, max_records=500)
                for records in batches.values():
                    for record in records:
                        event = json.loads(record.value)
                        if (
                            event.get("message", {}).get("conversation_id")
                            == args.run_id
                        ):
                            record_event(
                                ledger.observe,
                                "kafka",
                                valid(event["message"]),
                                time.monotonic(),
                            )
        except Exception as error:  # noqa: BLE001 - observer failure must stop the run
            halt("kafka_observer_" + type(error).__name__)

    async def ws_reader(ws):
        nonlocal sequence_errors
        previous = 0
        try:
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get("type") == "message.created":
                    message = valid(frame["message"])
                    seq = int(message["seq"])
                    if seq <= previous:
                        sequence_errors += 1
                    previous = seq
                    record_event(ledger.observe, "ws", message, time.monotonic())
                elif frame.get("type") not in {"heads", "subscribed"}:
                    halt("unexpected_ws_frame")
        except Exception as error:  # noqa: BLE001 - observer failure must stop the run
            halt("ws_observer_" + type(error).__name__)

    async def reconcile():
        nonlocal outbox_ok
        password = os.environ["READER_PASSWORD"]
        for channel, host, recovery in (
            ("primary", "chat-primary", False),
            ("replica", "chat-replica", True),
        ):
            conn = await asyncpg.connect(
                host=host,
                port=5432,
                user="chat_reader",
                password=password,
                database="laughtale_chat",
                timeout=5,
                command_timeout=10,
            )
            try:
                identity = await conn.fetchrow(
                    "SELECT current_database(),current_user,pg_is_in_recovery()"
                )
                if tuple(identity) != ("laughtale_chat", "chat_reader", recovery):
                    raise ValueError("Read target identity mismatch")
                for offset in range(0, args.count, 500):
                    batch = await query(
                        "SELECT cid FROM attempts WHERE idx>=? AND idx<? ORDER BY idx",
                        (offset, offset + 500),
                    )
                    ids = [UUID(row[0]) for row in batch]
                    rows = await conn.fetch(
                        """SELECT m.id,m.client_message_id,m.seq,m.text,
                        o.published_at FROM chat.messages m LEFT JOIN chat.message_outbox o ON o.event_id=m.id
                        WHERE m.conversation_id=$1 AND m.client_message_id=ANY($2::uuid[])""",
                        UUID(args.run_id),
                        ids,
                    )
                    for row in rows:
                        await call(
                            ledger.observe,
                            channel,
                            {
                                "client_message_id": str(row["client_message_id"]),
                                "message_id": str(row["id"]),
                                "seq": row["seq"],
                                "text": row["text"],
                            },
                            time.monotonic(),
                        )
                        if channel == "primary" and row["published_at"] is None:
                            outbox_ok = False
            finally:
                await conn.close()

    try:
        tasks.append(asyncio.create_task(evidence_writer()))
        tasks.append(asyncio.create_task(maintenance()))
        await consumer.start()
        partitions = consumer.partitions_for_topic("chat.message-created.v1")
        if partitions != {0, 1, 2, 3}:
            raise ValueError("Require the fixed four-partition lab topic")
        tps = [TopicPartition("chat.message-created.v1", p) for p in sorted(partitions)]
        offsets = await consumer.end_offsets(tps)
        for tp, offset in offsets.items():
            consumer.seek(tp, offset)
        result["kafka_start_offsets"] = {
            str(tp.partition): offset for tp, offset in offsets.items()
        }
        tasks.append(asyncio.create_task(kafka_reader()))
        # A newly scheduled Pod may precede network-policy reconciliation.
        # Retry only this readiness probe, never a product send request.
        async with httpx.AsyncClient(trust_env=False, timeout=2) as probe:
            for attempt in range(30):
                if stop.is_set():
                    raise RuntimeError("Stopped before gateway readiness")
                try:
                    ready = await probe.get(
                        f"http://{os.environ['GATEWAY_IP']}:18082/health/ready"
                    )
                    if ready.status_code == 200:
                        result["gateway_probe_attempts"] = attempt + 1
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Gateway startup path unavailable")
        async with connect(
            "ws://127.0.0.1:18082/v1/ws",
            host=os.environ["GATEWAY_IP"],
            port=18082,
            origin=ORIGIN,
            additional_headers={
                "Cookie": "chat_session=" + os.environ["SESSION_COOKIE"]
            },
            proxy=None,
            compression=None,
            max_size=16384,
            max_queue=1024,
            open_timeout=10,
            close_timeout=3,
        ) as ws:
            await ws.send(
                json.dumps({"type": "subscribe", "conversation_id": args.run_id})
            )
            async with asyncio.timeout(10):
                while json.loads(await ws.recv())["type"] != "subscribed":
                    pass
            tasks.append(asyncio.create_task(ws_reader(ws)))
            queue = asyncio.Queue(maxsize=64)
            headers = {
                "Origin": ORIGIN,
                "Cookie": "chat_session=" + os.environ["SESSION_COOKIE"],
                "X-Lab-Ingress-Token": os.environ["LAB_INGRESS_TOKEN"],
            }
            async with httpx.AsyncClient(
                base_url="http://chat:18082",
                trust_env=False,
                timeout=5,
                headers=headers,
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=32),
            ) as http:

                async def worker():
                    while True:
                        item = await queue.get()
                        try:
                            if item is None:
                                return
                            idx, cid, due = item
                            if stop.is_set():
                                write(
                                    "UPDATE attempts SET state='skipped' WHERE idx=?",
                                    (idx,),
                                )
                                continue
                            recorded = write(
                                "UPDATE attempts SET state='inflight' WHERE idx=?",
                                (idx,),
                            )
                            if not recorded:
                                continue
                            sent = time.monotonic()
                            try:
                                response = await http.post(
                                    f"/v1/internal-conversations/{args.run_id}/messages",
                                    json={"client_message_id": cid, "text": BODY},
                                )
                                now = time.monotonic()
                                if response.status_code == 201:
                                    message = valid(response.json()["data"])
                                    if message["client_message_id"] != cid or digest(
                                        message["text"]
                                    ) != digest(BODY):
                                        raise ValueError("ACK content mismatch")
                                    write(
                                        "UPDATE attempts SET state='ack',code=201,mid=?,seq=?,ack=?,sent=?,dispatch_delay=? WHERE idx=?",
                                        (
                                            message["message_id"],
                                            int(message["seq"]),
                                            now,
                                            sent,
                                            sent - due,
                                            idx,
                                        ),
                                    )
                                else:
                                    # A 5xx may occur after commit; never classify it as a definite rejection.
                                    state = (
                                        "rejected"
                                        if response.status_code
                                        in {400, 401, 403, 404, 409, 422, 429}
                                        else "unknown"
                                    )
                                    write(
                                        "UPDATE attempts SET state=?,code=?,sent=?,dispatch_delay=? WHERE idx=?",
                                        (
                                            state,
                                            response.status_code,
                                            sent,
                                            sent - due,
                                            idx,
                                        ),
                                    )
                            except (httpx.HTTPError, ValueError, KeyError, OSError):
                                write(
                                    "UPDATE attempts SET state='unknown',sent=?,dispatch_delay=? WHERE idx=?",
                                    (sent, sent - due, idx),
                                )
                        finally:
                            queue.task_done()

                workers = [asyncio.create_task(worker()) for _ in range(32)]
                tasks.extend(workers)
                planned = await query("SELECT idx,cid FROM attempts ORDER BY idx")
                skipped = []
                began = time.monotonic()
                for idx, cid in planned:
                    if stop.is_set():
                        break
                    due = began + idx / args.rate
                    await asyncio.sleep(max(0, due - time.monotonic()))
                    if time.monotonic() - due > 0.1 or queue.full():
                        skipped.append(idx)
                    else:
                        queue.put_nowait((idx, cid, due))
                for idx in skipped:
                    write("UPDATE attempts SET state='skipped' WHERE idx=?", (idx,))
                await queue.join()
                result["send_elapsed_seconds"] = time.monotonic() - began
                for _ in workers:
                    await queue.put(None)
                await asyncio.gather(*workers)
            # A safety stop must also drain accepted evidence writes before reporting.
            await evidence_queue.join()
            deadline = time.monotonic() + 300
            while not stop.is_set() and time.monotonic() < deadline:
                await evidence_queue.join()
                acknowledged = (
                    await query("SELECT count(*) FROM attempts WHERE state='ack'")
                )[0][0]
                missing = (
                    await query("""SELECT count(*) FROM attempts a WHERE a.state='ack' AND
                  (NOT EXISTS(SELECT 1 FROM observed o WHERE o.channel='ws' AND o.cid=a.cid)
                  OR NOT EXISTS(SELECT 1 FROM observed o WHERE o.channel='kafka' AND o.cid=a.cid))""")
                )[0][0]
                if acknowledged and not missing:
                    break
                await asyncio.sleep(0.5)
            if time.monotonic() >= deadline:
                halt("delivery_drain_timeout")
            await reconcile()
            result.update(await call(ledger.report, digest(BODY)))
            result["checks"].update(
                outbox_published=outbox_ok, ws_order=sequence_errors == 0
            )
            states = result["states"]
            result["achieved_ack_per_second"] = (
                states.get("ack", 0) / result["send_elapsed_seconds"]
            )
            result["checks"].update(
                offered_at_least_99_percent=(
                    args.count - states.get("skipped", 0) - states.get("planned", 0)
                )
                / args.count
                >= 0.99,
                ack_at_least_999_per_mille=states.get("ack", 0) / args.count >= 0.999,
                ack_p99_under_second=(result["ack_p99_seconds"] or float("inf")) <= 1,
                ws_p99_under_two_seconds=(result["ws_p99_seconds"] or float("inf"))
                <= 2,
                safety_guard=not reasons,
            )
            result["status"] = "pass" if all(result["checks"].values()) else "fail"
    except Exception as error:  # noqa: BLE001 - persist incomplete status, never SDK secrets
        halt("exception_" + type(error).__name__)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await consumer.stop()
        await call(ledger.close)
        executor.shutdown(wait=True)
        result["stop_reasons"] = reasons
        (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
    return result["status"] == "pass"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--rate", type=float, required=True)
    args = parser.parse_args()
    if not 1 <= args.count <= 100_000 or not 1 <= args.rate <= 2000:
        parser.error("Pilot requires count1..100000 and rate1..2000")
    raise SystemExit(0 if asyncio.run(execute(args)) else 1)
