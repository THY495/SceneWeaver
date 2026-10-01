"""Write unattended launch failures without opening a PyInstaller error dialog."""

import argparse
import json
from pathlib import Path


def record_smoke_failure(arguments, error):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--smoke-report", type=Path)
    options, _ = parser.parse_known_args(arguments)
    if options.smoke_report is None:
        return False
    options.smoke_report.parent.mkdir(parents=True, exist_ok=True)
    options.smoke_report.write_text(
        json.dumps({"status": "launch_failed", "error": str(error)}, ensure_ascii=False),
        encoding="utf-8",
    )
    return True
