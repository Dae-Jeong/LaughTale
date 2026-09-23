"""Independent HTTP/WS evidence comparison; no application imports."""

from collections import Counter
from hashlib import sha256
from uuid import UUID

ROOM = "00000000-0000-4000-8000-000000000010"
ACTOR = "00000000-0000-4000-8000-000000000001"
FIELDS = (
    "message_id",
    "conversation_id",
    "sender_id",
    "client_message_id",
    "seq",
    "text_sha256",
)
CHECKS = (
    "shared_session",
    "anonymous_http_denied",
    "anonymous_ws_denied",
    "relogin_replaced_cookie",
    "revoked_http_denied",
    "revoked_ws_closed",
    "revoked_ws_handshake_denied",
    "ws_live_before_relogin",
)


class EvidenceError(ValueError):
    """Static error codes only: never include response bodies or credentials."""


def normalize(message: dict) -> dict:
    result = {}
    for field in FIELDS[:4]:
        value = message.get(field)
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise EvidenceError("invalid_message_uuid")
        result[field] = value
    seq = message.get("seq")
    if (
        not isinstance(seq, str)
        or not seq.isascii()
        or not seq.isdecimal()
        or not 0 < int(seq) < 2**63
        or str(int(seq)) != seq
    ):
        raise EvidenceError("invalid_message_seq")
    text = message.get("text")
    if not isinstance(text, str):
        raise EvidenceError("invalid_message_text")
    result.update(seq=seq, text_sha256=sha256(text.encode()).hexdigest())
    return result


def compare(evidence: dict) -> dict:
    issues = Counter()
    planned = evidence["plan"]
    attempts = evidence["attempts"]
    expected = {row["client_message_id"]: row for row in planned}
    if not expected or len(expected) != len(planned):
        issues["invalid_plan"] += 1
    if len(attempts) != len(planned):
        issues["attempt_count_mismatch"] += 1
    ack = {}
    seqs = set()
    for attempt in attempts:
        key = attempt["client_message_id"]
        if key in ack:
            issues["duplicate_attempt"] += 1
        if attempt["outcome"] != "acknowledged":
            issues["request_" + attempt["outcome"]] += 1
            continue
        row = attempt["message"]
        intent = expected.get(key)
        if intent is None or any(
            row[field] != intent[field]
            for field in (
                "client_message_id",
                "conversation_id",
                "sender_id",
                "text_sha256",
            )
        ):
            issues["ack_payload_mismatch"] += 1
        if row["seq"] in seqs:
            issues["duplicate_seq"] += 1
        seqs.add(row["seq"])
        ack[key] = row
    by_id = {row["message_id"]: row for row in ack.values()}
    if len(by_id) != len(ack):
        issues["duplicate_message_id"] += 1
    counts = {
        "planned": len(planned),
        "acknowledged": len(ack),
        "ws_received": 0,
        "history_recovered": 0,
        "history_rows": 0,
        "history_overlap_ws": 0,
        "duplicate_ws": 0,
    }
    peers = evidence["peers"]
    if len(peers) != evidence["expected_peers"]:
        issues["missing_peer"] += 1
    if len({peer.get("id") for peer in peers}) != len(peers):
        issues["duplicate_peer_evidence"] += 1
    for peer in peers:
        seen = set()
        duplicates = 0
        for source, rows in (
            ("ws", peer["received"]),
            ("history", peer["history_recovered"]),
        ):
            for row in rows:
                wanted = by_id.get(row["message_id"])
                if wanted is None:
                    # Concurrent unrelated traffic is not this run's message.
                    if row["client_message_id"] in expected:
                        issues["unexpected_message_id"] += 1
                    continue
                if any(row[field] != wanted[field] for field in FIELDS):
                    issues[source + "_payload_mismatch"] += 1
                if source == "ws":
                    duplicates += row["message_id"] in seen
                    counts["ws_received"] += 1
                else:
                    counts["history_rows"] += 1
                    if row["message_id"] not in seen:
                        counts["history_recovered"] += 1
                    else:
                        counts["history_overlap_ws"] += 1
                seen.add(row["message_id"])
        counts["duplicate_ws"] += duplicates
        issues["duplicate_ws_delivery"] += duplicates
        missing = set(by_id) - seen
        issues["missing_receipt"] += len(missing)
        if peer.get("error"):
            issues["peer_error"] += 1
        offline = set(peer.get("offline_client_ids", []))
        if evidence.get("scenario") == "reconnect" and offline:
            offline_ids = {
                row["message_id"] for key, row in ack.items() if key in offline
            }
            history_ids = {row["message_id"] for row in peer["history_recovered"]}
            ws_ids = {row["message_id"] for row in peer["received"]}
            exclusive = history_ids - ws_ids
            issues["offline_batch_not_exclusively_history_recovered"] += len(
                offline_ids - exclusive
            )
        live_expected = {
            row["message_id"] for key, row in ack.items() if key not in offline
        }
        live_seen = {row["message_id"] for row in peer["received"]}
        issues["missing_live_receipt"] += len(live_expected - live_seen)
    for name in CHECKS:
        passed = evidence["checks"].get(name)
        if passed is not True:
            issues["check_" + name] += 1
    if not evidence.get("complete"):
        issues["incomplete"] += 1
    if (
        evidence.get("resource_guard_enabled")
        and evidence.get("resource_guard_passed") is not True
    ):
        issues["resource_guard_incomplete_or_stop"] += 1
    issues = {key: value for key, value in issues.items() if value}
    return {
        "status": "pass" if not issues else "fail",
        "counts": counts,
        "issues": issues,
        "scope": "Bounded synthetic HTTP/WS delivery and history recovery; not capacity or production authentication proof.",
    }
