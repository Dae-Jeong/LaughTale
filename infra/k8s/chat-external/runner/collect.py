"""완료된 전용 Job의 JSON 증거만 수집합니다. cluster를 변경하지 않습니다."""

import argparse
import json
from pathlib import Path
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--job", default="g4-traffic")
    args = parser.parse_args()
    target = Path(args.output)
    if target.exists():
        raise SystemExit("ARTIFACT_EXISTS: choose a new output path")
    completed = subprocess.run([
        "kubectl", "--context", "k3d-laughtale-local", "-n", "laughtale-chat-external",
        "logs", f"job/{args.job}", "--timestamps=false",
    ], check=True, capture_output=True, text=True, timeout=15)
    data = json.loads(completed.stdout)
    if not isinstance(data, dict) or "summary" not in data or "records" not in data:
        raise SystemExit("INVALID_RUNNER_EVIDENCE")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
    print(json.dumps(data["summary"]))


if __name__ == "__main__":
    main()
