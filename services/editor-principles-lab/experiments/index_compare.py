"""Bounded PostgreSQL teaching experiment; stdlib + existing psql only."""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
DATABASE = "editor_principles_lab"
VARIANTS = ("primary_only", "suited", "unsuitable")
SIZES = (1000, 10000)


def validate(host, port, database, owner, limit):
    if host not in ("127.0.0.1", "localhost", "::1") or port != 5434:
        raise ValueError("Only localhost shared test port 5434 is allowed")
    if database != DATABASE:
        raise ValueError("Only dedicated database editor_principles_lab is allowed")
    if type(owner) is not int or not 0 <= owner < 10:
        raise ValueError("owner must be an integer from 0 to 9")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")


def expected_ids(n, owner, limit, updated=False):
    def timestamp(i):
        return (99999 if i == owner + 1 else (i * 37) % 997 + 2000) if updated else (i * 37) % 997
    return sorted((i for i in range(1, n + 1) if (i - 1) % 10 == owner),
                  key=lambda i: (timestamp(i), i), reverse=True)[:limit]


def assert_ids(actual, expected):
    if actual != expected:
        raise ValueError("Ordered result IDs differ from deterministic oracle")


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def build_sql(schema, owner=3, limit=20):
    validate("127.0.0.1", 5434, DATABASE, owner, limit)
    if not re.fullmatch(r"index_lab_[a-f0-9]{12}", schema):
        raise ValueError("Invalid experiment schema")
    sql = ["BEGIN;", "SET LOCAL statement_timeout = '15s';",
           "SET LOCAL lock_timeout = '2s';", "SET LOCAL search_path = pg_catalog;",
           "DO $$ BEGIN IF current_database() <> 'editor_principles_lab' "
           "OR inet_server_addr() IS NULL THEN RAISE EXCEPTION 'Database guard failed'; END IF; END $$;",
           f"CREATE SCHEMA {schema};",
           f"CREATE FUNCTION {schema}.measure(q text) RETURNS jsonb LANGUAGE plpgsql AS $$ "
           "DECLARE result json; BEGIN EXECUTE 'EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' || q INTO result; "
           "RETURN result::jsonb; END $$;",
           "SELECT jsonb_build_object('kind','environment','version',version(),"
           "'database',current_database(),'settings',(SELECT jsonb_object_agg(name,setting) FROM pg_settings "
           "WHERE name IN ('shared_buffers','work_mem','effective_cache_size','random_page_cost',"
           "'seq_page_cost','max_parallel_workers_per_gather','jit','server_encoding')));"
           ]
    for n in SIZES:
        for variant in VARIANTS:
            table = f"{schema}.docs_{n}_{variant}"
            sql.append(f"CREATE TABLE {table} (id integer PRIMARY KEY, owner_id integer NOT NULL, "
                       "updated_at timestamp NOT NULL, title text NOT NULL);")
            if variant == "suited":
                sql.append(f"CREATE INDEX ON {table} (owner_id, updated_at DESC, id DESC);")
            elif variant == "unsuitable":
                sql.append(f"CREATE INDEX ON {table} (title);")
            prefix = f"'rows',{n},'variant','{variant}',"

            def measure(kind, query, extra=""):
                sql.append(f"SELECT jsonb_build_object({prefix}'kind','{kind}',{extra}"
                           f"'explain',{schema}.measure({literal(query)}));")

            measure("insert", f"INSERT INTO {table} SELECT i, (i-1)%10, "
                    f"timestamp '2026-01-01' + ((i*37)%997)*interval '1 second', "
                    f"'document-' || i || repeat('x',160) FROM generate_series(1,{n}) AS g(i)")
            sql.append(f"ANALYZE {table};")
            query = f"SELECT id FROM {table} WHERE owner_id={owner} ORDER BY updated_at DESC,id DESC LIMIT {limit}"
            for repeat in range(6):
                measure("warmup" if repeat == 0 else "read", query, f"'repeat',{repeat},")
            for stage in ("before", "after"):
                if stage == "after":
                    measure("update", f"UPDATE {table} SET updated_at = CASE WHEN id={owner+1} "
                            "THEN timestamp '2026-01-01' + interval '99999 seconds' "
                            f"ELSE updated_at + interval '2000 seconds' END WHERE owner_id={owner}")
                sql.append(f"SELECT jsonb_build_object({prefix}'kind','ids','stage','{stage}',"
                           f"'ids',COALESCE((SELECT jsonb_agg(id ORDER BY updated_at DESC,id DESC) FROM "
                           f"(SELECT id,updated_at FROM {table} WHERE owner_id={owner} "
                           f"ORDER BY updated_at DESC,id DESC LIMIT {limit}) s),'[]'::jsonb));")
                sql.append(f"SELECT jsonb_build_object({prefix}'kind','size','stage','{stage}',"
                           f"'heap_bytes',pg_relation_size('{table}'),'indexes_bytes',pg_indexes_size('{table}'),"
                           f"'total_bytes',pg_total_relation_size('{table}'),"
                           f"'indexes',(SELECT jsonb_agg(jsonb_build_object('definition',pg_get_indexdef(indexrelid),"
                           f"'bytes',pg_relation_size(indexrelid))) FROM pg_index WHERE indrelid='{table}'::regclass));")
    sql.extend(["ROLLBACK;", "SELECT jsonb_build_object('kind','cleanup','schema_absent',"
                f"NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='{schema}')); "])
    return "\n".join(sql)


