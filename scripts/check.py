"""Run local checks in order; live model evaluations are deliberately excluded."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    # Windows console encodings must not hide the original check failure.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("all", "static", "frontend", "backend"), default="all")
    args = parser.parse_args()
    folder = (
        ROOT / ".runtime" / "checks" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6])
    )
    folder.mkdir(parents=True)
    # Several project/asset tests create deep trees. Keep the pytest root short
    # enough for ordinary Windows installations without long-path support.
    pytest_temp = Path(tempfile.gettempdir()) / "sceneweaver-tests" / uuid4().hex[:6]
    pytest_temp.parent.mkdir(parents=True, exist_ok=True)
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    checks = [
        ("static", "ruff", ROOT, [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts/check.py"]),
        ("frontend", "format", ROOT / "apps/web", [npm, "run", "format:check"]),
        (
            "frontend",
            "build-studio",
            ROOT / "apps/web",
            [npm, "run", "build", "--", "--outDir", str(folder / "studio-web")],
        ),
        (
            "frontend",
            "build-game",
            ROOT / "apps/web",
            [npm, "run", "build:game", "--", "--outDir", str(folder / "game-web")],
        ),
        ("frontend", "audio", ROOT / "apps/web", [npm, "test"]),
        (
            "backend",
            "pytest",
            ROOT,
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--tb=short",
                "-m",
                "not live",
                "--basetemp=" + str(pytest_temp),
                "-p",
                "no:cacheprovider",
            ],
        ),
    ]
    results = []
    for stage, name, cwd, command in checks:
        if args.stage not in ("all", stage):
            continue
        if command[0] is None:
            parser.error("Node/npm is required for frontend checks")
        log = folder / (name + ".log")
        print(f"Running {name}; log: {log}", flush=True)
        with log.open("w", encoding="utf-8") as output:
            result = subprocess.run(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT)
        results.append({"check": name, "exit_code": result.returncode, "log": str(log.relative_to(ROOT))})
        (folder / "result.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        if result.returncode:
            print(log.read_text("utf-8", errors="replace")[-8000:])
            return result.returncode
        print(f"Passed {name}", flush=True)
    print(f"All selected checks passed. Results: {folder / 'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
