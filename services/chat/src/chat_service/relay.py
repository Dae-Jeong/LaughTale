"""독립 Relay 프로세스입니다. API lifespan과 공유하지 않습니다."""

import asyncio
import json
import logging
import os
import signal
import sys
import threading
from collections import Counter
from time import monotonic

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from chat_service.adapters.kafka_publisher import KafkaPublisher
from chat_service.contracts.relay import PublisherShutdownError, RelayOutcome
from chat_service.core.relay_settings import RelaySettings
from chat_service.services.relay import OutboxRelay


def idle_delay_seconds(value: str) -> float:
    if value not in {"100", "500"}:
        raise ValueError("Unsupported lab idle delay")
    return int(value) / 1000


def poll_delay(outcome: RelayOutcome, idle_delay: float) -> float:
    if outcome == RelayOutcome.IDLE:
        return idle_delay
    return 0.5 if outcome == RelayOutcome.DATABASE_RETRY else 0


async def run(settings: RelaySettings) -> None:
    idle_delay = idle_delay_seconds(os.environ.get("LAB_RELAY_IDLE_MS", "500"))
    lab_stats = "LAB_RELAY_IDLE_MS" in os.environ
    counts = Counter()
    last_report = monotonic()

    def report() -> None:
        print(
            json.dumps(
                {
                    "event": "relay.poll_stats",
                    "at_monotonic": monotonic(),
                    "outcomes": dict(counts),
                }
            ),
            flush=True,
        )

    if lab_stats:
        report()
    engine = create_async_engine(
        settings.db_primary_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=2,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "timeout": 3,
            "server_settings": {
                "application_name": "chat-relay",
                "statement_timeout": "5000",
                "lock_timeout": "1000",
            },
        },
    )
    publisher = KafkaPublisher(settings.kafka_bootstrap_servers)
    relay = OutboxRelay(async_sessionmaker(engine, expire_on_commit=False), publisher)
    stop = asyncio.Event()
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()

    def terminate() -> None:
        if stop.is_set():
            return
        stop.set()
        # CLI 전용 안전장치: SDK의 취소 불응도 종료 예산을 넘기지 않습니다.
        watchdog = threading.Timer(15, lambda: os._exit(1))
        watchdog.daemon = True
        watchdog.start()
        if task is not None:
            task.cancel()

    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, terminate)
    try:
        while not stop.is_set():
            try:
                outcome = await relay.once()
            except PublisherShutdownError:
                # 재사용 불가 sender가 남은 경우 무한 cleanup 대신 lease 복구에 맡깁니다.
                print(
                    '{"event":"relay.failed","code":"PRODUCER_STOP_TIMEOUT"}',
                    flush=True,
                )
                os._exit(1)
            except Exception:
                outcome = RelayOutcome.DATABASE_RETRY
            if lab_stats:
                counts[
                    outcome
                    if outcome
                    in {
                        RelayOutcome.IDLE,
                        RelayOutcome.PUBLISHED,
                        RelayOutcome.RETRY,
                        RelayOutcome.STALE,
                        RelayOutcome.DATABASE_RETRY,
                    }
                    else RelayOutcome.UNKNOWN
                ] += 1
                if monotonic() - last_report >= 5:
                    report()
                    last_report = monotonic()
            if outcome != RelayOutcome.IDLE:
                print(
                    json.dumps({"event": "relay.attempt", "outcome": outcome}),
                    flush=True,
                )
            delay = poll_delay(outcome, idle_delay)
            if delay:
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        if not stop.is_set():
            raise
    finally:
        if lab_stats:
            report()
        try:
            await publisher.close()
            async with asyncio.timeout(3):
                await engine.dispose()
        except Exception:
            os._exit(1)


def main() -> None:
    # 이 프로세스는 allowlisted JSON 결과만 출력하며 SDK/DB 원문을 숨깁니다.
    logging.disable(logging.CRITICAL)
    try:
        settings = RelaySettings()
    except Exception:
        print("Relay settings invalid", file=sys.stderr)
        raise SystemExit(1) from None
    try:
        asyncio.run(run(settings))
    except Exception:
        print("Relay failed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
