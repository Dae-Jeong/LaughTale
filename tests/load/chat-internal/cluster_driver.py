"""Bounded in-cluster HTTP/WS/Kafka workload; credentials enter via stdin/env only."""

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from contextlib import AsyncExitStack

import httpx2 as httpx
from aiokafka import AIOKafkaConsumer, TopicPartition
from websockets.asyncio.client import connect

ORIGIN = "http://127.0.0.1:18083"
TOPIC = "chat.message-created.v1"


def subscription_ready(frame, room):
    return (
        frame.get("type") == "subscribed"
        and frame.get("conversation_id") == room
        and str(frame.get("head_seq")) == "0"
    )


def message_row(message):
    return {
        "mid": message["message_id"],
        "cid": message["client_message_id"],
        "room": message["conversation_id"],
        "seq": int(message["seq"]),
        "sender": message["sender_id"],
        "hash": hashlib.sha256(message["text"].encode()).hexdigest(),
    }


async def main(config):
    if config["rate"] != 25 or len(config["rooms"]) != 10 or len(config["ids"]) != 1500:
        raise ValueError("bounded_profile_required")
    result = {"status": "incomplete", "requests": [], "peers": [], "kafka": []}
    tasks, sockets = [], []
    token = os.environ["LAB_INGRESS_TOKEN"]
    headers = {
        "Host": "chat:18082",
        "Origin": ORIGIN,
        "X-Lab-Ingress-Token": token,
        "Cookie": "chat_session=" + config["cookies"][0],
    }
    consumer = AIOKafkaConsumer(
        TOPIC, bootstrap_servers="kafka:9092", group_id=None, enable_auto_commit=False
    )
    try:
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=config["api"],
                    headers=headers,
                    trust_env=False,
                    timeout=3,
                    limits=httpx.Limits(
                        max_connections=32,
                        max_keepalive_connections=20,
                        keepalive_expiry=1,
                    ),
                )
            )
            # Never change the app's security policy for the benchmark.
            bad_token = await client.get(
                "/v1/session", headers={"X-Lab-Ingress-Token": "invalid"}
            )
            no_cookie = await client.get("/v1/session", headers={"Cookie": ""})
            good = await client.get("/v1/session")
            result["security"] = {
                "bad_token": bad_token.status_code,
                "no_cookie": no_cookie.status_code,
                "valid": good.status_code,
            }
            if (
                bad_token.status_code != 403
                or no_cookie.status_code != 401
                or good.status_code != 200
            ):
                raise ValueError("security_control_failed")

            async def metrics():
                samples = {}
                for name, ip in config["apis"].items():
                    r = await client.get(f"http://{ip}:18082/metrics")
                    r.raise_for_status()
                    samples[name] = "\n".join(
                        line
                        for line in r.text.splitlines()
                        if line.startswith(("db_", "http_"))
                    )
                return samples

            async def receiver(ws, peer):
                try:
                    async for raw in ws:
                        event = json.loads(raw)
                        if event.get("type") == "heads":
                            continue
                        if (
                            event.get("type") != "message.created"
                            or event.get("schema_version") != 1
                        ):
                            raise ValueError("unexpected_event")
                        row = message_row(event["message"])
                        if (
                            event["event_id"] != row["mid"]
                            or len(peer["received"]) >= 500
                        ):
                            raise ValueError("event_identity_or_budget")
                        peer["received"].append(
                            {**row, "received_monotonic": time.monotonic()}
                        )
                    peer["error"] = "unexpected_close"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    peer["error"] = type(exc).__name__

            for room in config["rooms"]:
                for index, ip in enumerate(config["gateways"]):
                    ws = await connect(
                        "ws://127.0.0.1:18082/v1/ws",
                        host=ip,
                        port=18082,
                        origin=ORIGIN,
                        additional_headers={
                            "Cookie": "chat_session=" + config["cookies"][index]
                        },
                        proxy=None,
                        compression=None,
                        max_size=16384,
                        max_queue=64,
                        open_timeout=5,
                        close_timeout=2,
                    )
                    sockets.append(ws)
                    await ws.send(
                        json.dumps({"type": "subscribe", "conversation_id": room})
                    )
                    frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    if not subscription_ready(frame, room):
                        raise ValueError("subscription_barrier")
                    peer = {"id": f"{room}-{index}", "room": room, "received": []}
                    result["peers"].append(peer)
                    tasks.append(asyncio.create_task(receiver(ws, peer)))
            await consumer.start()
            parts = consumer.partitions_for_topic(TOPIC)
            if not parts:
                raise ValueError("kafka_metadata")
            consumer.unsubscribe()
            tps = [TopicPartition(TOPIC, p) for p in sorted(parts)]
            consumer.assign(tps)
            starts = await consumer.end_offsets(tps)
            for tp in tps:
                consumer.seek(tp, starts[tp])

            async def kafka_receiver():
                async for event in consumer:
                    payload = json.loads(event.value)
                    m = payload.get("message", {})
                    if m.get("conversation_id") not in config["rooms"]:
                        continue
                    row = message_row(m)
                    if (
                        payload.get("event_id") != row["mid"]
                        or event.key != row["room"].encode()
                        or len(result["kafka"]) >= 3000
                    ):
                        raise ValueError("kafka_identity_or_budget")
                    result["kafka"].append(
                        {
                            **row,
                            "partition": event.partition,
                            "observed_monotonic": time.monotonic(),
                        }
                    )

            tasks.append(asyncio.create_task(kafka_receiver()))
            result["metrics_before"] = await metrics()
            pending = set()

            async def send(cid, room, due):
                start = time.monotonic()
                row = {
                    "cid": cid,
                    "room": room,
                    "status": "unknown",
                    "started_monotonic": start,
                    "dispatch_delay": start - due,
                }
                try:
                    r = await client.post(
                        f"/v1/internal-conversations/{room}/messages",
                        json={"client_message_id": cid, "text": "x" * 1024},
                    )
                    row["http_status"] = r.status_code
                    if r.status_code == 201:
                        row.update(message_row(r.json()["data"]))
                        row.update(status="ack", pod_uid=r.headers.get("X-Lab-Pod-UID"))
                except Exception as exc:  # noqa: BLE001
                    row["error_type"] = type(exc).__name__
                row["seconds"] = time.monotonic() - start
                result["requests"].append(row)

            started = time.monotonic()
            for i, cid in enumerate(config["ids"]):
                due = started + i / config["rate"]
                await asyncio.sleep(max(0, due - time.monotonic()))
                if len(pending) >= 32 or time.monotonic() - due > 0.1:
                    result["requests"].append({"cid": cid, "status": "skipped"})
                    continue
                work = asyncio.create_task(send(cid, config["rooms"][i % 10], due))
                pending.add(work)
                work.add_done_callback(pending.discard)
            await asyncio.gather(*pending)
            result["send_seconds"] = time.monotonic() - started
            result["metrics_after"] = await metrics()
            acks = sum(r["status"] == "ack" for r in result["requests"])
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if any(t.done() for t in tasks):
                    raise ValueError("receiver_stopped")
                if (
                    len(result["kafka"]) >= acks
                    and sum(len(p["received"]) for p in result["peers"]) >= 2 * acks
                ):
                    break
                await asyncio.sleep(0.1)
            result["status"] = "measured"
    except Exception as exc:  # noqa: BLE001 — no exception text, cookies, or payloads
        result["error_type"] = type(exc).__name__
        if isinstance(exc, ValueError) and str(exc) in {
            "security_control_failed",
            "subscription_barrier",
            "kafka_metadata",
            "receiver_stopped",
        }:
            result["error_code"] = str(exc)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)
        await consumer.stop()
    return result


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    config = json.load(sys.stdin)
    print(json.dumps(asyncio.run(main(config))))
