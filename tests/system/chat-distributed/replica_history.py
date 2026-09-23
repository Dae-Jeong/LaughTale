"""E4a 실험 전용 읽기입니다. 제품 API 라우팅·세션 인증 구현이 아닙니다."""

import argparse
import asyncio
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import asyncpg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[3]
DB = "laughtale_chat"
IDENTITY_SQL = "SELECT system_identifier::text FROM pg_control_system(); SELECT timeline_id FROM pg_control_checkpoint();"
AUTH_SQL = """SELECT c.last_seq FROM chat.conversations c JOIN chat.members m
ON m.conversation_id=c.id WHERE c.id=$1 AND m.user_id=$2 AND c.kind='dm'"""
HISTORY_SQL = """SELECT id::text AS message_id, conversation_id::text, sender_id::text,
client_message_id::text, seq, encode(sha256(convert_to(text,'UTF8')),'hex') AS text_sha256
FROM chat.messages WHERE conversation_id=$1 AND seq>$2 AND seq<=$3 ORDER BY seq LIMIT $4"""


class ReadUnavailable(Exception):
    pass


def lsn_number(value: str) -> int:
    parts = value.split("/")
    if len(parts) != 2 or any(not part or len(part) > 8 for part in parts):
        raise ValueError("INVALID_LSN")
    if any(any(char not in "0123456789ABCDEF" for char in part) for part in parts):
        raise ValueError("INVALID_LSN")
    return (int(parts[0], 16) << 32) + int(parts[1], 16)


@dataclass(frozen=True)
class Snapshot:
    actor_id: str
    conversation_id: str
    after_seq: int
    head_seq: int
    required_lsn: str
    system_identifier: str
    timeline: int
    captured_at: str

    def validate(self) -> None:
        if (
            str(UUID(self.actor_id)) != self.actor_id
            or str(UUID(self.conversation_id)) != self.conversation_id
        ):
            raise ValueError("NONCANONICAL_ID")
        if not (
            0 <= self.after_seq <= self.head_seq <= 2**63 - 1
            and self.head_seq - self.after_seq <= 100
        ):
            raise ValueError("SNAPSHOT_WINDOW_LIMIT")
        if not self.system_identifier.isdigit() or self.timeline < 1:
            raise ValueError("INVALID_DATABASE_IDENTITY")
        lsn_number(self.required_lsn)


def gate(snapshot: Snapshot, observation: dict) -> str:
    if (
        observation.get("system_identifier") != snapshot.system_identifier
        or observation.get("timeline") != snapshot.timeline
    ):
        return "identity_mismatch"
    if (
        observation.get("in_recovery") is not True
        or observation.get("role") != "chat_reader"
    ):
        return "role_or_recovery_mismatch"
    replay = observation.get("replay_lsn")
    if not isinstance(replay, str):
        return "replay_unknown"
    try:
        return (
            "eligible"
            if lsn_number(replay) >= lsn_number(snapshot.required_lsn)
            else "replay_behind"
        )
    except ValueError:
        return "replay_unknown"


def validate_page(rows: list[dict], after: int, head: int, limit: int) -> int:
    expected = min(limit, head - after)
    if len(rows) != expected or [row["seq"] for row in rows] != list(
        range(after + 1, after + 1 + expected)
    ):
        raise ReadUnavailable("HISTORY_CONTINUITY_VIOLATION")
    if len({row["message_id"] for row in rows}) != len(rows):
        raise ReadUnavailable("DUPLICATE_MESSAGE_ID")
    return rows[-1]["seq"] if rows else after


