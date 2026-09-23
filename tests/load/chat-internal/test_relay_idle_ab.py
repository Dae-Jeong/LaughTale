import pytest
from relay_idle_ab import cost_delta, profile_patch


def test_cost_windows_are_separate():
    a = {
        "uid": "one",
        "stats": {"at_monotonic": 10, "outcomes": {"idle": 20}},
        "cpu": {"usage_usec": 10000},
        "sample_monotonic": 11,
    }
    b = {
        "uid": "one",
        "stats": {"at_monotonic": 30, "outcomes": {"idle": 60}},
        "cpu": {"usage_usec": 50000},
        "sample_monotonic": 36,
    }
    result = cost_delta(a, b)
    assert result["empty_claims_per_second"] == 2
    assert result["cpu_millicores"] == 1.6
    with pytest.raises(ValueError, match="relay_changed"):
        cost_delta(a, {**b, "uid": "other"})
    with pytest.raises(ValueError, match="counter_reset"):
        cost_delta(b, a)


def test_profile_patch_preserves_other_environment():
    container = profile_patch("image", None)["spec"]["template"]["spec"]["containers"][
        0
    ]
    assert container == {
        "name": "relay",
        "image": "image",
        "env": [{"name": "LAB_RELAY_IDLE_MS", "$patch": "delete"}],
    }
