import asyncio
import copy

import pytest
import repeat_probe as probe
from repeat_probe import classify


@pytest.mark.parametrize(
    ("proof", "expected"),
    [
        (None, "incomplete_evidence"),
        ({"correct": True, "pass": True}, "incomplete_path"),
        ({"forward_alive": False, "correct": True, "pass": True}, "incomplete_path"),
        ({"forward_alive": True, "correct": False}, "integrity_or_arrival_failure"),
        ({"forward_alive": True, "correct": True, "pass": False}, "latency_failure"),
        ({"forward_alive": True, "correct": True, "pass": True}, "pass"),
    ],
)
def test_classification(proof, expected):
    assert classify(proof) == expected


def test_measurement_failure_stops_repeats_and_restores_cpu(monkeypatch, tmp_path):
    original = {"resources": {"limits": {"cpu": "200m"}}, "replicas": 1}
    state = copy.deepcopy(original)
    changes, calls = [], []
    (tmp_path / ".artifacts/chat-lock").mkdir(parents=True)
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "pods", dict)
    monkeypatch.setattr(probe, "guard", lambda _: {})
    monkeypatch.setattr(probe, "sql", lambda _: 0)
    monkeypatch.setattr(
        probe,
        "deployment",
        lambda component="relay": copy.deepcopy(
            state if component == "relay" else original
        ),
    )

    def set_cpu(value):
        changes.append(value)
        state["resources"]["limits"]["cpu"] = value

    async def fail(*args, **kwargs):
        calls.append(kwargs)
        raise ValueError("synthetic")

    monkeypatch.setattr(probe, "set_cpu", set_cpu)
    monkeypatch.setattr(probe, "e2e", fail)
    assert asyncio.run(probe.main()) is False
    assert changes == ["400m", "200m"]
    assert calls == [{"http_keepalive": True, "http_keepalive_expiry": 1}]
    assert state == original
