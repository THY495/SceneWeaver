"""Verify the exact roleplay source snapshot and optional staged index."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {
    ".env",
    ".git",
    ".venv",
    ".runtime",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "data",
    "tmp",
    "t",
    "dist",
    "dist-game",
}
SENSITIVE_NAME = re.compile(r"(?:^|/)(?:[^/]*_key|openrouter)\.txt$|\.(?:pem|pfx|p12)$", re.I)
SECRET = re.compile(rb"(?<![A-Za-z0-9])(?:sk-|sk_)[A-Za-z0-9_-]{20,}|Bearer[ \t]+[A-Za-z0-9._~-]{24,}")


def included() -> set[str]:
    return {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.is_file() and not any(part in IGNORED for part in path.relative_to(ROOT).parts)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
    expected = manifest["files"]
    errors = []
    if manifest["version"] != "0.1.0-alpha.2" or manifest["scope"] != "roleplay-source-only":
        errors.append("release identity mismatch")
    actual = included() - {"RELEASE-MANIFEST.json"}
    if actual != set(expected):
        errors.append(
            f"file set changed: added={sorted(actual - set(expected))}, missing={sorted(set(expected) - actual)}"
        )
    for relative, digest in expected.items():
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            errors.append(f"missing or linked: {relative}")
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            errors.append(f"content changed: {relative}")
        if SENSITIVE_NAME.search(relative):
            errors.append(f"sensitive filename: {relative}")
        if SECRET.search(data) and relative != "tests/test_game_library.py":
            errors.append(f"secret-shaped string: {relative}")
    if "projects/office/README.md" in expected or "apps/web/src/main.tsx" in expected:
        errors.append("office product or legacy mixed frontend included")
    config = (ROOT / "src/chatbot/config.py").read_text(encoding="utf-8")
    if 'product: Literal["roleplay"] = "roleplay"' not in config:
        errors.append("backend roleplay-only setting is missing")
    index = (ROOT / "apps/web/index.html").read_text(encoding="utf-8")
    if "/src/studio.tsx" not in index:
        errors.append("Studio does not use the roleplay-only entry")
    if args.staged:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        staged = {name.decode("utf-8") for name in result.stdout.split(b"\0") if name}
        expected_staged = set(expected) | {"RELEASE-MANIFEST.json"}
        if staged != expected_staged:
            errors.append(
                f"index file set differs: added={sorted(staged - expected_staged)}, "
                f"missing={sorted(expected_staged - staged)}"
            )
        indexed_manifest = subprocess.run(
            ["git", "show", ":RELEASE-MANIFEST.json"],
            cwd=ROOT,
            capture_output=True,
        )
        if (
            indexed_manifest.returncode
            or indexed_manifest.stdout != (ROOT / "RELEASE-MANIFEST.json").read_bytes()
        ):
            errors.append("index manifest differs from working tree")
        for relative, digest in expected.items():
            if relative not in staged:
                continue
            indexed = subprocess.run(
                ["git", "show", f":{relative}"],
                cwd=ROOT,
                capture_output=True,
            )
            if indexed.returncode or hashlib.sha256(indexed.stdout).hexdigest() != digest:
                errors.append(f"index content changed: {relative}")
    print(json.dumps({"files": len(expected), "errors": errors}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
