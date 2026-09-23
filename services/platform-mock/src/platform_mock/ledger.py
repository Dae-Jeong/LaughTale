from dataclasses import dataclass, field
from http import HTTPStatus
from uuid import NAMESPACE_URL, UUID, uuid5

from platform_contracts.wire import (
    AcceptedEffect,
    InboundEvent,
    OutboundCommand,
    RunConfig,
)

from platform_mock.contracts import MockError


class Rejected(Exception):
    def __init__(self, status: HTTPStatus, code: MockError) -> None:
        self.status = status
        self.code = code


@dataclass
class Run:
    config: RunConfig
    events: dict[str, InboundEvent] = field(default_factory=dict)
    operations: dict[UUID, tuple[OutboundCommand, AcceptedEffect]] = field(
        default_factory=dict
    )
    attempts: dict[str, int] = field(default_factory=dict)
    records: list[dict[str, object]] = field(default_factory=list)
    attempt_count: int = 0
    effect_count: int = 0
    active: int = 0

    def record(self, kind: str, **values: object) -> None:
        self.records.append({"cursor": len(self.records) + 1, "kind": kind, **values})

    def start_attempt(self, key: str) -> int:
        if self.attempt_count >= self.config.max_attempts:
            raise Rejected(HTTPStatus.TOO_MANY_REQUESTS, MockError.ATTEMPT_LIMIT)
        self.attempt_count += 1
        self.attempts[key] = self.attempts.get(key, 0) + 1
        return self.attempts[key]

    def register(self, event: InboundEvent) -> bool:
        existing = self.events.get(event.external_event_id)
        if existing is not None:
            if existing != event:
                raise Rejected(HTTPStatus.CONFLICT, MockError.EVENT_CONFLICT)
            return False
        if len(self.events) >= self.config.max_events:
            raise Rejected(HTTPStatus.TOO_MANY_REQUESTS, MockError.EVENT_LIMIT)
        self.events[event.external_event_id] = event
        self.record("event", event=event.model_dump(mode="json"))
        return True

    def accept(self, command: OutboundCommand) -> tuple[AcceptedEffect, bool]:
        existing = self.operations.get(command.outbound_operation_id)
        if existing and self.config.idempotency_supported:
            if existing[0] != command:
                raise Rejected(HTTPStatus.CONFLICT, MockError.OPERATION_CONFLICT)
            return existing[1], False
        if self.effect_count >= self.config.max_effects:
            raise Rejected(HTTPStatus.TOO_MANY_REQUESTS, MockError.EFFECT_LIMIT)
        self.effect_count += 1
        effect = AcceptedEffect(
            effect_id=uuid5(
                NAMESPACE_URL,
                f"{self.config.run_id}:{self.config.seed}:{command.outbound_operation_id}:{self.effect_count}",
            ),
            outbound_operation_id=command.outbound_operation_id,
        )
        self.operations[command.outbound_operation_id] = (command, effect)
        self.record(
            "effect",
            command=command.model_dump(mode="json"),
            effect=effect.model_dump(mode="json"),
        )
        return effect, True


@dataclass
class Ledger:
    runs: dict[str, Run] = field(default_factory=dict)
    active: int = 0

    def get(self, run_id: str) -> Run:
        if run_id not in self.runs:
            raise Rejected(HTTPStatus.NOT_FOUND, MockError.RUN_NOT_FOUND)
        return self.runs[run_id]

    def create(self, config: RunConfig) -> None:
        if config.run_id in self.runs:
            raise Rejected(HTTPStatus.CONFLICT, MockError.RUN_EXISTS)
        if len(self.runs) >= 4:
            raise Rejected(HTTPStatus.TOO_MANY_REQUESTS, MockError.RUN_LIMIT)
        self.runs[config.run_id] = Run(config)

    def enter(self, run: Run) -> None:
        if self.active >= 16:
            raise Rejected(HTTPStatus.TOO_MANY_REQUESTS, MockError.CONCURRENCY_LIMIT)
        self.active += 1
        run.active += 1

    def leave(self, run: Run) -> None:
        self.active -= 1
        run.active -= 1
