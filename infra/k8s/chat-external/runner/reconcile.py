"""발생기 ACK와 root가 수집한 DB 행·Pod 로그를 독립 대사합니다."""

import argparse
from collections import Counter
import json
from pathlib import Path
from run import audit_attempts


def reconcile(evidence: dict, rows: list[dict], pod_logs: str = "") -> dict:
    execution = audit_attempts(evidence.get("plan", []), evidence["records"])
    keys = [(row["connection_id"], row["external_message_id"]) for row in rows]
    duplicates = sum(count - 1 for count in Counter(keys).values() if count > 1)
    row_keys = set(keys)
    by_message = {row["message_id"]: row for row in rows}
    expected = {(record["connection_id"], record["external_message_id"]) for record in evidence["records"]}
    content_fields = ("text_sha256", "external_sender_id", "external_conversation_id", "profile")
    sources = {}
    for item in evidence.get("plan", []):
        key = (item["connection_id"], item["external_message_id"])
        content = tuple(item[field] for field in content_fields)
        if key in sources and sources[key] != content:
            raise ValueError("CONFLICTING_SOURCE_PLAN")
        sources[key] = content
    content_mismatches = sum(
        (row["connection_id"], row["external_message_id"]) in sources
        and tuple(row.get(field) for field in content_fields) != sources[(row["connection_id"], row["external_message_id"])]
        for row in rows
    )
    if not expected or len(expected) != evidence["summary"]["planned_unique_events"]:
        raise ValueError("INCOMPLETE_GENERATOR_EVIDENCE")
    acknowledged = [record for record in evidence["records"] if record["outcome"] == "acknowledged"]
    missing_ack = []
    for record in acknowledged:
        stored = by_message.get(record["message_id"])
        if stored is None or any(record[field] != stored[field] for field in ("conversation_id", "seq", "connection_id", "external_message_id")):
            missing_ack.append(record["event_id"])
    request_ids = {record["request_id"] for record in acknowledged if record.get("request_id")}
    pod_requests: dict[str, set[str]] = {}
    for line in pod_logs.splitlines():
        prefix, separator, value = line.partition(" {")
        if not separator:
            continue
        try:
            event = json.loads("{" + value)
            request_id = event.get("app", {}).get("work", {}).get("id")
            if request_id in request_ids:
                pod_requests.setdefault(prefix.strip(), set()).add(request_id)
        except (ValueError, AttributeError):
            continue
    return {
        **execution,
        "source_content_or_route_mismatches": content_mismatches,
        "acknowledged_but_missing_or_changed": len(missing_ack), "duplicate_db_messages": duplicates,
        "planned_missing_in_db": len(expected - row_keys), "unexpected_db_messages": len(row_keys - expected),
        "db_rows": len(rows), "pod_ack_counts": {pod: len(ids) for pod, ids in pod_requests.items()},
        "pod_distribution_observed": len(pod_requests),
        "all_acknowledged_requests_mapped_to_pods": request_ids.issubset(set().union(*pod_requests.values())) if pod_requests else False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True)
    parser.add_argument("--db-json", required=True)
    parser.add_argument("--pod-logs")
    args = parser.parse_args()
    evidence = json.loads(Path(args.result).read_text())
    rows = json.loads(Path(args.db_json).read_text())
    logs = Path(args.pod_logs).read_text() if args.pod_logs else ""
    result = reconcile(evidence, rows, logs)
    print(json.dumps(result, indent=2))
    if not result["execution_complete"] or result["observed_failed_attempts"] or any(result[key] for key in ("source_content_or_route_mismatches", "acknowledged_but_missing_or_changed", "duplicate_db_messages", "planned_missing_in_db", "unexpected_db_messages")):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
