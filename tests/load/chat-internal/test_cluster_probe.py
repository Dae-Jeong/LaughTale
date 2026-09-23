from copy import deepcopy

import pytest
from cluster_driver import subscription_ready
from cluster_probe import ACTOR, HASH, metric_deltas, reconcile, restoration_ok


def fixture():
    ids = [str(i) for i in range(1500)]
    rooms = ["room" + str(i) for i in range(10)]
    rows = [
        {
            "cid": cid,
            "mid": cid,
            "room": rooms[i % 10],
            "seq": i // 10 + 1,
            "sender": ACTOR,
            "hash": HASH,
            "status": "ack",
            "seconds": 0.1,
            "started_monotonic": 1,
            "pod_uid": "pod",
        }
        for i, cid in enumerate(ids)
    ]
    peers = [
        {
            "room": room,
            "received": [
                {**r, "received_monotonic": 1.2} for r in rows if r["room"] == room
            ],
        }
        for room in rooms
        for _ in range(2)
    ]
    kafka = [{**r, "partition": 0, "observed_monotonic": 1.15} for r in rows]
    db = [{**r, "outbox_seconds": 0.05} for r in rows]
    return (
        {"ids": ids, "rooms": rooms},
        {"requests": rows, "peers": peers, "kafka": kafka},
        db,
        deepcopy(db),
    )


def test_valid():
    assert reconcile(*fixture())["pass"]


@pytest.mark.parametrize(
    "case",
    [
        "kafka_missing",
        "kafka_duplicate",
        "ws_duplicate",
        "db_hash",
        "ack_room",
        "unpublished",
    ],
)
def test_negative(case):
    config, raw, primary, replica = fixture()
    if case == "kafka_missing":
        raw["kafka"].pop()
    elif case == "kafka_duplicate":
        raw["kafka"].append(raw["kafka"][0])
    elif case == "ws_duplicate":
        raw["peers"][0]["received"].append(raw["peers"][0]["received"][0])
    elif case == "db_hash":
        replica[0]["hash"] = "wrong"
    elif case == "ack_room":
        raw["requests"][0]["room"] = "wrong"
    else:
        primary[0]["outbox_seconds"] = None
    assert not reconcile(config, raw, primary, replica)["correct"]


def test_metrics_use_delta_and_reject_reset():
    a = {"pod": "db_test_sum 10\ndb_test_count 10\n"}
    b = {"pod": "db_test_sum 12\ndb_test_count 14\n"}
    assert metric_deltas(a, b)["pod"]["db_test{}"] == {"count": 4, "mean_seconds": 0.5}
    with pytest.raises(ValueError, match="metrics_reset"):
        metric_deltas(b, a)
    with pytest.raises(ValueError, match="metrics_missing"):
        metric_deltas(a, {})


def test_new_series_is_not_assumed_zero():
    a = {"pod": "db_test_sum 10\ndb_test_count 10\n"}
    b = {"pod": "db_test_sum 12\ndb_test_count 14\nhttp_new_sum 5\nhttp_new_count 20\n"}
    result = metric_deltas(a, b)["pod"]
    assert result["unavailable_series"] == ["http_new{}"]
    assert "http_new{}" not in result


def test_subscription_wire_sequence():
    frame = {"type": "subscribed", "conversation_id": "room", "head_seq": "0"}
    assert subscription_ready(frame, "room")
    assert not subscription_ready(frame, "other")
    assert not subscription_ready({**frame, "head_seq": "1"}, "room")


def test_restoration_requires_cleanup_and_no_pending():
    state = {
        "relay_restored": True,
        "fanout_unchanged": True,
        "cleanup_absent": True,
        "pending": 0,
        "guard": {},
    }
    assert restoration_ok(state)
    assert not restoration_ok({**state, "pending": 1})
    assert not restoration_ok({**state, "cleanup_absent": False})
    assert not restoration_ok({**state, "error_type": "TimeoutError"})
