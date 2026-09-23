"""E1 independent evidence checks: one room/actor, API replica transition only."""

from collections import Counter
from datetime import datetime
from itertools import pairwise
from math import isfinite

from oracle import FIELDS

INITIAL_APPS = {
    "chat": 1,
    "chat-gateway": 2,
    "chat-fanout": 2,
    "chat-relay": 1,
    "chat-growth-proxy": 1,
}
MAX_START_GAP_SECONDS = 1.0
MAX_GENERATOR_LAG_SECONDS = 1.0


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("aware_timestamp_required")
    return parsed.timestamp()


def transition_issues(baseline, current, allow_growth):
    issues = []
    old = {row["uid"]: row for row in baseline["pods"]}
    now = {row["uid"]: row for row in current["pods"]}
    if baseline["containers"] != current["containers"]:
        issues.append("container_identity_or_restart_changed")
    if not set(old) <= set(now):
        issues.append("baseline_pod_missing")
    added = set(now) - set(old)
    if len(added) > int(allow_growth) or any(
        now[uid]["app"] != "chat" for uid in added
    ):
        issues.append("unapproved_pod_added")
    for uid, row in now.items():
        if row["restarts"] != 0 or row.get("terminating"):
            issues.append("pod_restart_or_termination")
        if uid in old and (not row["ready"] or row["app"] != old[uid]["app"]):
            issues.append("baseline_pod_unavailable")
    if current.get("status") != "ok":
        issues.append("resource_sample_not_ok")
    return issues


def stats(rows):
    latencies = sorted(row["latency_seconds"] * 1000 for row in rows)
    starts = sorted(timestamp(row["started_at"]) for row in rows)
    return {
        "requests": len(rows),
        "acknowledged": sum(row["outcome"] == "acknowledged" for row in rows),
        "latency_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        if latencies
        else None,
        "max_generator_lag_ms": max(
            (row["generator_lag_seconds"] * 1000 for row in rows), default=None
        ),
        "pod_requests": dict(Counter(row.get("pod_uid", "missing") for row in rows)),
        "max_request_start_gap_ms": max(
            ((right - left) * 1000 for left, right in pairwise(starts)),
            default=None,
        ),
    }


