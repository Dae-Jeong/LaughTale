"""단일 내부 DM에 실제 HTTP 요청을 보내는 bounded open-loop 실험입니다."""

import argparse
import asyncio
import hashlib
import json
import math
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

ROOM = "00000000-0000-4000-8000-000000000010"
SENDER = "00000000-0000-4000-8000-000000000001"
STAGES = (5, 20, 50, 100)


def percentile(values: list[float], quantile: float) -> float | None:
    return (
        sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)]
        if values
        else None
    )


def make_plan(run_id: str, seconds: int = 15) -> list[dict]:
    if not 1 <= seconds <= 15:
        raise ValueError("STAGE_SECONDS_LIMIT")
    return [
        {
            "attempt_id": stage * 100000 + number,
            "run_id": run_id,
            "client_message_id": str(uuid4()),
            "conversation_id": ROOM,
            "sender_id": SENDER,
            "stage_rps": rate,
            "scheduled_seconds": stage * seconds + number / rate,
            "text_sha256": hashlib.sha256(
                text_for(run_id, stage * 100000 + number).encode()
            ).hexdigest(),
        }
        for stage, rate in enumerate(STAGES)
        for number in range(rate * seconds)
    ]


def text_for(run_id: str, attempt_id: int) -> str:
    return f"Synthetic internal DM load {run_id} request {attempt_id}"


def validate_ack(body: object, item: dict) -> dict:
    from chat_service.schemas.chat import StoredMessageData
    from chat_service.schemas.responses import Success

    if (
        not isinstance(body, dict)
        or set(body) != {"data"}
        or not isinstance(body["data"], dict)
        or body["data"].get("state") != "stored"
    ):
        raise ValueError("INVALID_ACK_ENVELOPE")
    message = Success[StoredMessageData].model_validate(body).data
    if (
        str(message.client_message_id) != item["client_message_id"]
        or str(message.conversation_id) != item["conversation_id"]
        or str(message.sender_id) != item["sender_id"]
        or hashlib.sha256(message.text.encode()).hexdigest() != item["text_sha256"]
    ):
        raise ValueError("ACK_IDENTITY_OR_BODY_MISMATCH")
    return {"response_message_id": str(message.message_id), "response_seq": message.seq}


def endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 18092
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("ONLY_DEDICATED_LOOPBACK_18092_ALLOWED")
    return value


