"""Independent E3 checks; generic role transition guard is also usable by E2."""

from collections import Counter
from itertools import pairwise

from growth_oracle import stats, timestamp
from oracle import FIELDS

INITIAL_APPS = {
    "chat": 2,
    "chat-gateway": 2,
    "chat-fanout": 1,
    "chat-relay": 1,
    "chat-growth-proxy": 1,
}


def role_transition(baseline, current, allowed_app, allow_growth):
    old = {row["uid"]: row for row in baseline["pods"]}
    current_rows = current["pods"]
    present = {row["uid"]: row for row in current_rows}
    errors = []
    if len(present) != len(current_rows):
        errors.append("duplicate_pod_evidence")
    if baseline["containers"] != current["containers"]:
        errors.append("container_changed")
    if not set(old) <= set(present):
        errors.append("baseline_pod_missing")
    new = set(present) - set(old)
    if len(new) > int(allow_growth) or any(
        present[uid]["app"] != allowed_app for uid in new
    ):
        errors.append("unapproved_pod_added")
    for uid, row in present.items():
        if row["restarts"] != 0 or row.get("terminating"):
            errors.append("pod_restart_or_termination")
        if uid in old and (not row["ready"] or row["app"] != old[uid]["app"]):
            errors.append("baseline_pod_unavailable")
    if current.get("status") != "ok":
        errors.append("resource_sample_not_ok")
    return errors


def stable_owners(sample):
    if sample["state"] != "Stable":
        return None
    owners = {}
    for member in sample["members"]:
        for partition in member["partitions"]:
            if partition in owners or partition not in range(4):
                raise ValueError("invalid_partition_ownership")
            owners[partition] = member["pod_uid"]
    if set(owners) != set(range(4)):
        raise ValueError("incomplete_partition_assignment")
    return owners