def check_records(records, owner, limit):
    for n in SIZES:
        for variant in VARIANTS:
            group = [r for r in records if r.get("rows") == n and r.get("variant") == variant]
            for stage in ("before", "after"):
                ids = [r["ids"] for r in group if r["kind"] == "ids" and r["stage"] == stage]
                if len(ids) != 1:
                    raise ValueError("Missing or duplicate correctness evidence")
                assert_ids(ids[0], expected_ids(n, owner, limit, stage == "after"))
            if len([r for r in group if r["kind"] == "read"]) != 5:
                raise ValueError("Expected five measured reads")
    if not records or records[-1] != {"kind": "cleanup", "schema_absent": True}:
        raise ValueError("Rollback cleanup not verified")


def connection_env(host):
    # Prevent ambient PGHOSTADDR/PGSERVICE/PGOPTIONS from rerouting the connection.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    for key in ("PGPASSWORD", "PGPASSFILE"):
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(PGCONNECT_TIMEOUT="5", PGHOSTADDR="::1" if host == "::1" else "127.0.0.1")
    return env


def plan_nodes(node):
    return [node["Node Type"]] + [kind for child in node.get("Plans", []) for kind in plan_nodes(child)]


def markdown(report):
    lines = ["# PostgreSQL index 실험 — 실제 로컬 관측", "",
             f"측정 UTC: {report['measured_at']}", "", f"Engine: {report['records'][0]['version']}", "",
             "동일 ordered IDs를 독립 Python oracle과 대조: before/after 모두 통과. 전용 schema rollback 확인.", "",
             "| 행 수 | 조건 | read 중앙값 ms (min–max) | insert ms | update ms | index bytes (before→after) | total bytes (before→after) | 첫 read plan |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for n in SIZES:
        for variant in VARIANTS:
            group = [r for r in report["records"] if r.get("rows") == n and r.get("variant") == variant]
            reads = [r["explain"][0] for r in group if r["kind"] == "read"]
            times = [r["Execution Time"] for r in reads]
            writes = {r["kind"]: r["explain"][0]["Execution Time"] for r in group if r["kind"] in ("insert", "update")}
            sizes = {r["stage"]: r for r in group if r["kind"] == "size"}
            lines.append(f"| {n} | {variant} | {statistics.median(times):.3f} ({min(times):.3f}–{max(times):.3f}) "
                         f"| {writes['insert']:.3f} | {writes['update']:.3f} "
                         f"| {sizes['before']['indexes_bytes']}→{sizes['after']['indexes_bytes']} "
                         f"| {sizes['before']['total_bytes']}→{sizes['after']['total_bytes']} "
                         f"| {' → '.join(plan_nodes(reads[0]['Plan']))} |")
    lines += ["", "primary_only에도 PRIMARY KEY B-tree는 존재하며 보조 인덱스만 없다.", "",
              "각 조건 warmup 1회 후 read 5회; insert/update는 각 1회인 탐색 관측이다. "
              "EXPLAIN 계측 비용을 포함한 서버 실행 시간이며 연결/전송/commit 시간은 제외된다. "
              "고정 실행 순서, 따뜻한 캐시, 작은 합성 데이터, ANALYZE 표본 및 VM/다른 부하 영향으로 일반화할 수 없다. "
              "Seq Scan이 선택되면 작은 테이블의 순차 읽기+정렬 비용 판단 또는 title 인덱스와 쿼리의 불일치로 해석할 수 있다. "
              "scan 강제 옵션을 사용하지 않았으며 향상 배수는 보장하지 않는다.", "",
              "[실습·예측 질문](../docs/index-experiment.md) · [전체 JSON/BUFFERS/plan](index-results.json)", "",
              "검증자: 작성 agent self-review. 사용자 이해도는 미측정. 회사 내부 구현·경력 성과의 근거가 아니다."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Connect only after shared DB permission")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5434)
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--user", help="Existing PostgreSQL role; never put a password here")
    parser.add_argument("--owner", type=int, default=3)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    validate(args.host, args.port, args.database, args.owner, args.limit)
    if args.user is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", args.user):
        raise ValueError("Use a plain existing role name")
    sql = build_sql("index_lab_" + uuid.uuid4().hex[:12], args.owner, args.limit)
    if not args.run:
        print("READY (offline only): SQL generated and guards validated; no DB connection or measurements.\n"
              "After permission: python3 experiments/index_compare.py --run --user ROLE")
        return
    if not args.user:
        parser.error("--run requires --user for an explicit existing role")
    command = ["psql", "-X", "-q", "-A", "-t", "-w", "-v", "ON_ERROR_STOP=1",
               "-h", args.host, "-p", str(args.port), "-d", args.database, "-U", args.user]
    try:
        result = subprocess.run(command, input=sql, text=True, capture_output=True,
                                env=connection_env(args.host), timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("psql unavailable or timed out; no result published") from None
    if result.returncode:
        # Do not persist raw libpq diagnostics, which may contain authentication details.
        raise RuntimeError(f"psql failed (exit {result.returncode}); no result published; verify dedicated DB/role/access")
    records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    check_records(records, args.owner, args.limit)
    report = {"status": "measured", "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "client": {"os": platform.system(), "release": platform.release(), "machine": platform.machine(),
                         "logical_cpus": os.cpu_count(), "python": platform.python_version()},
              "hardware_limits": "Client CPU count is not Docker allocation; server CPU/RAM/storage allocation unknown",
              "parameters": {"sizes": SIZES, "owner": args.owner, "limit": args.limit, "owners": 10,
                             "seed": "formula i=1..n, owner=(i-1)%10, timestamp_seconds=(i*37)%997",
                             "warmups": 1, "read_repeats": 5, "write_repeats": 1,
                             "variant_order": VARIANTS}, "records": records}
    directory = ROOT / "results"
    directory.mkdir(exist_ok=True)
    (directory / "index-results.json").write_text(json.dumps(report, indent=2) + "\n")
    (directory / "index-results.md").write_text(markdown(report))
    print("Measured and verified: results/index-results.json, results/index-results.md")


if __name__ == "__main__":
    main()
