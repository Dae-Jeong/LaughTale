from copy import deepcopy

import pytest
from e2e_probe import ACTOR, assess


def fixture():
    expected = {
        str(i): {
            "room": "room",
            "cid": str(i),
            "seq": i,
            "hash": "hash",
            "started_monotonic": 1,
        }
        for i in (1, 2)
    }
    rows = [
        {"mid": mid, **row, "sender": ACTOR, "received_monotonic": 1.1}
        for mid, row in expected.items()
    ]
    return expected, [{"room": "room", "received": rows}]


def test_valid_receipts():
    expected, peers = fixture()
    result = assess(expected, peers)
    assert not result["failures"]
    assert result["receipts"] == 2
    assert result["ws_p99_seconds"] == pytest.approx(0.1)


@pytest.mark.parametrize(
    "case",
    ["missing", "duplicate", "room", "body", "order", "sender", "unknown", "error"],
)
def test_negative_controls(case):
    expected, peers = fixture()
    rows = peers[0]["received"]
    if case == "missing":
        rows.pop()
    elif case == "duplicate":
        rows.append(deepcopy(rows[-1]))
    elif case == "room":
        rows[0]["room"] = "other-room"
    elif case == "body":
        rows[0]["hash"] = "wrong"
    elif case == "order":
        rows.reverse()
    elif case == "sender":
        rows[0]["sender"] = "wrong"
    elif case == "unknown":
        rows[0]["mid"] = "unexpected"
    else:
        peers[0]["error"] = "closed"
    assert assess(expected, peers)["failures"]