class SafeHistory:
    def __init__(
        self, primary, replica, replica_identity: dict, *, fallback_limit: int = 10
    ):
        if not 0 <= fallback_limit <= 10:
            raise ValueError("FALLBACK_LIMIT")
        self.primary, self.replica, self.replica_identity = (
            primary,
            replica,
            replica_identity,
        )
        self.fallback_remaining = fallback_limit
        self.serial = asyncio.Lock()

    async def authorize(self, snapshot: Snapshot) -> None:
        async with asyncio.timeout(2):
            head = await self.primary.fetchval(
                AUTH_SQL, UUID(snapshot.conversation_id), UUID(snapshot.actor_id)
            )
        if head is None or head < snapshot.head_seq:
            raise ReadUnavailable("PRIMARY_AUTHORIZATION_OR_HEAD_REJECTED")

    async def observation(self) -> dict:
        if self.replica is None:
            raise ReadUnavailable("REPLICA_CONNECTION_UNAVAILABLE")
        async with asyncio.timeout(0.5):
            row = await self.replica.fetchrow(
                "SELECT pg_is_in_recovery() AS in_recovery, current_user AS role, pg_last_wal_replay_lsn()::text AS replay_lsn"
            )
        return {**self.replica_identity, **dict(row)}

    async def page(self, snapshot: Snapshot, after: int, limit: int = 5) -> dict:
        snapshot.validate()
        if not snapshot.after_seq <= after <= snapshot.head_seq or not 1 <= limit <= 20:
            raise ValueError("INVALID_PAGE_BOUNDARY")
        async with self.serial, asyncio.timeout(5):
            await self.authorize(snapshot)
            observation = {}
            try:
                observation = await self.observation()
                reason = gate(snapshot, observation)
            except (asyncpg.PostgresError, OSError, TimeoutError, ReadUnavailable):
                reason = "replay_observation_failed"
            rows = None
            if reason == "eligible":
                try:
                    # gate SELECT와 동일한 오래된 RR snapshot을 재사용하지 않습니다.
                    # Gate가 완료된 뒤 새 READ COMMITTED 읽기에서 본문을 확인합니다.
                    async with (
                        asyncio.timeout(2),
                        self.replica.transaction(
                            isolation="read_committed", readonly=True
                        ),
                    ):
                        rows = [
                            dict(row)
                            for row in await self.replica.fetch(
                                HISTORY_SQL,
                                UUID(snapshot.conversation_id),
                                after,
                                snapshot.head_seq,
                                limit,
                            )
                        ]
                    validate_page(rows, after, snapshot.head_seq, limit)
                except (asyncpg.PostgresError, OSError, TimeoutError, ReadUnavailable):
                    rows = None
                    reason = "replica_read_failed"
            if rows is None:
                if self.fallback_remaining <= 0:
                    raise ReadUnavailable("PRIMARY_FALLBACK_BUDGET_EXHAUSTED")
                self.fallback_remaining -= 1
                await self.authorize(snapshot)
                async with (
                    asyncio.timeout(2),
                    self.primary.transaction(isolation="read_committed", readonly=True),
                ):
                    rows = [
                        dict(row)
                        for row in await self.primary.fetch(
                            HISTORY_SQL,
                            UUID(snapshot.conversation_id),
                            after,
                            snapshot.head_seq,
                            limit,
                        )
                    ]
                route = "primary_fallback"
            else:
                route = "replica"
            cursor = validate_page(rows, after, snapshot.head_seq, limit)
            return {
                "route": route,
                "reason": reason,
                "observation": observation,
                "rows": rows,
                "next_cursor": cursor,
                "snapshot_head_seq": snapshot.head_seq,
                "fallback_remaining": self.fallback_remaining,
            }


def admin_identity(service: str, port: int) -> dict:
    # 승인된 고정 대상만 확인합니다. 환경변수·비밀번호는 inspect하지 않습니다.
    container = f"laughtale-postgres-lab-{service}-1"
    actual = (
        subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{json .NetworkSettings.Ports}}',
                container,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        .stdout.strip()
        .split("|", 2)
    )
    if actual[:2] != ["laughtale-postgres-lab", service] or json.loads(actual[2])[
        "5432/tcp"
    ] != [{"HostIp": "127.0.0.1", "HostPort": str(port)}]:
        raise ReadUnavailable("UNEXPECTED_LAB_TARGET")
    values = (
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "postgres",
                "-d",
                DB,
                "-X",
                "-At",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                IDENTITY_SQL,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        .stdout.strip()
        .splitlines()
    )
    if len(values) != 2 or not all(value.isdigit() for value in values):
        raise ReadUnavailable("IDENTITY_OBSERVATION_FAILED")
    return {"system_identifier": values[0], "timeline": int(values[1])}


async def connect(port: int):
    values = dotenv_values(ROOT / "infra/postgres/.env")
    return await asyncpg.connect(
        host="127.0.0.1",
        port=port,
        database=DB,
        user="chat_reader",
        password=values["CHAT_READER_PASSWORD"],
        timeout=2,
        command_timeout=2,
        server_settings={
            "application_name": "replica-history-experiment",
            "default_transaction_read_only": "on",
            "statement_timeout": "2000",
        },
    )


