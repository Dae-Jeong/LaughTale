import hashlib

import pytest
from relay_compare import HEAD_QUERY, decode, fixture


@pytest.mark.parametrize(
    "layout,next_id", [("one", None), ("ten", 1002), ("skew", 1010)]
)
def test_fixture_candidate_oracle(layout, next_id):
    assert fixture(layout, "ready")[1] == hashlib.md5(b"1001").hexdigest()
    assert fixture(layout, "leased_all")[1] is None
    assert fixture(layout, "retry_first")[1] == (
        hashlib.md5(str(next_id).encode()).hexdigest() if next_id else None
    )


def test_heads_are_computed_before_eligibility_filters():
    head_definition = HEAD_QUERY.split("SELECT o.*")[0]
    assert "published_at IS NULL" in head_definition
    assert "lease_until" not in head_definition
    assert "next_attempt_at" not in head_definition


def test_decode_preserves_plan_and_actual_selection():
    assert decode('setup_complete\n[{"Plan": {}}]\n[]\n') == [[{"Plan": {}}], []]
