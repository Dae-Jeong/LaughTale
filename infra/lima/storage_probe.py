"""Bounded host-volume write/fsync/reopen check; not a power-loss test."""

import hashlib
import os
import tempfile
import time
from pathlib import Path

from capacity import EXTERNAL, EXTERNAL_ID, IMAGE_ID, MOUNT, check_volume, info


def main():
    check_volume(info(EXTERNAL), EXTERNAL_ID, EXTERNAL)
    check_volume(info(MOUNT), IMAGE_ID, MOUNT)
    block = os.urandom(1024 * 1024)
    expected = hashlib.sha256()
    started = time.monotonic()
    # Only this invocation's temporary file is removed on exit.
    with tempfile.TemporaryDirectory(prefix="storage-probe-", dir=MOUNT) as directory:
        path = Path(directory) / "probe.bin"
        with path.open("xb") as stream:
            for _ in range(64):
                stream.write(block)
                expected.update(block)
            stream.flush()
            os.fsync(stream.fileno())
        actual = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                actual.update(chunk)
        if actual.digest() != expected.digest():
            raise RuntimeError("Storage hash mismatch")
    print(
        f"PASS: 64 MiB write/fsync/reopen SHA256 {actual.hexdigest()}; "
        f"{time.monotonic() - started:.3f}s (includes cache; not disk throughput)"
    )


if __name__ == "__main__":
    main()
