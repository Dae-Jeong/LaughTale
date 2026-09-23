"""20-message integration check, not a throughput benchmark or bulk generator."""

import asyncio
import json
import time
from collections import Counter
from uuid import uuid4

import httpx2 as httpx
from app import CONTEXT, KUBE, NAMESPACE, guarded_cluster, sql
from lab import ROOT, guarded_engine, run
from websockets.asyncio.client import connect

ROOM = "00000000-0000-4000-8000-000000000010"
ORIGIN = "http://127.0.0.1:18083"
PATH = f"/v1/internal-conversations/{ROOM}/messages"


def compare(expected, observed):
    return Counter(expected) == Counter(observed)


async def exercise():
    guarded_engine()
    guarded_cluster()
    if sql("SELECT count(*) FROM chat.messages") != "0":
        raise RuntimeError(
            "Initial smoke requires an empty message store; use the scoped bulk verifier for later runs"
        )
    run_id = str(uuid4())
    expected, received = [], []
    started = time.monotonic()
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:26492",
        trust_env=False,
        timeout=5,
        headers={"Host": "127.0.0.1:18082", "Origin": ORIGIN},
    ) as api:
        anonymous = await api.get("/v1/session")
        if anonymous.status_code != 401:
            raise RuntimeError("Anonymous API access was not denied")
        response = await api.post("/v1/dev/session", json={"user": "user_a"})
        response.raise_for_status()
        cookie = response.cookies["chat_session"]
        async with connect(
            "ws://127.0.0.1:18082/v1/ws",
            host="127.0.0.1",
            port=26494,
            origin=ORIGIN,
            additional_headers={"Cookie": "chat_session=" + cookie},
            proxy=None,
            compression=None,
            max_size=16384,
            max_queue=64,
        ) as ws:
            await ws.send(json.dumps({"type": "subscribe", "conversation_id": ROOM}))
            async with asyncio.timeout(10):
                while json.loads(await ws.recv())["type"] != "subscribed":
                    pass

            async def collect():
                async with asyncio.timeout(30):
                    while len(received) < 20:
                        frame = json.loads(await ws.recv())
                        if frame.get("type") == "message.created":
                            received.append(frame["message"]["client_message_id"])

            receiver = asyncio.create_task(collect())
            try:
                async with httpx.AsyncClient(
                    base_url="http://127.0.0.1:26496",
                    trust_env=False,
                    timeout=5,
                    headers={
                        "Host": "127.0.0.1:18096",
                        "Origin": ORIGIN,
                        "Cookie": "chat_session=" + cookie,
                    },
                ) as proxy:
                    for _ in range(20):
                        client_id = str(uuid4())
                        reply = await proxy.post(
                            PATH,
                            json={"client_message_id": client_id, "text": "x" * 1024},
                        )
                        if reply.status_code != 201:
                            raise RuntimeError(
                                f"Expected new ACK; status={reply.status_code}"
                            )
                        if reply.json()["data"]["client_message_id"] != client_id:
                            raise RuntimeError("ACK identity mismatch")
                        expected.append(client_id)
                        await asyncio.sleep(0.2)
                await receiver
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
    # IDs are generated UUIDs, never untrusted SQL fragments or message bodies.
    values = ",".join(f"'{value}'" for value in expected)
    database = sql(
        f"SELECT client_message_id FROM chat.messages WHERE client_message_id IN ({values})"
    ).splitlines()
    published = sql(
        f"SELECT m.client_message_id FROM chat.messages m JOIN chat.message_outbox o ON o.event_id=m.id WHERE m.client_message_id IN ({values}) AND o.published_at IS NOT NULL"
    ).splitlines()
    raw = await asyncio.to_thread(
        run,
        [
            "docker",
            "--context",
            CONTEXT,
            "exec",
            "laughtale-capacity-kafka-1",
            "/opt/kafka/bin/kafka-console-consumer.sh",
            "--bootstrap-server",
            "127.0.0.1:9092",
            "--topic",
            "chat.message-created.v1",
            "--from-beginning",
            "--max-messages",
            "20",
            "--timeout-ms",
            "10000",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    kafka = [
        json.loads(line)["message"]["client_message_id"]
        for line in raw.stdout.splitlines()
    ]
    checks = {
        "db": compare(expected, database),
        "published": compare(expected, published),
        "kafka": compare(expected, kafka),
        "websocket": compare(expected, received),
    }
    pods = json.loads(
        run(
            KUBE + ["-n", NAMESPACE, "get", "pods", "-o", "json"],
            capture_output=True,
            text=True,
        ).stdout
    )
    checks["no_restarts"] = all(
        status["restartCount"] == 0
        for pod in pods["items"]
        for status in pod["status"].get("containerStatuses", [])
    )
    result = {
        "run_id": run_id,
        "status": "pass" if all(checks.values()) else "fail",
        "ack": len(expected),
        "db": len(database),
        "published": len(published),
        "kafka": len(kafka),
        "websocket": len(received),
        "checks": checks,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "limitations": [
            "20-message smoke only",
            "one subscriber",
            "Kafka first20; fresh topic required",
            "not an open-loop throughput benchmark",
        ],
    }
    path = ROOT / ".artifacts/chat-capacity" / f"smoke-{run_id}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if result["status"] != "pass":
        raise RuntimeError("Smoke reconciliation failed")


if __name__ == "__main__":
    asyncio.run(exercise())