def compare(evidence):
    issues = Counter()
    samples, kafka, attempts, plan = (
        evidence[key] for key in ("samples", "kafka", "attempts", "plan")
    )
    scale = evidence.get("scale", {})
    if (
        not evidence.get("complete")
        or evidence.get("resource_error")
        or not evidence.get("monitoring_completed")
    ):
        issues["incomplete"] += 1
    if (
        not evidence.get("session_entry_verified")
        or not evidence.get("kafka_monitoring_completed")
        or evidence.get("kafka_error")
    ):
        issues["required_observation_incomplete"] += 1
    before = scale.get("before", {})
    initial_deployment = evidence.get("initial_deployment", {})
    if (
        scale.get("success") is not True
        or scale.get("writes") != 1
        or before.get("replicas") != 1
        or before.get("ready") != 1
    ):
        issues["scale_preconditions_or_result_invalid"] += 1
    if (
        not initial_deployment.get("uid")
        or initial_deployment.get("uid") != before.get("uid")
        or scale.get("deployment_uid") != before.get("uid")
    ):
        issues["scale_identity_mismatch"] += 1
    rv = scale.get("resource_version_before")
    if (
        not isinstance(rv, str)
        or not rv.strip()
        or rv != before.get("resource_version")
    ):
        issues["scale_resource_version_unbound"] += 1
    requested = None
    try:
        requested = timestamp(scale["requested_at"])
        if (
            not timestamp(evidence["traffic_started_at"])
            <= requested
            <= timestamp(scale["completed_at"])
            <= timestamp(scale["rollout_ready_observed_at"])
        ):
            issues["scale_time_order_invalid"] += 1
    except (KeyError, ValueError, TypeError, AttributeError):
        issues["scale_time_evidence_missing"] += 1
    initial_uids, new_uids = set(), set()
    if len(samples) < 2:
        issues["resource_evidence_missing"] += 1
    else:
        initial_uids = {
            row["uid"] for row in samples[0]["pods"] if row["app"] == "chat-fanout"
        }
        if Counter(row["app"] for row in samples[0]["pods"]) != INITIAL_APPS:
            issues["initial_topology_invalid"] += 1
        for sample in samples:
            new_uids.update(
                row["uid"]
                for row in sample["pods"]
                if row["app"] == "chat-fanout" and row["uid"] not in initial_uids
            )
            issues.update(
                role_transition(
                    samples[0],
                    sample,
                    "chat-fanout",
                    requested is not None
                    and timestamp(sample["observed_at"]) >= requested,
                )
            )
        if len(new_uids) != 1:
            issues["exactly_one_new_fanout_required"] += 1
        final_ready = {
            row["uid"]
            for row in samples[-1]["pods"]
            if row["app"] == "chat-fanout" and row["ready"]
        }
        if final_ready != initial_uids | new_uids:
            issues["final_fanout_not_ready"] += 1
    known_uids = initial_uids | new_uids
    stable = []
    for sample in kafka:
        try:
            owners = stable_owners(sample)
        except ValueError:
            issues["invalid_kafka_assignment"] += 1
            continue
        if owners is not None:
            stable.append((sample, owners))
            member_uids = [member["pod_uid"] for member in sample["members"]]
            if (
                len(member_uids) != len(set(member_uids))
                or not set(member_uids) <= known_uids
            ):
                issues["kafka_member_pod_identity_invalid"] += 1
        for offset in sample["offsets"]:
            if (
                offset["committed"] < 0
                or offset["committed"] > offset["end"]
                or offset["lag"] != offset["end"] - offset["committed"]
            ):
                issues["invalid_commit_bounds"] += 1
        if len(sample["offsets"]) != 4 or {
            row["partition"] for row in sample["offsets"]
        } != set(range(4)):
            issues["four_partition_offsets_required"] += 1
    if len(stable) < 2:
        issues["stable_assignment_evidence_missing"] += 1
    if not kafka or kafka[0]["state"] != "Stable" or kafka[-1]["state"] != "Stable":
        issues["stable_boundary_evidence_missing"] += 1
    first_two = None
    hot_partitions, new_hot_owner = [], False
    if stable:
        first, first_owners = stable[0]
        last, last_owners = stable[-1]
        if len(first["members"]) != 1 or set(first_owners.values()) != initial_uids:
            issues["initial_single_consumer_missing"] += 1
        if len(last["members"]) != 2 or set(last_owners.values()) != known_uids:
            issues["final_two_consumer_assignment_missing"] += 1
        if first_owners == last_owners:
            issues["assignment_did_not_change"] += 1
        for sample, owners in stable:
            if len(sample["members"]) == 2 and set(owners.values()) == known_uids:
                first_two = timestamp(sample["observed_at"])
                break
        if requested is not None and first_two is not None and first_two < requested:
            issues["consumer_added_before_scale"] += 1
        first_offsets = {row["partition"]: row for row in first["offsets"]}
        last_offsets = {row["partition"]: row for row in last["offsets"]}
        if set(first_offsets) != set(range(4)) or set(last_offsets) != set(range(4)):
            issues["four_partition_offsets_required"] += 1
        else:
            hot_partitions = [
                p for p in range(4) if last_offsets[p]["end"] > first_offsets[p]["end"]
            ]
            if len(hot_partitions) != 1:
                issues["single_room_partition_activity_not_isolated"] += 1
            new_hot_owner = bool(
                hot_partitions and last_owners.get(hot_partitions[0]) in new_uids
            )
            if any(row["lag"] != 0 for row in first["offsets"] + last["offsets"]):
                issues["initial_or_final_lag_not_zero"] += 1
            if sum(
                last_offsets[p]["committed"] - first_offsets[p]["committed"]
                for p in range(4)
            ) < len(attempts):
                issues["commit_progress_below_workload"] += 1
    for left, right in pairwise(kafka):
        if timestamp(right["observed_at"]) < timestamp(left["observed_at"]):
            issues["kafka_observation_time_regressed"] += 1
        old = {row["partition"]: row["committed"] for row in left["offsets"]}
        if any(
            row["committed"] < old.get(row["partition"], 0) for row in right["offsets"]
        ):
            issues["commit_regressed"] += 1
    expected = {row["client_message_id"]: row for row in plan}
    if len(expected) != 210 or len(attempts) != 210:
        issues["bounded_ramp_incomplete"] += 1
    ack, seen_intents, seqs = {}, set(), set()
    phases = {"before": [], "during": [], "after": []}
    for row in attempts:
        instant = timestamp(row["started_at"])
        phase = (
            "before"
            if requested is None or instant < requested
            else "during"
            if first_two is None or instant < first_two
            else "after"
        )
        phases[phase].append(row)
        key = row["client_message_id"]
        if key in seen_intents:
            issues["duplicate_attempt"] += 1
        seen_intents.add(key)
        if row["outcome"] != "acknowledged":
            issues["request_not_acknowledged"] += 1
            continue
        message, intent = row["message"], expected.get(key)
        if intent is None or any(
            message[field] != intent[field]
            for field in (
                "client_message_id",
                "conversation_id",
                "sender_id",
                "text_sha256",
            )
        ):
            issues["ack_payload_mismatch"] += 1
        if message["message_id"] in ack or message["seq"] in seqs:
            issues["duplicate_stored_identity"] += 1
        ack[message["message_id"]] = message
        seqs.add(message["seq"])
        if not 0 <= row["generator_lag_seconds"] <= 1:
            issues["generator_lag_exceeded"] += 1
    for left, right in pairwise(attempts):
        gap = timestamp(right["started_at"]) - timestamp(left["started_at"])
        planned_gap = right["scheduled_seconds"] - left["scheduled_seconds"]
        if gap < 0 or gap > planned_gap + 1:
            issues["planned_gap_budget_exceeded"] += 1
    if any(not rows for rows in phases.values()):
        issues["phase_evidence_missing"] += 1
    peers = evidence["peers"]
    if (
        kafka
        and attempts
        and timestamp(kafka[-1]["observed_at"])
        < max(timestamp(row["completed_at"]) for row in attempts)
    ):
        issues["final_kafka_sample_predates_workload_end"] += 1
    if len(peers) != 2 or Counter(row.get("target") for row in peers) != {
        "gateway_a": 1,
        "gateway_b": 1,
    }:
        issues["both_gateways_required"] += 1
    for peer in peers:
        seen = set()
        for row in peer["received"]:
            wanted = ack.get(row["message_id"])
            if wanted is None:
                continue
            if any(row[field] != wanted[field] for field in FIELDS):
                issues["ws_payload_mismatch"] += 1
            if row["message_id"] in seen:
                issues["duplicate_ws"] += 1
            seen.add(row["message_id"])
        issues["missing_ws"] += len(set(ack) - seen)
        if peer.get("error") or not peer.get("alive_after_traffic"):
            issues["ws_interrupted"] += 1
    return {
        "status": "pass" if not any(issues.values()) else "fail",
        "issues": {k: v for k, v in issues.items() if v},
        "counts": {
            "planned": len(plan),
            "acknowledged": len(ack),
            "ws_receipts": sum(len(p["received"]) for p in peers),
        },
        "phases": {name: stats(rows) for name, rows in phases.items()},
        "hot_partitions": hot_partitions,
        "new_consumer_owns_hot_partition_at_end": new_hot_owner,
        "kafka_summary": {
            "samples": len(kafka),
            "initial_members": kafka[0]["members"] if kafka else [],
            "final_members": kafka[-1]["members"] if kafka else [],
            "initial_offsets": kafka[0]["offsets"] if kafka else [],
            "final_offsets": kafka[-1]["offsets"] if kafka else [],
            "peak_lag_by_partition": {
                str(p): max(
                    (
                        row["lag"]
                        for sample in kafka
                        for row in sample["offsets"]
                        if row["partition"] == p
                    ),
                    default=None,
                )
                for p in range(4)
            },
        },
        "scope": "E3 consumer participation/reassignment and delivery continuity only; no hot-partition throughput or horizontal capacity claim.",
    }
