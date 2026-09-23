"""Offline, stdlib-only reconciliation of synthetic external-chat evidence."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID

PROFILES = {
    "telegram",
    "line",
    "instagram",
    "facebook",
    "whatsapp",
    "wechat",
    "kakao-bizgo",
}
ROUTE = ("profile", "connection_id", "external_conversation_id")
INBOUND_KEY = (*ROUTE, "external_message_id")
INBOUND = {*INBOUND_KEY, "external_sender_id", "text_sha256"}
OUTBOUND = {*ROUTE, "outbound_operation_id", "text_sha256"}
ENVELOPE = {"schema_version", "run_id", "synthetic", "complete"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class EvidenceError(ValueError):
    """Invalid or incomplete evidence; messages never include input values."""


def fields(value: Any, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise EvidenceError("evidence_shape")


def identifier(value: Any) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise EvidenceError("invalid_identifier")


def uuid_value(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise EvidenceError("invalid_uuid") from None


def row(value: Any, expected: set[str]) -> None:
    fields(value, expected)
    for name, item in value.items():
        if name == "profile":
            if not isinstance(item, str) or item not in PROFILES:
                raise EvidenceError("invalid_profile")
        elif name == "text_sha256":
            if not isinstance(item, str) or not DIGEST.fullmatch(item):
                raise EvidenceError("invalid_digest")
        elif name in {"connection_id", "outbound_operation_id", "message_id"}:
            uuid_value(item)
        elif name == "effect_id":
            if item is not None:
                uuid_value(item)
        elif name.startswith("external_"):
            identifier(item)


def envelope(value: Any, lists: set[str]) -> None:
    fields(value, ENVELOPE | lists)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise EvidenceError("unsupported_schema")
    identifier(value["run_id"])
    if value["synthetic"] is not True:
        raise EvidenceError("synthetic_evidence_required")
    if value["complete"] is not True:
        raise EvidenceError("snapshot_incomplete")
    if any(not isinstance(value[name], list) for name in lists):
        raise EvidenceError("evidence_list_required")


def key(value: dict[str, Any], names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value[name] for name in names)


def unique_intents(rows: list[dict[str, Any]], names: tuple[str, ...]) -> dict:
    result = {}
    for value in rows:
        identity = key(value, names)
        if identity in result and result[identity] != value:
            raise EvidenceError("conflicting_manifest_intents")
        result[identity] = value
    return result


def validate(manifest: dict, chat: dict, mock: dict) -> tuple[dict, dict]:
    envelope(manifest, {"inbound", "outbound", "attempts"})
    envelope(chat, {"inbound_messages", "outbound_jobs"})
    envelope(mock, {"effects"})
    if not (manifest["run_id"] == chat["run_id"] == mock["run_id"]):
        raise EvidenceError("run_mismatch")
    for value in manifest["inbound"]:
        row(value, INBOUND)
    for value in manifest["outbound"]:
        row(value, OUTBOUND | {"expected_state", "expected_effects"})
        state, effects = value["expected_state"], value["expected_effects"]
        if state not in ("accepted", "rejected", "unknown"):
            raise EvidenceError("invalid_expected_state")
        if type(effects) is not int or effects not in (0, 1):
            raise EvidenceError("explicit_effect_expectation_required")
        if (state == "accepted" and effects != 1) or (
            state == "rejected" and effects != 0
        ):
            raise EvidenceError("contradictory_effect_expectation")
    inbound = unique_intents(manifest["inbound"], INBOUND_KEY)
    outbound = unique_intents(manifest["outbound"], ("outbound_operation_id",))
    if not inbound and not outbound:
        raise EvidenceError("empty_manifest")

    attempts, covered = set(), {"inbound": set(), "outbound": set()}
    for attempt in manifest["attempts"]:
        fields(attempt, {"attempt_id", "direction", "intent_index"})
        identifier(attempt["attempt_id"])
        direction, index = attempt["direction"], attempt["intent_index"]
        if direction not in ("inbound", "outbound"):
            raise EvidenceError("invalid_attempt_direction")
        if type(index) is not int or not 0 <= index < len(manifest[direction]):
            raise EvidenceError("unknown_attempt_intent")
        if attempt["attempt_id"] in attempts:
            raise EvidenceError("duplicate_attempt_id")
        attempts.add(attempt["attempt_id"])
        names = INBOUND_KEY if direction == "inbound" else ("outbound_operation_id",)
        covered[direction].add(key(manifest[direction][index], names))
    if covered["inbound"] != set(inbound) or covered["outbound"] != set(outbound):
        raise EvidenceError("missing_attempt_evidence")

    for value in chat["inbound_messages"]:
        row(value, INBOUND | {"message_id", "seq"})
        seq = value["seq"]
        if not isinstance(seq, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", seq):
            raise EvidenceError("invalid_seq")
        if int(seq) > 2**63 - 1:
            raise EvidenceError("invalid_seq")
    for value in chat["outbound_jobs"]:
        row(value, OUTBOUND | {"state", "effect_id"})
        if value["state"] not in (
            "pending",
            "sending",
            "accepted",
            "rejected",
            "unknown",
        ):
            raise EvidenceError("invalid_observed_state")
    for value in mock["effects"]:
        row(value, OUTBOUND | {"effect_id"})
        if value["effect_id"] is None:
            raise EvidenceError("effect_identity_required")
    return inbound, outbound


def reconcile(manifest: dict, chat: dict, mock: dict) -> dict:
    """Compare complete run-scoped snapshots, without importing application code."""
    try:
        inbound, outbound = validate(manifest, chat, mock)
    except EvidenceError as exc:
        return {"schema_version": 1, "status": "incomplete", "issues": {str(exc): 1}}

    issues: Counter[str] = Counter()

    def compare_rows(
        expected: dict, observed: list, names: tuple, compared: set, prefix: str
    ) -> dict:
        indexed = {}
        for value in observed:
            identity = key(value, names)
            if identity in indexed:
                issues[f"{prefix}_duplicate"] += 1
            indexed[identity] = value
            wanted = expected.get(identity)
            if wanted is None:
                issues[f"{prefix}_unexpected"] += 1
            elif any(value[field] != wanted[field] for field in compared):
                issues[f"{prefix}_content_or_route_mismatch"] += 1
        issues[f"{prefix}_missing"] += len(set(expected) - set(indexed))
        return indexed

    compare_rows(inbound, chat["inbound_messages"], INBOUND_KEY, INBOUND, "inbound")
    jobs = compare_rows(
        outbound, chat["outbound_jobs"], ("outbound_operation_id",), OUTBOUND, "job"
    )
    wanted_effects = {
        identity: value
        for identity, value in outbound.items()
        if value["expected_effects"]
    }
    effects = compare_rows(
        wanted_effects, mock["effects"], ("outbound_operation_id",), OUTBOUND, "effect"
    )
    for name, values in (
        (
            "message_id_reused",
            [value["message_id"] for value in chat["inbound_messages"]],
        ),
        ("effect_id_reused", [value["effect_id"] for value in mock["effects"]]),
        (
            "room_seq_reused",
            [key(value, (*ROUTE, "seq")) for value in chat["inbound_messages"]],
        ),
    ):
        issues[name] += len(values) - len(set(values))
    for identity, wanted in outbound.items():
        job = jobs.get(identity)
        if job is None:
            continue
        if job["state"] != wanted["expected_state"]:
            issues["job_state_mismatch"] += 1
        if job["state"] == "accepted":
            effect = effects.get(identity)
            if effect is None or job["effect_id"] != effect["effect_id"]:
                issues["accepted_effect_reference_mismatch"] += 1
        elif job["effect_id"] is not None:
            issues["nonaccepted_effect_reference"] += 1
    issues = +issues
    return {
        "schema_version": 1,
        "status": "failed" if issues else "pass",
        "issues": dict(sorted(issues.items())),
        "counts": {
            "inbound_intents": len(inbound),
            "outbound_intents": len(outbound),
            "attempts": len(manifest["attempts"]),
            "stored_inbound": len(chat["inbound_messages"]),
            "outbound_jobs": len(chat["outbound_jobs"]),
            "provider_effects": len(mock["effects"]),
            "expected_unknown": sum(
                value["expected_state"] == "unknown" for value in outbound.values()
            ),
        },
    }


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise EvidenceError("duplicate_json_field")
        result[name] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "chat", "mock"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument(
        "--output", type=Path, help="Create a new sanitized report; never overwrite"
    )
    args = parser.parse_args(argv)
    try:
        evidence = [
            json.loads(
                path.read_text(encoding="utf-8"), object_pairs_hook=unique_json_object
            )
            for path in (args.manifest, args.chat, args.mock)
        ]
        result = reconcile(*evidence)
    except (OSError, UnicodeError, ValueError, RecursionError):
        result = {
            "schema_version": 1,
            "status": "incomplete",
            "issues": {"input_unreadable": 1},
        }
    report = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        try:
            with args.output.open("x", encoding="utf-8") as output:
                output.write(report)
        except OSError:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "incomplete",
                        "issues": {"output_unwritable": 1},
                    }
                )
            )
            return 2
    print(report, end="")
    return {"pass": 0, "failed": 1, "incomplete": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
