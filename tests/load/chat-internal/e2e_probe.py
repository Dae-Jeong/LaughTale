"""Bounded live HTTP→DB→Kafka→two Gateway WS evidence; no browser claim."""

import argparse
import asyncio
import hashlib
import json
import math
import socket
import subprocess
import time
from collections import Counter
from contextlib import AsyncExitStack

import httpx2 as httpx
from aiokafka import AIOKafkaConsumer, TopicPartition
from lock_probe import ACTOR, HASH, KUBE, append, guard, pods, run, sql
from websockets.asyncio.client import connect

TOPIC = "chat.message-created.v1"


def assess(expected, peers):
    """Independent dictionary oracle; never import application serializers."""
    failures, delays = [], []
    for peer in peers:
        want = {
            mid: row for mid, row in expected.items() if row["room"] == peer["room"]
        }
        seen = Counter(row["mid"] for row in peer["received"])
        if set(seen) != set(want):
            failures.append("missing_or_unexpected")
        if any(n != 1 for n in seen.values()):
            failures.append("duplicate")
        seqs = [r["seq"] for r in peer["received"]]
        if seqs != sorted(seqs):
            failures.append("order")
        for row in peer["received"]:
            original = want.get(row["mid"])
            if original is None:
                continue
            if (
                any(row[k] != original[k] for k in ("room", "seq", "hash", "cid"))
                or row["sender"] != ACTOR
            ):
                failures.append("identity_or_body")
            delays.append(row["received_monotonic"] - original["started_monotonic"])
        if peer.get("error"):
            failures.append("receiver_error")
    if not peers or not expected or not delays or any(d < 0 for d in delays):
        failures.append("invalid_evidence")
    delays.sort()
    return {
        "failures": sorted(set(failures)),
        "receipts": sum(len(p["received"]) for p in peers),
        "ws_p99_seconds": delays[math.ceil(len(delays) * 0.99) - 1] if delays else None,
    }


