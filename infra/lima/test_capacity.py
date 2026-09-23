import unittest
from pathlib import Path

from capacity import check_volume


class VolumeGuardTests(unittest.TestCase):
    def test_expected_mount(self):
        check_volume(
            {
                "Mounted": True,
                "VolumeUUID": "expected",
                "MountPoint": "/Volumes/LaughtaleLab",
            },
            "expected",
            Path("/Volumes/LaughtaleLab"),
        )

    def test_wrong_disk_is_rejected(self):
        with self.assertRaises(RuntimeError):
            check_volume(
                {"Mounted": True, "VolumeUUID": "other"},
                "expected",
                Path("/Volumes/LaughtaleLab"),
            )

    def test_unmounted_is_rejected(self):
        with self.assertRaises(RuntimeError):
            check_volume(
                {"Mounted": False, "VolumeUUID": "expected"},
                "expected",
                Path("/Volumes/LaughtaleLab"),
            )

    def test_internal_fallback_is_rejected(self):
        with self.assertRaises(RuntimeError):
            check_volume(
                {"Mounted": True, "VolumeUUID": "expected", "MountPoint": "/"},
                "expected",
                Path("/Volumes/LaughtaleLab"),
            )


if __name__ == "__main__":
    unittest.main()