async def run(args):
    if args.output.exists():
        raise FileExistsError("FRESH_EVIDENCE_REQUIRED")
    primary_identity = admin_identity("primary", 5440)
    primary = await connect(5440)
    replica = None
    try:
        if await primary.fetchval("SELECT pg_is_in_recovery()") is not False:
            raise ReadUnavailable("PRIMARY_ROLE_CHANGED")
        if args.command == "snapshot":
            head = await primary.fetchval(AUTH_SQL, args.conversation_id, args.actor_id)
            if head is None:
                raise ReadUnavailable("PRIMARY_AUTHORIZATION_REJECTED")
            watermark = await primary.fetchval(
                "SELECT pg_current_wal_insert_lsn()::text"
            )
            snapshot = Snapshot(
                str(args.actor_id),
                str(args.conversation_id),
                max(0, head - args.window),
                head,
                watermark,
                **primary_identity,
                captured_at=datetime.now(UTC).isoformat(),
            )
            snapshot.validate()
            result = asdict(snapshot)
        else:
            replica_port = args.replica_port
            replica_identity = admin_identity(
                "replica" if replica_port == 5441 else "replica-candidate", replica_port
            )
            snapshot = Snapshot(**json.loads(args.snapshot.read_text()))
            snapshot.validate()
            if primary_identity != {
                "system_identifier": snapshot.system_identifier,
                "timeline": snapshot.timeline,
            }:
                raise ReadUnavailable("PRIMARY_IDENTITY_CHANGED")
            try:
                replica = await connect(replica_port)
            except (asyncpg.PostgresError, OSError, TimeoutError):
                replica = None
            reader = SafeHistory(
                primary, replica, replica_identity, fallback_limit=args.fallback_limit
            )
            await reader.authorize(snapshot)
            reference = [
                dict(row)
                for row in await primary.fetch(
                    HISTORY_SQL,
                    UUID(snapshot.conversation_id),
                    snapshot.after_seq,
                    snapshot.head_seq,
                    100,
                )
            ]
            validate_page(reference, snapshot.after_seq, snapshot.head_seq, 100)
            pages, observed, cursor = [], [], snapshot.after_seq
            while cursor < snapshot.head_seq:
                page = await reader.page(snapshot, cursor)
                pages.append(page)
                observed.extend(page["rows"])
                cursor = page["next_cursor"]
            actual_routes = sorted({page["route"] for page in pages})
            result = {
                "status": "pass"
                if observed == reference and actual_routes == [args.expect_route]
                else "failed",
                "scope": "experimental read router only; product API remains Primary",
                "snapshot": asdict(snapshot),
                "reference": reference,
                "pages": pages,
                "rows_match": observed == reference,
                "actual_routes": actual_routes,
                "expected_route": args.expect_route,
            }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
        print(
            json.dumps(
                {
                    "status": result.get("status", "captured"),
                    "scope": "E4a experimental reader",
                    "output": str(args.output),
                }
            )
        )
        return result.get("status") != "failed"
    finally:
        if replica is not None:
            await replica.close()
        await primary.close()


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("snapshot")
    capture.add_argument("--actor-id", required=True, type=UUID)
    capture.add_argument("--conversation-id", required=True, type=UUID)
    capture.add_argument("--window", type=int, choices=range(1, 101), default=20)
    capture.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--snapshot", type=Path, required=True)
    verify.add_argument("--replica-port", type=int, choices=[5441, 5442], default=5441)
    verify.add_argument("--output", type=Path, required=True)
    verify.add_argument("--fallback-limit", type=int, choices=range(11), default=10)
    verify.add_argument(
        "--expect-route", choices=["replica", "primary_fallback"], required=True
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = arguments()

    async def bounded_run():
        async with asyncio.timeout(120):
            return await run(args)

    try:
        raise SystemExit(0 if asyncio.run(bounded_run()) else 1)
    except Exception as error:  # noqa: BLE001 -- CLI must redact DB credentials and driver input.
        evidence = {
            "status": "incomplete",
            "error_class": type(error).__name__,
            "scope": "experimental read router; no product route changed",
        }
        if not args.output.exists():
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x") as stream:
                json.dump(evidence, stream, indent=2)
        print(json.dumps(evidence))
        raise SystemExit(1) from None
