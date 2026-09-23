import importlib.util
import json
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "internal_resources", Path(__file__).with_name("resources.py")
)
resources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resources)


def fake_command(*args):
    if args[0] == "memory_pressure":
        return "System-wide memory free percentage: 38%"
    if args[:2] == ("docker", "inspect"):
        return json.dumps(
            [
                {
                    "Name": name,
                    "Id": name,
                    "State": {"Running": True, "OOMKilled": False},
                    "RestartCount": 0,
                }
                for name in resources.CONTAINERS
            ]
        )
    if args[:2] == ("docker", "stats"):
        return "\n".join(
            json.dumps({"Name": name, "MemPerc": "50%", "CPUPerc": "1%"})
            for name in resources.CONTAINERS
        )
    if args[0] == "kubectl":
        return json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": str(i), "uid": str(i)},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [{"ready": True, "restartCount": 0}],
                        },
                    }
                    for i in range(3)
                ]
            }
        )
    if "psql" in args:
        return json.dumps(
            {"total": 10, "pending": 2, "published": 8, "oldest_pending_seconds": 3}
        )
    if "du" in args:
        return "1234\t/var/lib/kafka/data\n"
    raise AssertionError(args)


def test_known_resource_values(monkeypatch):
    monkeypatch.setattr(resources, "command", fake_command)
    value, identities = resources.sample()
    assert value["status"] == "ok"
    assert value["outbox"]["pending"] == 2
    assert value["host_free_percent"] == 38
    assert len(identities) == 7


def test_memory_threshold_is_stop(monkeypatch):
    def low(*args):
        return (
            "System-wide memory free percentage: 9%"
            if args[0] == "memory_pressure"
            else fake_command(*args)
        )

    monkeypatch.setattr(resources, "command", low)
    assert resources.sample()[0]["status"] == "stop"


def test_missing_sample_writes_stop_not_zero(monkeypatch, tmp_path):
    def broken():
        raise ValueError("unavailable")

    monkeypatch.setattr(resources, "sample", broken)
    monkeypatch.setattr(sys, "argv", ["resources.py", "--run-dir", str(tmp_path)])
    with pytest.raises(SystemExit):
        resources.main()
    assert (tmp_path / "STOP").exists()
    value = json.loads((tmp_path / "infra.json").read_text())
    assert value["status"] == "incomplete"
    assert "outbox" not in value