async def main(rate, *, http_keepalive=True, http_keepalive_expiry=5):
    baseline = pods()
    guard(baseline)
    gateways = sorted(
        p[0] for p in baseline.values() if p[0].startswith("chat-gateway-")
    )
    if len(gateways) != 2:
        raise ValueError("two_gateways_required")
    forwards, receivers, peers = [], [], []
    forward_logs = []
    consumer = AIOKafkaConsumer(
        TOPIC,
        bootstrap_servers="127.0.0.1:19092",
        group_id=None,
        enable_auto_commit=False,
    )
    folder = None
    try:
        for pod, port in zip(gateways, (18094, 18095), strict=True):
            with socket.socket() as check:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                check.bind(("127.0.0.1", port))
            forwards.append(
                await asyncio.to_thread(
                    subprocess.Popen,
                    KUBE
                    + [
                        "port-forward",
                        "pod/" + pod,
                        f"{port}:18082",
                        "--address",
                        "127.0.0.1",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            )
        await consumer.start()
        partitions = consumer.partitions_for_topic(TOPIC)
        if not partitions:
            raise ValueError("kafka_metadata")
        consumer.unsubscribe()
        tps = [TopicPartition(TOPIC, p) for p in sorted(partitions)]
        consumer.assign(tps)
        starts = await consumer.end_offsets(tps)
        for tp in tps:
            consumer.seek(tp, starts[tp])
        async with AsyncExitStack() as stack:

            async def receive(ws, peer):
                try:
                    async for raw in ws:
                        event = json.loads(raw)
                        if event.get("type") == "heads":
                            continue
                        if (
                            event.get("type") != "message.created"
                            or event.get("schema_version") != 1
                        ):
                            raise ValueError("unexpected_frame")
                        m = event["message"]
                        if (
                            event["event_id"] != m["message_id"]
                            or len(peer["received"]) >= 1000
                        ):
                            raise ValueError("event_identity_or_budget")
                        row = {
                            "mid": m["message_id"],
                            "room": m["conversation_id"],
                            "cid": m["client_message_id"],
                            "seq": int(m["seq"]),
                            "sender": m["sender_id"],
                            "hash": hashlib.sha256(m["text"].encode()).hexdigest(),
                            "received_monotonic": time.monotonic(),
                        }
                        peer["received"].append(row)
                        append(
                            folder / "ws-receipts.jsonl", {"peer": peer["id"], **row}
                        )
                    peer["error"] = "unexpected_close"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — persist type only, no cookies/frames
                    peer["error"] = type(exc).__name__

            async def ready(client, rooms, directory):
                nonlocal folder
                folder = directory
                cookies = [client.cookies.get("chat_session")]
                async with httpx.AsyncClient(
                    base_url=str(client.base_url),
                    trust_env=False,
                    timeout=3,
                    limits=httpx.Limits(
                        max_keepalive_connections=20 if http_keepalive else 0,
                        keepalive_expiry=http_keepalive_expiry,
                    ),
                    headers={
                        "Host": "127.0.0.1:18082",
                        "Origin": "http://127.0.0.1:18083",
                    },
                ) as other:
                    response = await other.post(
                        "/v1/dev/session", json={"user": "user_b"}
                    )
                    if (
                        response.status_code != 200
                        or response.json()["data"]["user_id"]
                        != "00000000-0000-4000-8000-000000000002"
                    ):
                        raise ValueError("second_actor")
                    cookies.append(other.cookies.get("chat_session"))
                if not all(cookies):
                    raise ValueError("session_cookie")
                for room in rooms:
                    for index, cookie in enumerate(cookies):
                        ws = await stack.enter_async_context(
                            connect(
                                "ws://127.0.0.1:18082/v1/ws",
                                host="127.0.0.1",
                                port=18094 + index,
                                origin="http://127.0.0.1:18083",
                                additional_headers={"Cookie": "chat_session=" + cookie},
                                proxy=None,
                                compression=None,
                                max_size=16384,
                                max_queue=64,
                                open_timeout=5,
                                close_timeout=2,
                            )
                        )
                        await ws.send(
                            json.dumps({"type": "subscribe", "conversation_id": room})
                        )
                        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
                        if (
                            frame.get("type") != "subscribed"
                            or frame.get("conversation_id") != room
                            or int(frame["head_seq"]) != 0
                        ):
                            raise ValueError("subscription_barrier")
                        peer = {
                            "id": f"{room}-{index}",
                            "room": room,
                            "gateway": gateways[index],
                            "received": [],
                        }
                        peers.append(peer)
                        receivers.append(asyncio.create_task(receive(ws, peer)))
                (folder / "ws-targets.json").write_text(
                    json.dumps(
                        {
                            "gateways": gateways,
                            "baseline": baseline,
                            "peers": [
                                {k: v for k, v in p.items() if k != "received"}
                                for p in peers
                            ],
                        },
                        indent=2,
                    )
                )

            result = await run(
                rate,
                60,
                10,
                before_send=ready,
                http_keepalive=http_keepalive,
                http_keepalive_expiry=http_keepalive_expiry,
            )
            if folder is None or "checks" not in result:
                raise ValueError("http_setup_or_measurement_incomplete")
            primary = json.loads((folder / "primary.json").read_text())
            requests = [
                json.loads(x)
                for x in (folder / "requests.jsonl").read_text().splitlines()
            ]
            sent = {x["cid"]: x for x in requests if x["status"] == "ack"}
            expected = {
                r["mid"]: {**r, "started_monotonic": sent[cid]["started_monotonic"]}
                for cid, r in primary.items()
                if cid in sent
            }
            room_ids = sorted({p["room"] for p in peers})
            room_sql = ",".join("'" + r + "'::uuid" for r in room_ids)
            query = f"SELECT coalesce(json_agg(o.event_id::text),'[]') FROM chat.message_outbox o JOIN chat.messages m ON m.id=o.event_id WHERE m.conversation_id IN ({room_sql}) AND o.published_at IS NOT NULL;"
            deadline, next_guard = time.monotonic() + 60, 0
            while True:
                published = set(await asyncio.to_thread(sql, query))
                verdict = assess(expected, peers)
                if published == set(expected) and not verdict["failures"]:
                    break
                if time.monotonic() >= deadline or any(p.get("error") for p in peers):
                    break
                if time.monotonic() >= next_guard:
                    append(
                        folder / "e2e-resources.jsonl",
                        await asyncio.to_thread(guard, baseline),
                    )
                    next_guard = time.monotonic() + 5
                await asyncio.sleep(0.2)
            ends = await consumer.end_offsets(tps)
            kafka = Counter()
            kafka_bad = 0
            async with asyncio.timeout(30):
                while not all([await consumer.position(tp) >= ends[tp] for tp in tps]):
                    batch = await consumer.getmany(timeout_ms=500, max_records=500)
                    for tp, items in batch.items():
                        for item in items:
                            if item.offset >= ends[tp]:
                                continue
                            event = json.loads(item.value)
                            m = event.get("message", {})
                            if m.get("conversation_id") not in room_ids:
                                continue
                            mid = event.get("event_id")
                            kafka[mid] += 1
                            want = expected.get(mid)
                            valid = (
                                want is not None
                                and m.get("message_id") == mid
                                and item.key == want["room"].encode()
                                and int(m["seq"]) == want["seq"]
                                and hashlib.sha256(m["text"].encode()).hexdigest()
                                == HASH
                            )
                            kafka_bad += not valid
            # Freeze receiver state while sockets are still open; close is cleanup, not a failure.
            for task in receivers:
                task.cancel()
            await asyncio.gather(*receivers, return_exceptions=True)
            verdict = assess(expected, peers)
            proof = {
                **verdict,
                "rate": rate,
                "rooms": 10,
                "sockets": len(peers),
                "expected_messages": rate * 60,
                "ack": len(sent),
                "primary": len(primary),
                "replica": result.get("replica_stored"),
                "published": len(published),
                "kafka_unique": len(kafka),
                "kafka_invalid": kafka_bad,
                "kafka_duplicates": sum(n - 1 for n in kafka.values()),
                "ack_p99_seconds": result.get("ack_p99"),
                "http_result": result["status"],
                "gateways": gateways,
                "browser_verified": False,
                "http_keepalive": http_keepalive,
                "http_keepalive_expiry": http_keepalive_expiry,
                "forward_alive": result.get("forward_exit_before_cleanup") is None
                and all(f.poll() is None for f in forwards),
                "offsets": {str(tp.partition): [starts[tp], ends[tp]] for tp in tps},
            }
            proof["correct"] = (
                len(peers) == 20
                and not verdict["failures"]
                and len(sent) == rate * 60
                and published == set(expected) == set(kafka)
                and not kafka_bad
                and proof["kafka_duplicates"] == 0
                and all(result["checks"].values())
            )
            proof["pass"] = (
                proof["correct"]
                and proof["forward_alive"]
                and result["status"] == "pass"
                and result["ack_p99"] <= 0.5
                and verdict["ws_p99_seconds"] <= 1
            )
            append(
                folder / "e2e-resources.jsonl", await asyncio.to_thread(guard, baseline)
            )
            (folder / "e2e-proof.json").write_text(json.dumps(proof, indent=2))
            print(json.dumps({"folder": str(folder), **proof}), flush=True)
            return proof["pass"]
    finally:
        for task in receivers:
            task.cancel()
        await asyncio.gather(*receivers, return_exceptions=True)
        await consumer.stop()
        for forward in forwards:
            forward.terminate()
            _, stderr = await asyncio.to_thread(forward.communicate, timeout=8)
            forward_logs.append(stderr.decode(errors="replace"))
        if folder is not None:
            for index, log in enumerate(forward_logs):
                (folder / f"gateway-{index}-forward.log").write_text(log)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=int, choices=(10, 25, 50), default=10)
    parser.add_argument("--no-http-keepalive", action="store_true")
    parser.add_argument("--http-keepalive-expiry", type=int, choices=(1, 5), default=5)
    args = parser.parse_args()
    try:
        passed = asyncio.run(
            main(
                args.rate,
                http_keepalive=not args.no_http_keepalive,
                http_keepalive_expiry=args.http_keepalive_expiry,
            )
        )
    except Exception as exc:  # noqa: BLE001 — do not expose credentials in traceback
        print(
            json.dumps(
                {
                    "status": "incomplete",
                    "error_type": type(exc).__name__,
                    "errno": getattr(exc, "errno", None),
                }
            )
        )
        raise SystemExit(2) from None
    raise SystemExit(0 if passed else 1)
