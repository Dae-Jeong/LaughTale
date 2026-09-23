"""Capture current repository SELECTs and inspect bounded plans on synthetic IDs."""

import asyncio
import json
from uuid import UUID, uuid4

from chat_service.repositories.chat import ChatRepository
from chat_service.repositories.relay import RelayRepository
from chat_service.repositories.sessions import SessionRepository
from lock_probe import ACTOR, DOCKER, PRIMARY, ROOT, command
from sqlalchemy.dialects import postgresql


class Captured(Exception):
    def __init__(self, statement):
        self.statement = statement


class CaptureSession:
    async def execute(self, statement):
        raise Captured(statement)

    scalar = execute
    scalars = execute


async def statement(awaitable):
    try:
        await awaitable
    except Captured as captured:
        return str(
            captured.statement.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
    raise ValueError("No query captured")


def pg(query):
    return command(
        DOCKER
        + [
            "exec",
            "-i",
            PRIMARY,
            "psql",
            "-U",
            "postgres",
            "-d",
            "laughtale_chat",
            "-XAtq",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input=query,
    )


async def run():
    # This manifest contains only this lab's synthetic IDs, never session tokens.
    manifest = json.loads(
        (
            ROOT / ".artifacts/chat-lock/5eb1812d-e327-4b6a-a6d3-2aaf56993a8e/plan.json"
        ).read_text()
    )
    cid = UUID(manifest["ids"][0])
    room = UUID(manifest["rooms"][str(cid)])
    session = CaptureSession()
    chat = ChatRepository(session)
    queries = {
        "auth_absent_synthetic_hash": await statement(
            SessionRepository(session).resolve_many(("0" * 64,))
        ),
        "room_lock": await statement(
            chat.authorized_head(UUID(ACTOR), room, lock=True)
        ),
        "idempotency_existing": await statement(
            chat.find_message(UUID(ACTOR), room, cid)
        ),
        "relay_claim_select_only": await statement(RelayRepository(session).claim()),
        "outbox_count": "SELECT count(*) FROM chat.message_outbox WHERE published_at IS NULL",
    }
    folder = ROOT / ".artifacts/chat-query" / str(uuid4())
    folder.mkdir(parents=True, exist_ok=False)
    summary = {"folder": str(folder), "queries": {}}
    indexes = json.loads(
        pg(
            "SELECT json_agg(row_to_json(s)) FROM (SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='chat') s;"
        )
    )
    (folder / "indexes.json").write_text(json.dumps(indexes, indent=2))
    for name, query in queries.items():
        plan = json.loads(
            pg(
                "BEGIN; SET LOCAL statement_timeout='2s'; SET LOCAL lock_timeout='200ms'; EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) "
                + query
                + "; ROLLBACK;"
            )
        )
        (folder / (name + ".json")).write_text(json.dumps(plan, indent=2))
        (folder / (name + ".sql")).write_text(query + ";\n")
        nodes = []

        def visit(node, nodes=nodes):
            nodes.append(
                {
                    key: node[key]
                    for key in (
                        "Node Type",
                        "Relation Name",
                        "Index Name",
                        "Actual Rows",
                        "Actual Loops",
                        "Rows Removed by Filter",
                        "Shared Hit Blocks",
                        "Shared Read Blocks",
                    )
                    if key in node
                }
            )
            for child in node.get("Plans", []):
                visit(child)

        visit(plan[0]["Plan"])
        summary["queries"][name] = {
            "execution_ms": plan[0]["Execution Time"],
            "nodes": nodes,
        }
    (folder / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
