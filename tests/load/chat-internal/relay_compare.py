"""One-variable TEMP-only Relay comparisons; no production planner/schema changes."""

import argparse
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from uuid import uuid4

from lock_probe import ROOT, guard, pods
from query_probe import pg

HEAD_QUERY = """
WITH heads AS MATERIALIZED (
 SELECT m.conversation_id,min(m.seq) seq
 FROM pg_temp.messages m JOIN pg_temp.message_outbox o ON o.event_id=m.id
 WHERE o.published_at IS NULL GROUP BY m.conversation_id
)
SELECT o.*,m.conversation_id
FROM pg_temp.message_outbox o JOIN pg_temp.messages m ON m.id=o.event_id
JOIN heads h ON h.conversation_id=m.conversation_id AND h.seq=m.seq
WHERE o.published_at IS NULL
AND (o.lease_until IS NULL OR o.lease_until<=clock_timestamp())
AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=clock_timestamp())
ORDER BY o.created_at,o.event_id LIMIT 1 FOR UPDATE OF o SKIP LOCKED
"""


def fixture(layout, state):
    room_sql = {
        "one": "1",
        "ten": "1+(g-1)%10",
        "skew": "CASE WHEN (g-1)%10<9 THEN 1 ELSE 2+(g/10)%9 END",
    }[layout]
    heads = {}
    for g in range(1001, 10001):
        room = 1 if layout == "one" else 1 + (g - 1) % 10
        if layout == "skew":
            room = 1 if (g - 1) % 10 < 9 else 2 + (g // 10) % 9
        heads.setdefault(room, g)
    eligible = sorted(heads.values())
    if state == "leased_all":
        eligible = []
    elif state == "retry_first":
        eligible = eligible[1:]
    expected = hashlib.md5(str(eligible[0]).encode()).hexdigest() if eligible else None
    setup = f"""
    BEGIN; SET LOCAL statement_timeout='2s'; SET LOCAL lock_timeout='200ms';
    CREATE TEMP TABLE messages (LIKE chat.messages INCLUDING ALL) ON COMMIT DROP;
    CREATE TEMP TABLE message_outbox (LIKE chat.message_outbox INCLUDING ALL) ON COMMIT DROP;
    INSERT INTO messages(id,conversation_id,sender_id,client_message_id,seq,text,payload_version,payload_hash,created_at)
    SELECT md5(g::text)::uuid,md5(({room_sql})::text)::uuid,md5('actor')::uuid,
    md5(g::text)::uuid,g,repeat('x',1024),1,repeat('a',64),
    '2026-01-01'::timestamptz+g*interval '1 millisecond' FROM generate_series(1,10000) g;
    INSERT INTO message_outbox(event_id,payload,published_at,created_at)
    SELECT id,jsonb_build_object('text',text),CASE WHEN seq>1000 THEN NULL ELSE now() END,created_at FROM messages;
    """
    if state == "leased_all":
        ids = ",".join(f"md5('{g}')::uuid" for g in heads.values())
        setup += f"UPDATE message_outbox SET lease_until=now()+interval '1 hour' WHERE event_id IN ({ids});"
    elif state == "retry_first":
        setup += "UPDATE message_outbox SET next_attempt_at=now()+interval '1 hour' WHERE event_id=md5('1001')::uuid;"
    return setup + "ANALYZE messages; ANALYZE message_outbox;", expected


def decode(raw):
    decoder = json.JSONDecoder()
    values = []
    rest = raw.removeprefix("setup_complete\n").strip()
    while rest:
        value, end = decoder.raw_decode(rest)
        values.append(value)
        rest = rest[end:].lstrip()
    return values


async def run(variant):
    folder = ROOT / ".artifacts/chat-query" / str(uuid4())
    folder.mkdir(parents=True, exist_ok=False)
    query = Path(__file__).with_name("relay_legacy.sql").read_text()
    if variant == "heads":
        query = HEAD_QUERY
    (folder / "query.sql").write_text(query)
    results = []
    print(f"Evidence: {folder}", flush=True)
    for repeat in range(2):
        layouts = ("one", "ten", "skew") if repeat == 0 else ("skew", "ten", "one")
        for layout in layouts:
            for state in ("ready", "leased_all", "retry_first"):
                guard(pods())
                setup, expected = fixture(layout, state)
                if variant == "order_index":
                    setup += "CREATE INDEX probe_order ON message_outbox(created_at,event_id) WHERE published_at IS NULL;"
                elif variant == "join_plan":
                    setup += "SET LOCAL enable_nestloop=off;"
                case = {
                    "variant": variant,
                    "layout": layout,
                    "state": state,
                    "repeat": repeat,
                }
                try:
                    raw = pg(
                        setup
                        + "SELECT 'setup_complete'; EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) "
                        + query
                        + ";SELECT coalesce(json_agg(row_to_json(s)),'[]') FROM ("
                        + query
                        + ") s;ROLLBACK;"
                    )
                    (folder / f"{repeat}-{layout}-{state}.txt").write_text(raw)
                    plan, selected = decode(raw)
                    ids = [r["event_id"].replace("-", "") for r in selected]
                    case.update(
                        status="measured",
                        correct=ids == ([expected] if expected else []),
                        execution_ms=plan[0]["Execution Time"],
                        nodes=[],
                    )

                    def visit(node, case=case):
                        case["nodes"].append(
                            {
                                k: node[k]
                                for k in (
                                    "Node Type",
                                    "Join Type",
                                    "Index Name",
                                    "Actual Rows",
                                    "Actual Loops",
                                )
                                if k in node
                            }
                        )
                        for child in node.get("Plans", []):
                            visit(child)

                    visit(plan[0]["Plan"])
                    if not case["correct"]:
                        case["status"] = "incorrect_candidate"
                except subprocess.CalledProcessError as error:
                    case["status"] = (
                        "statement_timeout"
                        if "statement timeout" in (error.stderr or "")
                        else "sql_failed"
                    )
                    case["stage"] = (
                        "measurement_or_selection"
                        if "setup_complete" in (error.stdout or "")
                        else "fixture"
                    )
                except subprocess.TimeoutExpired:
                    case["status"] = "session_timeout"
                results.append(case)
                (folder / "result.json").write_text(json.dumps(results, indent=2))
                print(
                    json.dumps({k: v for k, v in case.items() if k != "nodes"}),
                    flush=True,
                )
                if case["status"] in (
                    "sql_failed",
                    "incorrect_candidate",
                    "session_timeout",
                ):
                    raise RuntimeError("comparison_stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "variant", choices=("baseline", "order_index", "join_plan", "heads")
    )
    asyncio.run(run(parser.parse_args().variant))
