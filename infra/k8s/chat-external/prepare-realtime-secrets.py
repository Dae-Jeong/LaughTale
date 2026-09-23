"""Generate exclusive local credentials; never print their values."""

import os
import secrets
from pathlib import Path


def main() -> None:
    target = Path(__file__).resolve().parents[3] / ".artifacts/chat-distributed/secrets"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = (target / "redis.env", target / "realtime.env")
    if any(path.exists() for path in files):
        raise SystemExit("Existing credentials preserved; no rotation performed.")
    password = secrets.token_urlsafe(48)
    token = secrets.token_urlsafe(48)
    contents = (
        f"REDIS_PASSWORD={password}\n",
        f"REDIS_URL=redis://default:{password}@redis:6379/0\nGATEWAY_DELIVERY_TOKEN={token}\n",
    )
    for path, content in zip(files, contents, strict=True):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
    print(
        "Created two private credential files under .artifacts/chat-distributed/secrets."
    )


if __name__ == "__main__":
    main()
