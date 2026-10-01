"""Regression for failed decryption: never replace an existing world's key."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from managed_host import DatabaseHost


class CredentialRecoveryTest(unittest.TestCase):
    def test_failed_decryption_keeps_credentials_and_world(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            credential = directory / "database.dpapi"
            credential.write_bytes(b"existing protected credential")
            data = directory / "data"
            data.mkdir()
            marker = data / "PG_VERSION"
            marker.write_text("17", encoding="ascii")
            host = DatabaseHost(directory, directory)
            with patch("managed_host.protect", side_effect=OSError("wrong identity")):
                with self.assertRaisesRegex(RuntimeError, "Windows 用户"):
                    host.initialize()
            self.assertEqual(credential.read_bytes(), b"existing protected credential")
            self.assertEqual(marker.read_text(), "17")
            self.assertFalse((directory / "init-password.txt").exists())
            self.assertFalse(credential.with_suffix(".tmp").exists())


if __name__ == "__main__":
    unittest.main()
