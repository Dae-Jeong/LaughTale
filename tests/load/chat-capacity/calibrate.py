"""Bounded TEMP-table storage calibration, never an API throughput benchmark."""

import argparse
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
PRIMARY = "laughtale-postgres-lab-primary-1"
GIB = 1024**3
COUNT = 100_000
TARGET = 10_000_000


def command(*args, timeout=10, input=None):
    result = subprocess.run(
        args, input=input, text=True, capture_output=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise RuntimeError("command_failed:" + args[0])
    return result.stdout.strip()


def psql(sql):
    return command(
        "docker",
        "exec",
        PRIMARY,
        "psql",
        "-X",
        "-qAt",
        "-U",
        "postgres",
        "-d",
        "laughtale_chat",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    )


def preflight():
    state = json.loads(command("docker", "inspect", PRIMARY))[0]
    labels = state["Config"].get("Labels", {})
    if (
        labels.get("com.docker.compose.project") != "laughtale-postgres-lab"
        or labels.get("com.docker.compose.service") != "primary"
        or not state["State"]["Running"]
        or state["State"]["OOMKilled"]
    ):
        raise RuntimeError("dedicated_primary_identity_or_health_failed")
    if psql("SELECT current_database(), pg_is_in_recovery();") != "laughtale_chat|f":
        raise RuntimeError("primary_role_mismatch")
    sample = resources()
    return {"container_id": state["Id"], "resources": sample}


def resources():
    free = shutil.disk_usage(ROOT).free
    pressure = command("memory_pressure", "-Q")
    match = re.search(r"free percentage:\s*(\d+)%", pressure)
    if match is None:
        raise RuntimeError("memory_pressure_unknown")
    memory = int(match.group(1))
    if free < 15 * GIB or memory < 20:
        raise RuntimeError("host_resource_guard")
    vm_free = (
        int(
            command("docker", "exec", PRIMARY, "df", "-Pk", "/var/lib/postgresql/data")
            .splitlines()[-1]
            .split()[3]
        )
        * 1024
    )
    if vm_free < 2 * GIB:
        raise RuntimeError("vm_disk_guard")
    return {
        "host_free_bytes": free,
        "host_memory_free_percent": memory,
        "vm_free_bytes": vm_free,
    }


def projection(samples, host_free, vm_free):
    if not samples or any(s["rows"] <= 0 for s in samples):
        raise ValueError("samples_required")
    pg_per_row = max(
        (s["message_bytes"] + s["outbox_bytes"]) / s["rows"] for s in samples
    )
    event_per_row = max(s["event_bytes"] / s["rows"] for s in samples)
    components = {
        "primary_relations": int(pg_per_row * TARGET),
        "replica_relations": int(pg_per_row * TARGET),
        "kafka_full_uncompressed_events": int(event_per_row * TARGET),
        "compact_evidence_assumption": 256 * TARGET,
    }
    subtotal = sum(components.values())
    total = int(subtotal * 1.25)
    available = min(max(0, host_free - 15 * GIB), max(0, vm_free - 2 * GIB))
    return {
        "target_messages": TARGET,
        "body_bytes": 1024,
        "postgres_bytes_per_message_and_outbox": pg_per_row,
        "event_json_bytes_per_message": event_per_row,
        "components_bytes": components,
        "growth_allowance_bytes": total - subtotal,
        "required_additional_bytes_scenario": total,
        "available_growth_bytes": available,
        "shortfall_bytes": max(0, total - available),
        "decision": "storage_blocked" if total > available else "storage_candidate",
        "assumptions": [
            "10 million new messages; 1KiB bodies; 100 rooms; two physical PG copies",
            "Full uncompressed Kafka event retention is a comparison scenario, not current retention",
            "Compact evidence 256 bytes/event is an unimplemented budget, not an observation",
            "25% growth allowance is not a measured WAL/bloat bound",
            "TEMP relations reproduce columns/indexes, not FK checks, WAL, relay update churn or API latency",
            "Use max sampled bytes/row; extrapolation is not proof of 10M fit or performance",
        ],
    }


def sql_script(application):
    # application is generated internally, never interpolated from CLI input.
    if re.fullmatch(r"capacity_[0-9a-f]{32}", application) is None:
        raise ValueError("invalid_application")
    statements = [
        f"SET application_name='{application}';",
        "BEGIN;",
        "SET LOCAL statement_timeout='90s';",
        "SET LOCAL lock_timeout='2s';",
        "SET LOCAL idle_in_transaction_session_timeout='30s';",
        "SET LOCAL work_mem='4MB'; SET LOCAL temp_buffers='8MB';",
        "SET LOCAL temp_file_limit='256MB';",
        "CREATE TEMP TABLE capacity_messages (LIKE chat.messages INCLUDING ALL) ON COMMIT DROP;",
        "CREATE TEMP TABLE capacity_outbox (LIKE chat.message_outbox INCLUDING ALL) ON COMMIT DROP;",
    ]
    for start in range(1, COUNT + 1, 10_000):
        end = start + 9999
        statements.append(f"""
INSERT INTO pg_temp.capacity_messages
(id,conversation_id,sender_id,client_message_id,seq,text,payload_version,payload_hash,created_at)
SELECT md5('message:'||i)::uuid, md5('room:'||(i%100))::uuid,
md5('actor')::uuid, md5('client:'||i)::uuid, i,
repeat(md5('body:'||i),32), 1, repeat(md5('hash:'||i),2), now()
FROM generate_series({start},{end}) AS g(i);
INSERT INTO pg_temp.capacity_outbox (event_id,payload,attempts,created_at)
SELECT id, jsonb_build_object('type','message.created','event_id',id,'schema_version',1,
'message',jsonb_build_object('message_id',id,'conversation_id',conversation_id,
'sender_id',sender_id,'client_message_id',client_message_id,'seq',seq::text,
'text',text,'created_at',created_at)), 0, created_at
FROM pg_temp.capacity_messages WHERE seq BETWEEN {start} AND {end};
DO $$ BEGIN
IF pg_total_relation_size('pg_temp.capacity_messages') +
pg_total_relation_size('pg_temp.capacity_outbox') > 536870912
THEN RAISE EXCEPTION 'TEMP_RELATION_BUDGET'; END IF;
END $$;
SELECT json_build_object('rows',{end},
'message_bytes',pg_total_relation_size('pg_temp.capacity_messages'),
'message_index_bytes',pg_indexes_size('pg_temp.capacity_messages'),
'outbox_bytes',pg_total_relation_size('pg_temp.capacity_outbox'),
'outbox_index_bytes',pg_indexes_size('pg_temp.capacity_outbox'),
'event_bytes',(SELECT sum(octet_length(payload::text)) FROM pg_temp.capacity_outbox),
'actual_messages',(SELECT count(*) FROM pg_temp.capacity_messages),
'actual_outbox',(SELECT count(*) FROM pg_temp.capacity_outbox),
'body_bytes',(SELECT min(octet_length(text)) FROM pg_temp.capacity_messages));
""")
    statements.append("ROLLBACK;")
    return "\n".join(statements)


def calibrate():
    report = {
        "status": "incomplete",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "TEMP storage calibration only; no API/Kafka/WS traffic",
    }
    application = "capacity_" + uuid4().hex
    process = None
    try:
        report["preflight"] = preflight()
        before = psql(
            "SELECT (SELECT count(*) FROM chat.messages), (SELECT count(*) FROM chat.message_outbox);"
        )
        report["product_counts_before"] = before
        started = time.monotonic()
        process = subprocess.Popen(
            [
                "docker",
                "exec",
                "-i",
                PRIMARY,
                "psql",
                "-X",
                "-qAt",
                "-U",
                "postgres",
                "-d",
                "laughtale_chat",
                "-v",
                "ON_ERROR_STOP=1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process.stdin.write(sql_script(application))
        process.stdin.close()
        process.stdin = None
        report["resource_samples"] = []
        while process.poll() is None:
            if time.monotonic() - started > 300:
                raise RuntimeError("calibration_time_budget")
            report["resource_samples"].append(resources())
            time.sleep(1)
        out, _ = process.communicate(timeout=5)
        if process.returncode:
            raise RuntimeError("calibration_sql_failed")
        samples = [json.loads(line) for line in out.splitlines() if line.strip()]
        if len(samples) != 10 or any(
            s["rows"] != s["actual_messages"]
            or s["rows"] != s["actual_outbox"]
            or s["body_bytes"] != 1024
            for s in samples
        ):
            raise RuntimeError("sample_count_or_payload_mismatch")
        report.update(samples=samples, calibration_seconds=time.monotonic() - started)
        report["product_counts_after"] = psql(
            "SELECT (SELECT count(*) FROM chat.messages), (SELECT count(*) FROM chat.message_outbox);"
        )
        if report["product_counts_after"] != before:
            raise RuntimeError("concurrent_product_change_requires_review")
        final = resources()
        report["projection"] = projection(
            samples, final["host_free_bytes"], final["vm_free_bytes"]
        )
        report["status"] = "calibration_complete"
    except BaseException as error:  # noqa: BLE001 -- Preserve evidence and clean up the owned session on interruption.
        report["error"] = (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__
        )
    finally:
        if process is not None and process.poll() is None:
            try:
                psql(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE application_name='{application}' AND datname='laughtale_chat' AND pid<>pg_backend_pid();"
                )
                process.communicate(timeout=5)
            except Exception:  # noqa: BLE001 -- Never expose raw database errors.
                report["cleanup"] = "unconfirmed_manual_session_check_required"
        try:
            report["session_remaining"] = int(
                psql(
                    f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{application}';"
                )
            )
            if report["session_remaining"]:
                report["status"] = "incomplete"
        except Exception:  # noqa: BLE001 -- Cleanup uncertainty is never a successful result.
            report["status"] = "incomplete"
            report["cleanup"] = "unconfirmed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-temp-write", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    root = ROOT / ".artifacts/chat-capacity"
    if not args.allow_temp_write or not output.is_relative_to(root) or output == root:
        parser.error(
            "Explicit opt-in and fresh output below .artifacts/chat-capacity required"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve before any SQL; an existing run is never overwritten.
    with output.open("x", encoding="utf-8") as handle:
        report = calibrate()
        json.dump(report, handle, indent=2)
    print(
        json.dumps(
            {k: report[k] for k in ("status", "projection", "error") if k in report}
        )
    )
    return 0 if report["status"] == "calibration_complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
