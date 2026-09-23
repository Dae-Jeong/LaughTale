"""Bounded Relay SELECT study on session-local tables, never live Outbox rows."""

import asyncio
import json
import subprocess
from uuid import uuid4

from chat_service.repositories.relay import RelayRepository
from lock_probe import ROOT, guard, pods
from query_probe import CaptureSession, pg, statement


async def run():
    folder = ROOT / ".artifacts/chat-query" / str(uuid4())
    folder.mkdir(parents=True, exist_ok=False)
    query = (await statement(RelayRepository(CaptureSession()).claim())).replace(
        "chat.", "pg_temp."
    )
    (folder / "query.sql").write_text(query)
    results = []
    for pending in (1000, 9000):
        for layout in ("one", "ten", "skew"):
            for leased in (False, True):
                guard(pods())
                room = {
                    "one": "1",
                    "ten": "1 + (g-1)%10",
                    "skew": "CASE WHEN (g-1)%10<9 THEN 1 ELSE 2+(g/10)%9 END",
                }[layout]
                setup = f"""
                BEGIN;
                SET LOCAL statement_timeout='2s';
                SET LOCAL lock_timeout='200ms';
                CREATE TEMP TABLE messages (LIKE chat.messages INCLUDING ALL) ON COMMIT DROP;
                CREATE TEMP TABLE message_outbox (LIKE chat.message_outbox INCLUDING ALL) ON COMMIT DROP;
                INSERT INTO messages(id,conversation_id,sender_id,client_message_id,seq,text,payload_version,payload_hash,created_at)
                SELECT md5(g::text)::uuid,md5(({room})::text)::uuid,md5('actor')::uuid,
                md5(g::text)::uuid,g,repeat('x',1024),1,repeat('a',64),
                '2026-01-01'::timestamptz+g*interval '1 millisecond'
                FROM generate_series(1,10000) g;
                INSERT INTO message_outbox(event_id,payload,published_at,created_at)
                SELECT id,jsonb_build_object('text',text),
                CASE WHEN seq>10000-{pending} THEN NULL ELSE now() END,created_at FROM messages;
                """
                if leased:
                    setup += """
                    UPDATE message_outbox SET lease_until=now()+interval '1 hour'
                    WHERE event_id IN (
                      SELECT DISTINCT ON (m.conversation_id) m.id
                      FROM messages m JOIN message_outbox o ON o.event_id=m.id
                      WHERE o.published_at IS NULL ORDER BY m.conversation_id,m.seq
                    );
                    """
                setup += "ANALYZE messages; ANALYZE message_outbox;"
                case = {"pending": pending, "layout": layout, "heads_leased": leased}
                name = f"{pending}-{layout}-{leased}"
                try:
                    raw = pg(
                        setup
                        + "SELECT 'setup_complete'; EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) "
                        + query
                        + ";ROLLBACK;"
                    )
                    raw = raw.removeprefix("setup_complete\n")
                    (folder / f"{name}.json").write_text(raw)
                    plan = json.loads(raw)[0]
                    case.update(
                        status="measured",
                        execution_ms=plan["Execution Time"],
                        rows=plan["Plan"]["Actual Rows"],
                        local_read_blocks=plan["Plan"].get("Local Read Blocks", 0),
                        local_hit_blocks=plan["Plan"].get("Local Hit Blocks", 0),
                    )
                    expected = 0 if leased else 1
                    if case["rows"] != expected:
                        raise ValueError("unexpected_candidate_count")
                except subprocess.CalledProcessError as error:
                    # SQL/statement-timeout errors are not latency measurements.
                    case.update(
                        status=(
                            "statement_timeout"
                            if "canceling statement due to statement timeout"
                            in (error.stderr or "")
                            else "sql_failed"
                        )
                    )
                    case["failure_stage"] = (
                        "explain"
                        if "setup_complete" in (error.stdout or "")
                        else "fixture"
                    )
                except subprocess.TimeoutExpired:
                    case.update(status="session_timeout")
                results.append(case)
                (folder / "result.json").write_text(json.dumps(results, indent=2))
                print(json.dumps(case), flush=True)
    print(f"Evidence: {folder}")


if __name__ == "__main__":
    asyncio.run(run())
