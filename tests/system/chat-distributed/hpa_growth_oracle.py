"""HPA evidence checks independent of the application and scale actuator."""

from collections import Counter
from decimal import Decimal
from math import isfinite

from oracle import FIELDS

TARGET = 60
IDLE_MAX_MILLICORES = 54
PHASES = ((1, 15), (5, 15), (10, 45))
COUNT = sum(rate * seconds for rate, seconds in PHASES)


def cpu_millicores(value):
    if value.endswith("n"):
        result = float(Decimal(value[:-1]) / 1_000_000)
    elif value.endswith("u"):
        result = float(Decimal(value[:-1]) / 1000)
    elif value.endswith("m"):
        result = float(Decimal(value[:-1]))
    else:
        result = float(Decimal(value) * 1000)
    if not isfinite(result) or result < 0:
        raise ValueError("invalid_cpu_quantity")
    return result


def idle_issues(samples):
    issues = []
    if len({row["metrics_timestamp"] for row in samples}) < 3:
        issues.append("idle_distinct_metrics_missing")
    if not samples or samples[-1]["elapsed"] - samples[0]["elapsed"] < 50:
        issues.append("idle_observation_too_short")
    if any(row["cpu_millicores"] >= IDLE_MAX_MILLICORES for row in samples):
        issues.append("idle_not_below_fixed_threshold_margin")
    if any(row["replicas"] != 1 or row["ready"] != 1 for row in samples):
        issues.append("idle_not_one_ready_api")
    if any(row.get("metrics_pod_count") != 1 for row in samples):
        issues.append("idle_metrics_not_one_api")
    if any(row.get("desired_replicas", 1) != 1 for row in samples):
        issues.append("idle_hpa_growth_invalidates_experiment")
    return issues


def compare(evidence):
    issues = Counter(idle_issues(evidence.get("idle", [])))
    if not evidence.get("complete"):
        issues["incomplete"] += 1
    if evidence.get("error"):
        issues["execution_or_resource_guard_failed"] += 1
    if evidence.get("scale_writes") != 0:
        issues["runner_scale_write_forbidden"] += 1
    plan = {row["client_message_id"]: row for row in evidence["plan"]}
    attempts = evidence["attempts"]
    if len(plan) != COUNT or len(attempts) != COUNT:
        issues["fixed_540_budget_or_attempt_count_invalid"] += 1
    if Counter(row["rate"] for row in attempts) != {1: 15, 5: 75, 10: 450}:
        issues["ramp_shape_invalid"] += 1
    ack, ids, seqs, keys = {}, set(), set(), set()
    for attempt in attempts:
        key = attempt["client_message_id"]
        if key in keys:
            issues["duplicate_post_attempt"] += 1
        keys.add(key)
        if attempt["outcome"] != "acknowledged":
            issues["non_acknowledged_post"] += 1
            continue
        message = attempt["message"]
        expected = plan.get(key, {})
        if any(
            message.get(field) != expected.get(field)
            for field in (
                "client_message_id",
                "conversation_id",
                "sender_id",
                "text_sha256",
            )
        ):
            issues["intent_mismatch"] += 1
        if message["message_id"] in ids or message["seq"] in seqs:
            issues["duplicate_message_or_seq"] += 1
        ids.add(message["message_id"])
        seqs.add(message["seq"])
        ack[message["message_id"]] = message
        if not 0 <= attempt["generator_lag_seconds"] <= 1:
            issues["generator_lag_exceeded"] += 1
    for source in [peer["received"] for peer in evidence["peers"]] + [
        evidence["history"]
    ]:
        seen = set()
        for row in source:
            if row["message_id"] not in ack or any(
                row[field] != ack[row["message_id"]][field] for field in FIELDS
            ):
                issues["receipt_or_history_mismatch"] += 1
            if row["message_id"] in seen:
                issues["duplicate_receipt_or_history"] += 1
            seen.add(row["message_id"])
        if seen != ids:
            issues["missing_receipt_or_history"] += 1
    if Counter(peer.get("target") for peer in evidence["peers"]) != {
        "gateway_a": 1,
        "gateway_b": 1,
    } or any(
        peer.get("error") or not peer.get("alive_after_traffic")
        for peer in evidence["peers"]
    ):
        issues["two_live_gateway_receivers_required"] += 1
    initial = set(evidence.get("initial_api_uids", []))
    observed = set(evidence.get("observed_api_uids", []))
    if len(initial) != 1 or not initial <= observed or len(observed) > 2:
        issues["api_identity_transition_invalid"] += 1
    handled = {
        row.get("pod_uid") for row in attempts if row["outcome"] == "acknowledged"
    }
    if not handled <= observed:
        issues["handling_uid_unobserved"] += 1
    mode = evidence["mode"]
    hpa = evidence.get("hpa", {})
    triggered = bool(hpa.get("cpu_desired_two") and hpa.get("successful_rescale"))
    if mode == "control":
        if observed != initial or evidence.get("hpa_present"):
            issues["control_scaled_or_hpa_present"] += 1
        status = "pass" if not issues else "fail"
    else:
        if not evidence.get("hpa_present") or hpa.get("target") != TARGET:
            issues["fixed_hpa_missing_or_changed"] += 1
        added = observed - initial
        if triggered and (
            len(added) != 1 or not added <= set(evidence.get("ready_api_uids", []))
        ):
            issues["desired_two_without_new_ready_api"] += 1
        if added and not triggered:
            issues["growth_without_hpa_cpu_evidence"] += 1
        if triggered and not added <= handled:
            issues["new_api_write_unproven"] += 1
        status = "fail" if issues else "pass" if triggered else "not_triggered"
    return {
        "status": status,
        "issues": dict(issues),
        "mode": mode,
        "counts": {
            "planned": COUNT,
            "attempts": len(attempts),
            "acknowledged": len(ack),
            "receipts": sum(len(p["received"]) for p in evidence["peers"]),
        },
        "hpa_triggered": triggered,
        "pod_post_counts": dict(
            Counter(row.get("pod_uid", "missing") for row in attempts)
        ),
        "auto_down_status": "not_tested",
        "scope": "CPU HPA one-room, two-WS, max-two-API experiment; no capacity or zero-loss production claim.",
    }
