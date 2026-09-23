"""격리 실험용 Kafka 발행입니다. 애플리케이션 재발행은 at-least-once입니다."""

import asyncio
import json

from aiokafka import AIOKafkaProducer

from chat_service.contracts.relay import PublisherShutdownError, RelayClaim

TOPIC = "chat.message-created.v1"


class KafkaPublisher:
    def __init__(self, bootstrap: str) -> None:
        self.bootstrap = bootstrap
        self.producer: AIOKafkaProducer | None = None

    async def publish(self, claim: RelayClaim) -> None:
        value = json.dumps(
            claim.payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(value) > 16384:
            raise ValueError("Event byte limit")
        try:
            if self.producer is None:
                self.producer = AIOKafkaProducer(
                    bootstrap_servers=self.bootstrap,
                    client_id="chat-outbox-relay-v1",
                    enable_idempotence=True,
                    acks="all",
                    request_timeout_ms=8000,
                    max_request_size=32768,
                    security_protocol="PLAINTEXT",
                )
                await self.producer.start()
            await self.producer.send_and_wait(
                TOPIC, value=value, key=str(claim.conversation_id).encode("ascii")
            )
        except BaseException:
            # 취소된 await는 broker write를 취소했다고 증명하지 않습니다.
            # sender를 종료한 뒤에만 다음 시도에서 새 producer를 만듭니다.
            await self.close()
            raise

    async def close(self) -> None:
        if self.producer is None:
            return
        producer = self.producer
        stop = asyncio.create_task(producer.stop())
        done, _ = await asyncio.wait({stop}, timeout=3)
        if not done:
            stop.cancel()
            raise PublisherShutdownError("Producer stop deadline")
        await stop
        self.producer = None
