"""Pydantic 정본에서 언어 독립 JSON Schema를 생성하거나 차이를 검사합니다."""

import argparse
import json
from pathlib import Path

from platform_contracts.wire import (
    AcceptedEffect,
    ConnectionSeed,
    InboundEvent,
    OutboundCommand,
    RunConfig,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("schemas.json"))
    args = parser.parse_args()
    models = (InboundEvent, OutboundCommand, AcceptedEffect, RunConfig, ConnectionSeed)
    text = (
        json.dumps(
            {model.__name__: model.model_json_schema() for model in models},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if args.check:
        if not args.output.exists() or args.output.read_text() != text:
            raise SystemExit("Contract schema drift")
    else:
        args.output.write_text(text)


if __name__ == "__main__":
    main()
