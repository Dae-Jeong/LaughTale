import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from bootstrap import prepare
from oracle import EvidenceError


def read_env(path: Path) -> dict[str, str]:
    return {
        name: value[1:-1]
        for name, value in (
            line.split("=", 1) for line in path.read_text().splitlines()
        )
    }


class BootstrapTests(unittest.TestCase):
    @patch("bootstrap.subprocess.run", return_value=subprocess.CompletedProcess([], 0))
    def test_private_configuration_and_shared_credentials(self, check_ignore):
        with tempfile.TemporaryDirectory(prefix="chat-bootstrap-") as directory:
            folder = prepare(Path(directory), "synthetic-bootstrap-1")
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            for path in folder.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            chat, mock, harness = (
                read_env(folder / name)
                for name in ("chat.env", "mock.env", "harness.env")
            )
            self.assertEqual(chat["MOCK_API_TOKEN"], mock["MOCK_SERVICE_TOKEN"])
            self.assertEqual(
                chat["EXTERNAL_CONTROL_TOKEN"], harness["CHAT_CONTROL_TOKEN"]
            )
            self.assertEqual(mock["MOCK_CONTROL_TOKEN"], harness["MOCK_CONTROL_TOKEN"])
            self.assertEqual(
                len(
                    {
                        chat["MOCK_API_TOKEN"],
                        chat["EXTERNAL_CONTROL_TOKEN"],
                        mock["MOCK_CONTROL_TOKEN"],
                    }
                ),
                3,
            )
            ids = json.loads(harness["CHAT_TEST_CONNECTION_IDS"])
            self.assertEqual(len(ids), 7)
            self.assertEqual(
                ids["telegram"],
                str(
                    uuid5(
                        NAMESPACE_URL,
                        "laughtale:external-smoke:synthetic-bootstrap-1:telegram",
                    )
                ),
            )
            chat_credentials = json.loads(chat["EXTERNAL_CONNECTION_CREDENTIALS"])
            mock_credentials = json.loads(mock["MOCK_CONNECTION_TOKENS"])
            for profile, connection in ids.items():
                self.assertEqual(chat_credentials[connection]["profile"], profile)
                self.assertEqual(
                    chat_credentials[connection]["token"], mock_credentials[connection]
                )
            before = (folder / "chat.env").read_bytes()
            with self.assertRaises(FileExistsError):
                prepare(Path(directory), "synthetic-bootstrap-1")
            self.assertEqual((folder / "chat.env").read_bytes(), before)

    @patch("bootstrap.subprocess.run", return_value=subprocess.CompletedProcess([], 1))
    def test_unignored_path_is_refused(self, check_ignore):
        with tempfile.TemporaryDirectory(prefix="chat-bootstrap-") as directory:
            with self.assertRaisesRegex(EvidenceError, "artifact_path_must_be_ignored"):
                prepare(Path(directory), "synthetic-bootstrap-1")
            self.assertFalse((Path(directory) / ".artifacts").exists())

    def test_traversal_run_id_is_refused(self):
        with self.assertRaises(EvidenceError):
            prepare(Path("/unused"), "../elsewhere")


if __name__ == "__main__":
    unittest.main()