class Evidence:
    def __init__(self, path: Path, plan: list[dict], run_id: str) -> None:
        path.mkdir(parents=True, exist_ok=False)
        self.path, self.plan, self.run_id = path, plan, run_id
        (path / "live").mkdir()
        self.rows: list[dict] = []
        self.samples: list[dict] = []
        self.started = time.monotonic()
        self.offered = 0
        self.attempted = 0
        self.inflight = 0
        self.status = "preflight"
        self.stop_reason: str | None = None
        self.previous_attempted = 0
        self.previous_created = 0
        self.previous_offered = 0
        self.previous_sample = 0.0
        self.journal = (path / "requests.jsonl").open(
            "x", encoding="utf-8", buffering=1
        )
        self.sample_journal = (path / "samples.jsonl").open(
            "x", encoding="utf-8", buffering=1
        )
        with (path / "plan.jsonl").open("x", encoding="utf-8") as stream:
            for item in plan:
                stream.write(json.dumps(item) + "\n")
        with (path / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "run_id": run_id,
                    "created_at": datetime.now(UTC).isoformat(),
                    "planned_count": len(plan),
                    "conversation_id": ROOM,
                    "sender_id": SENDER,
                    "rates": STAGES,
                    "max_inflight": 32,
                    "request_timeout_seconds": 3,
                    "max_run_seconds": 90,
                    "raw_text_or_cookies_recorded": False,
                },
                stream,
                indent=2,
            )

    def record(self, value: dict) -> None:
        self.rows.append(value)
        self.journal.write(json.dumps(value) + "\n")

    def snapshot(self) -> dict:
        elapsed = time.monotonic() - self.started
        counts = Counter(row["outcome"] for row in self.rows)
        latencies = [
            row["latency_seconds"] * 1000
            for row in self.rows
            if row.get("latency_seconds") is not None
        ]
        lags = [
            row["scheduler_lag_seconds"] * 1000
            for row in self.rows
            if row.get("scheduler_lag_seconds") is not None
        ]
        interval = elapsed - self.previous_sample
        created = sum(row.get("ack_status") == 201 for row in self.rows)
        sample = {
            "elapsed_seconds": round(elapsed, 3),
            "offered_rps": (self.offered - self.previous_offered) / interval
            if interval > 0
            else 0,
            "attempted_rps": (self.attempted - self.previous_attempted) / interval
            if interval > 0
            else 0,
            "inflight": self.inflight,
            "created_rps": (created - self.previous_created) / interval if interval > 0 else 0,
            "p95_ms": percentile(latencies, 0.95),
            "p99_ms": percentile(latencies, 0.99),
        }
        self.previous_sample, self.previous_offered, self.previous_attempted = (
            elapsed,
            self.offered,
            self.attempted,
        )
        self.previous_created = created
        self.samples.append(sample)
        self.samples = self.samples[-100:]
        value = {
            "run_id": self.run_id,
            "updated_at": datetime.now(UTC).isoformat(),
            "live": True,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "elapsed_seconds": elapsed,
            "planned": len(self.plan),
            "offered": self.offered,
            "attempted": self.attempted,
            "created_201": sum(row.get("ack_status") == 201 for row in self.rows),
            "replayed_200": sum(row.get("ack_status") == 200 for row in self.rows),
            "error": counts["rejected"],
            "unknown": counts["unknown"],
            "dropped": sum(
                row.get("reason") in {"inflight_limit", "scheduler_lag"}
                for row in self.rows
            ),
            "not_attempted": counts["not_attempted"],
            "inflight": self.inflight,
            "p95_ms": percentile(latencies, 0.95),
            "p99_ms": percentile(latencies, 0.99),
            "scheduler_lag_p95_ms": percentile(lags, 0.95),
            "samples": self.samples,
            "db_kafka_validation": "pending_independent_reconciliation",
        }
        self.sample_journal.write(json.dumps(sample) + "\n")
        temporary = self.path / "live" / "status.next"
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(self.path / "live" / "status.json")
        return value

    def finish(self) -> dict:
        complete = self.stop_reason is None and len(self.rows) == len(self.plan)
        self.status = "complete" if complete else "stopped"
        self.snapshot()
        counts = Counter(row["outcome"] for row in self.rows)
        result = {
            "run_id": self.run_id,
            "complete": complete,
            "stop_reason": self.stop_reason,
            "planned_count": len(self.plan),
            "passed": complete and counts["acknowledged"] == len(self.plan),
            "counts": dict(counts),
            "attempts": self.rows,
            "db_kafka_validation": "pending_independent_reconciliation",
        }
        with (self.path / "final.json").open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2)
        self.journal.close()
        self.sample_journal.close()
        return result


async def request(client, path: str, body: dict | None = None) -> tuple[int, object]:
    async with asyncio.timeout(3):
        async with client.stream(
            "GET" if body is None else "POST", path, json=body
        ) as response:
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 16384:
                    raise ValueError("RESPONSE_LIMIT")
            return response.status_code, json.loads(raw)


