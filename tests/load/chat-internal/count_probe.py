"""Outbox COUNT microbenchmark in a session-local temporary table; no live data edits."""

import json
from uuid import uuid4

from lock_probe import ROOT, guard, pods
from query_probe import pg


def run():
    guard(pods())
    folder = ROOT / ".artifacts/chat-query" / str(uuid4())
    folder.mkdir(parents=True, exist_ok=False)
    sql = """
    SET statement_timeout='10s';
    SET lock_timeout='200ms';
    CREATE TEMP TABLE outbox_count_probe (LIKE chat.message_outbox INCLUDING ALL);
    INSERT INTO outbox_count_probe(event_id,payload,published_at,created_at)
    SELECT md5(g::text)::uuid,jsonb_build_object('text',repeat('x',1024)),now(),now()
    FROM generate_series(1,10000) g;
    """
    cases = []
    for total in (10000, 100000):
        if total == 100000:
            sql += """INSERT INTO outbox_count_probe(event_id,payload,published_at,created_at)
            SELECT md5(g::text)::uuid,jsonb_build_object('text',repeat('x',1024)),now(),now()
            FROM generate_series(10001,100000) g;"""
        for pending in (0, 100, 1000, 9000):
            sql += "UPDATE outbox_count_probe SET published_at=now() WHERE published_at IS NULL;"
            sql += f"UPDATE outbox_count_probe SET published_at=NULL WHERE event_id IN (SELECT md5(g::text)::uuid FROM generate_series(1,{pending}) g);"
            sql += "ANALYZE outbox_count_probe;"
            for repeat in range(3):
                cases.append({"total": total, "pending": pending, "repeat": repeat})
                sql += "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) SELECT count(*) FROM outbox_count_probe WHERE published_at IS NULL;"
    sql += "SELECT json_build_object('temp_relation_bytes',pg_total_relation_size('outbox_count_probe'));"
    # command()'s timeout bounds the whole session; raising it here would need explicit budget review.
    raw = pg(sql)
    (folder / "raw-plans.txt").write_text(raw)
    decoder = json.JSONDecoder()
    values = []
    rest = raw.strip()
    while rest:
        value, end = decoder.raw_decode(rest)
        values.append(value)
        rest = rest[end:].lstrip()
    if len(values) != len(cases) + 1:
        raise ValueError("plan_count_mismatch")
    rows = []
    for case, plan in zip(cases, values[:-1], strict=True):
        root = plan[0]["Plan"]
        scan = root["Plans"][0]
        rows.append(
            case
            | {
                "execution_ms": plan[0]["Execution Time"],
                "scan": scan["Node Type"],
                "rows": scan["Actual Rows"],
                "loops": scan["Actual Loops"],
                "local_hit_blocks": root.get("Local Hit Blocks", 0),
                "local_read_blocks": root.get("Local Read Blocks", 0),
            }
        )
    result = {
        "folder": str(folder),
        "cases": rows,
        "size": values[-1],
        "live_data_modified": False,
    }
    (folder / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    run()
