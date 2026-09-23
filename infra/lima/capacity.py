"""Operate only the external capacity VM after both mounted volume IDs match."""

import argparse
import os
import plistlib
import subprocess
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXTERNAL = Path("/Volumes/외장하드2")
MOUNT = Path("/Volumes/LaughtaleLab")
EXTERNAL_ID = "82F9BF80-16CE-361E-8737-97F967810A9B"
IMAGE_ID = "42866728-DFAB-4FE4-A3DB-993A506E5F66"
INSTANCE = "laughtale-capacity"


def info(path):
    result = subprocess.run(
        ["diskutil", "info", "-plist", str(path)],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Volume is not mounted: {path}")
    return plistlib.loads(result.stdout)


def check_volume(value, uuid, mount):
    if not value.get("MountPoint") or value.get("VolumeUUID") != uuid:
        raise RuntimeError("Expected mounted volume UUID does not match")
    actual = unicodedata.normalize("NFC", str(Path(value["MountPoint"]).resolve()))
    expected = unicodedata.normalize("NFC", str(mount.resolve()))
    if actual != expected:
        raise RuntimeError("Unexpected mount point; refusing fallback to internal disk")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["check", "create", "start", "status", "stop"]
    )
    args = parser.parse_args()
    try:
        check_volume(info(EXTERNAL), EXTERNAL_ID, EXTERNAL)
        image = info(MOUNT)
        check_volume(image, IMAGE_ID, MOUNT)
        if image.get("FilesystemType") != "apfs":
            raise RuntimeError("APFS image required")
        print("External and APFS volume identities verified.", flush=True)
        if args.action == "check":
            return 0
        env = dict(os.environ, LIMA_HOME=str(MOUNT / "lima"))
        if args.action == "create":
            argv = [
                "limactl",
                "create",
                "--tty=false",
                "--name=" + INSTANCE,
                "--mount-none",
                str(ROOT / "infra/lima/capacity.yaml"),
            ]
        elif args.action == "start":
            argv = ["limactl", "start", "--tty=false", "--timeout=10m", INSTANCE]
        elif args.action == "status":
            argv = ["limactl", "list", INSTANCE]
        else:
            argv = ["limactl", "stop", INSTANCE]
        return subprocess.run(argv, env=env, check=False).returncode
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