def compare_growth(evidence):
    issues = Counter()
    samples, scale = evidence["samples"], evidence.get("scale", {})
    plan, attempts = evidence["plan"], evidence["attempts"]
    if not evidence.get("complete"):
        issues["incomplete"] += 1
    if not evidence.get("session_entry_verified"):
        issues["session_entry_unverified"] += 1
    if evidence.get("resource_error"):
        issues["resource_guard_failed"] += 1
    if len(samples) < 2:
        issues["resource_evidence_missing"] += 1
    if scale.get("success") is not True or scale.get("writes") != 1:
        issues["scale_not_confirmed_once"] += 1
    if (
        scale.get("replicas_before") != 1
        or scale.get("replicas_after") != 2
        or not scale.get("resource_version_before")
    ):
        issues["scale_precondition_evidence_missing"] += 1
    before = scale.get("before", {})
    initial_deployment = evidence.get("initial_deployment", {})
    uid = initial_deployment.get("uid")
    if not uid or scale.get("deployment_uid") != uid or before.get("uid") != uid:
        issues["scale_deployment_identity_mismatch"] += 1
    rv = scale.get("resource_version_before")
    # Kubernetes resourceVersion is opaque, not an integer to compare/order.
    if (
        not isinstance(rv, str)
        or not rv
        or len(rv) > 256
        or any(char.isspace() or ord(char) < 32 for char in rv)
        or before.get("resource_version") != rv
    ):
        issues["scale_resource_version_invalid_or_unbound"] += 1
    if any(
        row.get("replicas") != 1 or row.get("ready") != 1
        for row in (initial_deployment, before)
    ):
        issues["scale_initial_or_immediate_state_invalid"] += 1
    scale_times = {}
    for name in ("requested_at", "completed_at", "rollout_ready_observed_at"):
        try:
            scale_times[name] = timestamp(scale[name])
        except (KeyError, TypeError, ValueError, AttributeError):
            scale_times[name] = None
            issues["scale_time_evidence_invalid"] += 1
    scale_time = scale_times["requested_at"]
    try:
        traffic_start = timestamp(evidence["traffic_started_at"])
        if (
            any(value is None for value in scale_times.values())
            or not traffic_start
            <= scale_time
            <= scale_times["completed_at"]
            <= scale_times["rollout_ready_observed_at"]
        ):
            issues["scale_time_order_invalid"] += 1
    except (KeyError, TypeError, ValueError, AttributeError):
        issues["scale_time_order_invalid"] += 1
    initial = (
        {p["uid"] for p in samples[0]["pods"] if p["app"] == "chat"}
        if samples
        else set()
    )
    observed = {
        p["uid"] for sample in samples for p in sample["pods"] if p["app"] == "chat"
    }
    added = observed - initial
    if len(initial) != 1 or len(added) != 1:
        issues["unexpected_api_uid_transition"] += 1
    if samples:
        if Counter(row["app"] for row in samples[0]["pods"]) != INITIAL_APPS:
            issues["initial_fixed_topology_invalid"] += 1
        for issue in transition_issues(samples[0], samples[0], False):
            issues[issue] += 1
        for sample in samples:
            if len({row["uid"] for row in sample["pods"]}) != len(sample["pods"]):
                issues["duplicate_pod_identity_evidence"] += 1
        for sample in samples[1:]:
            for issue in transition_issues(
                samples[0],
                sample,
                bool(
                    scale_time is not None
                    and timestamp(sample["observed_at"]) >= scale_time
                ),
            ):
                issues[issue] += 1
        final_ready = {
            p["uid"] for p in samples[-1]["pods"] if p["app"] == "chat" and p["ready"]
        }
        if len(final_ready) != 2 or final_ready != observed:
            issues["final_two_ready_apis_missing"] += 1
    ready_dates = [
        p["ready_since"]
        for sample in samples
        for p in sample["pods"]
        if p["uid"] in added and p["ready"] and p.get("ready_since")
    ]
    ready_time = min(map(timestamp, ready_dates)) if ready_dates else None
    planned = {row["client_message_id"]: row for row in plan}
    if len(planned) != len(plan) or len(attempts) != len(plan):
        issues["plan_attempt_count_mismatch"] += 1
    ack, seqs, seen_intents, handled = {}, set(), set(), set()
    phase_rows = {"before": [], "during": [], "after": []}
    for row in attempts:
        key = row["client_message_id"]
        if key in seen_intents:
            issues["duplicate_attempt"] += 1
        seen_intents.add(key)
        instant = timestamp(row["started_at"])
        phase = (
            "before"
            if scale_time is None or instant < scale_time
            else "during"
            if ready_time is None or instant < ready_time
            else "after"
        )
        phase_rows[phase].append(row)
        if row["outcome"] != "acknowledged":
            issues["request_" + row["outcome"]] += 1
            continue
        message = row["message"]
        intent = planned.get(key)
        if intent is None or any(
            message[name] != intent[name]
            for name in (
                "client_message_id",
                "conversation_id",
                "sender_id",
                "text_sha256",
            )
        ):
            issues["ack_intent_mismatch"] += 1
        if message["seq"] in seqs or message["message_id"] in ack:
            issues["duplicate_message_or_seq"] += 1
        seqs.add(message["seq"])
        ack[message["message_id"]] = message
        uid = row.get("pod_uid")
        if uid not in observed:
            issues["unverified_handling_pod_uid"] += 1
        handled.add(uid)
        if uid in added and (
            scale_time is None
            or ready_time is None
            or instant < scale_time
            or timestamp(row["completed_at"]) < ready_time
        ):
            issues["new_pod_handled_before_scale_or_ready"] += 1
    if not added <= handled or not initial <= handled:
        issues["both_api_pods_must_handle_requests"] += 1
    for phase, rows in phase_rows.items():
        if not rows:
            issues["missing_phase_" + phase] += 1
    peers = evidence["peers"]
    if len(peers) != 2 or len({p["id"] for p in peers}) != 2:
        issues["two_distinct_ws_peers_required"] += 1
    if Counter(peer.get("target") for peer in peers) != {
        "gateway_a": 1,
        "gateway_b": 1,
    }:
        issues["both_gateway_targets_required"] += 1
    for peer in peers:
        seen = set()
        for row in peer["received"]:
            expected = ack.get(row["message_id"])
            if expected is None:
                if row["client_message_id"] in planned:
                    issues["unmatched_ws_message"] += 1
                continue
            if any(row[name] != expected[name] for name in FIELDS):
                issues["ws_payload_mismatch"] += 1
            if row["message_id"] in seen:
                issues["duplicate_ws_receipt"] += 1
            seen.add(row["message_id"])
        issues["missing_ws_receipt"] += len(set(ack) - seen)
        if peer.get("error") or not peer.get("alive_after_traffic"):
            issues["ws_failed_during_growth"] += 1
    starts = [timestamp(row["started_at"]) for row in attempts]
    gaps = [right - left for left, right in pairwise(starts)]
    max_gap = max(gaps, default=None)
    lags = [row["generator_lag_seconds"] for row in attempts]
    if any(gap < 0 for gap in gaps):
        issues["request_start_order_invalid"] += 1
    if max_gap is None or max_gap > MAX_START_GAP_SECONDS:
        issues["global_start_gap_budget_exceeded"] += 1
    if not lags or any(
        not isinstance(lag, (int, float))
        or not isfinite(lag)
        or not 0 <= lag <= MAX_GENERATOR_LAG_SECONDS
        for lag in lags
    ):
        issues["generator_lag_budget_exceeded"] += 1
    return {
        "status": "pass" if not any(issues.values()) else "fail",
        "issues": {k: v for k, v in issues.items() if v},
        "phases": {name: stats(rows) for name, rows in phase_rows.items()},
        "continuity": {
            "max_start_gap_ms": max_gap * 1000 if max_gap is not None else None,
            "max_generator_lag_ms": max(lags) * 1000 if lags else None,
            "start_gap_budget_ms": 1000,
            "generator_lag_budget_ms": 1000,
            "meaning": "Experiment traffic continuity budget including phase boundaries; not a latency SLO.",
        },
        "counts": {
            "planned": len(plan),
            "acknowledged": len(ack),
            "ws_receipts": sum(len(p["received"]) for p in peers),
        },
        "initial_api_uids": sorted(initial),
        "new_api_uids": sorted(added),
        "scope": "E1 API 1→2 transition only; one synthetic actor/room, two WS peers. Not many-room or capacity proof.",
    }
