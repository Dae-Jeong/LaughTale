"""Generate the private, lab-only entry credential without rotating existing files."""

import os
import secrets
from pathlib import Path


def main() -> None:
    directory = Path(__file__).resolve().parents[3] / ".artifacts/chat-distributed/secrets"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / "ingress.env"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(f"LAB_INGRESS_TOKEN={secrets.token_urlsafe(48)}\n")
    print("Created private ingress credential file; value not displayed.")


if __name__ == "__main__":
    main()
