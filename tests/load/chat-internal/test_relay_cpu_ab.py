import json

import pytest
import relay_cpu_ab as probe


def test_cpu_patch_does_not_change_memory_or_requests(monkeypatch):
    commands = []
    monkeypatch.setattr(probe, "command", lambda args: commands.append(args))
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    monkeypatch.setattr(probe, "pods", lambda: {str(i): [str(i), 0] for i in range(6)})
    probe.set_cpu("400m", "fanout")
    patch = json.loads(commands[0][-1])
    assert patch == {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": "fanout", "resources": {"limits": {"cpu": "400m"}}}
                    ]
                }
            }
        }
    }
    assert "chat-fanout" in commands[0]


def test_unstable_rollout_is_not_accepted(monkeypatch):
    monkeypatch.setattr(probe, "command", lambda _: "")
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)
    ticks = iter(range(0, 200, 10))
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(probe, "pods", dict)
    with pytest.raises(ValueError, match="rollout_not_stable"):
        probe.set_cpu("400m")