async def execute(args: argparse.Namespace) -> dict:
    import httpx2 as httpx
    from chat_service.schemas.chat import ActorData, ConversationData
    from chat_service.schemas.responses import Success

    run_id = str(uuid4())
    plan = make_plan(run_id, args.stage_seconds)
    if len(plan) > args.max_requests:
        raise ValueError("PLAN_EXCEEDS_MAX_REQUESTS")
    evidence = Evidence(Path(args.run_dir), plan, run_id)
    evidence.snapshot()
    pending: set[asyncio.Task] = set()
    all_tasks: list[asyncio.Task] = []
    sampling = True
    absolute_start = time.monotonic()

    async def sampler() -> None:
        while sampling:
            await asyncio.sleep(1)
            if (evidence.path / "STOP").exists():
                evidence.stop_reason = "operator_stop"
            if time.monotonic() - absolute_start >= 90:
                evidence.stop_reason = "max_run_seconds"
            if sampling:
                evidence.snapshot()

    async def send(client, item: dict) -> None:
        actual = time.monotonic() - evidence.started
        record = {
            **item,
            "actual_start_seconds": actual,
            "scheduler_lag_seconds": max(0, actual - item["scheduled_seconds"]),
            "ack_status": None,
            "http_status": None,
            "outcome": "unknown",
            "reason": "transport_error",
        }
        try:
            status, body = await request(
                client,
                f"/v1/internal-conversations/{ROOM}/messages",
                {
                    "client_message_id": item["client_message_id"],
                    "text": text_for(run_id, item["attempt_id"]),
                },
            )
            record["http_status"] = status
            if status in (200, 201):
                record.update(
                    validate_ack(body, item),
                    ack_status=status,
                    outcome="acknowledged",
                    reason=None,
                )
            elif 400 <= status < 500 and status != 408:
                record.update(outcome="rejected", reason="http_rejected")
            else:
                record.update(reason="http_result_uncertain")
        except asyncio.CancelledError:
            record["reason"] = "cancelled_after_dispatch"
            raise
        except (httpx.HTTPError, OSError, TimeoutError, ValueError):
            record["reason"] = "transport_timeout_or_invalid_contract"
        finally:
            record["latency_seconds"] = time.monotonic() - evidence.started - actual
            record["completed_seconds"] = time.monotonic() - evidence.started
            evidence.record(record)
            evidence.inflight -= 1

    poll = asyncio.create_task(sampler())
    try:
        async with httpx.AsyncClient(
            base_url=endpoint(args.api_origin),
            trust_env=False,
            follow_redirects=False,
            timeout=3,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=32),
            headers={"Origin": "http://127.0.0.1:18083", "Host": "127.0.0.1:18082"},
        ) as client:
            status, body = await request(client, "/v1/dev/session", {"user": "user_a"})
            if (
                status != 200
                or str(Success[ActorData].model_validate(body).data.user_id) != SENDER
            ):
                raise ValueError("SESSION_PREFLIGHT_FAILED")
            status, body = await request(client, "/v1/internal-conversations")
            if status != 200 or not any(
                str(room.conversation_id) == ROOM and room.kind == "dm"
                for room in Success[list[ConversationData]].model_validate(body).data
            ):
                raise ValueError("ROOM_PREFLIGHT_FAILED")
            evidence.started = time.monotonic()
            evidence.previous_sample = 0
            evidence.samples.clear()
            evidence.status = "running"
            evidence.snapshot()
            for item in plan:
                if (evidence.path / "STOP").exists():
                    evidence.stop_reason = "operator_stop"
                if time.monotonic() - absolute_start >= 90:
                    evidence.stop_reason = "max_run_seconds"
                while (
                    time.monotonic() - evidence.started < item["scheduled_seconds"]
                    and not evidence.stop_reason
                ):
                    await asyncio.sleep(
                        min(
                            0.05,
                            item["scheduled_seconds"]
                            - (time.monotonic() - evidence.started),
                        )
                    )
                    if (evidence.path / "STOP").exists():
                        evidence.stop_reason = "operator_stop"
                if evidence.stop_reason:
                    break
                evidence.offered += 1
                lag = time.monotonic() - evidence.started - item["scheduled_seconds"]
                if len(pending) >= 32 or lag > 0.5:
                    evidence.record(
                        {
                            **item,
                            "outcome": "not_attempted",
                            "reason": "inflight_limit"
                            if len(pending) >= 32
                            else "scheduler_lag",
                            "ack_status": None,
                            "http_status": None,
                            "scheduler_lag_seconds": lag,
                        }
                    )
                    continue
                evidence.attempted += 1
                evidence.inflight += 1
                task = asyncio.create_task(send(client, item))
                pending.add(task)
                task.add_done_callback(pending.discard)
                all_tasks.append(task)
            evidence.status = "draining"
            if all_tasks:
                results = await asyncio.gather(*all_tasks, return_exceptions=True)
                if any(isinstance(result, BaseException) for result in results):
                    evidence.stop_reason = "worker_failed"
    except (httpx.HTTPError, OSError, ValueError, TimeoutError):
        evidence.stop_reason = "preflight_or_execution_failed"
    except asyncio.CancelledError:
        evidence.stop_reason = "cancelled"
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        sampling = False
        poll.cancel()
        await asyncio.gather(poll, return_exceptions=True)
        known = {row["attempt_id"] for row in evidence.rows}
        for item in plan:
            if item["attempt_id"] not in known:
                evidence.stop_reason = evidence.stop_reason or "missing_execution"
                evidence.record(
                    {
                        **item,
                        "outcome": "not_attempted",
                        "reason": evidence.stop_reason or "missing_execution",
                        "ack_status": None,
                        "http_status": None,
                    }
                )
    return evidence.finish()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        required=True,
        help="새 디렉터리만 허용합니다. STOP 파일을 만들면 중단합니다.",
    )
    parser.add_argument("--api-origin", default="http://127.0.0.1:18092", type=endpoint)
    parser.add_argument("--stage-seconds", type=int, default=15, choices=range(1, 16))
    parser.add_argument("--max-requests", type=int, default=4000)
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 4000:
        parser.error("max-requests must be 1..4000")
    return args


if __name__ == "__main__":
    try:
        result = asyncio.run(execute(arguments()))
        print(
            json.dumps(
                {key: value for key, value in result.items() if key != "attempts"}
            )
        )
        raise SystemExit(0 if result["passed"] else 1)
    except (OSError, ValueError):
        raise SystemExit(
            "LOAD_SETUP_FAILED: fresh run directory and loopback target required"
        ) from None
