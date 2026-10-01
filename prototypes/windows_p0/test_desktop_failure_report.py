import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verify_desktop import main


class DesktopFailureTest(unittest.TestCase):
    def test_launch_failure_replaces_old_passing_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            report.write_text('{"status":"passed_on_developer_host"}')
            argv = ["verify_desktop", str(root / "desktop.exe"), str(root / "runs"), str(report)]
            error = OSError("Application control blocked launch")
            error.winerror = 4551
            with patch("sys.argv", argv), patch("verify_desktop.subprocess.run", side_effect=error):
                with self.assertRaises(OSError):
                    main()
            value = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(value["status"], "failed")
            self.assertEqual(value["winerror"], 4551)
            self.assertFalse(value["window_verified"])


if __name__ == "__main__":
    unittest.main()
