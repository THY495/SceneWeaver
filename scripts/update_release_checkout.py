"""Copy a verified alpha.2 source snapshot onto a clean GitHub checkout branch."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTES = {
    "https://github.com/thy495/sceneweaver",
    "git@github.com:thy495/sceneweaver",
    "ssh://git@github.com/thy495/sceneweaver",
}


def git(target: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(target), *args], capture_output=True, check=True
    )
    return result.stdout.decode("utf-8", errors="replace").strip()


def safe_file(target: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not relative or path.parts[0] == ".git":
        raise ValueError(f"Invalid release path: {relative}")
    destination = target / path
    if not destination.resolve().is_relative_to(target) or destination.is_symlink():
        raise ValueError(f"Linked or escaping target path: {relative}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--branch", choices=("main", "codex/alpha2-candidate"), default="codex/alpha2-candidate")
    args = parser.parse_args()
    target = args.target.resolve(strict=True)
    if target == ROOT or target.is_relative_to(ROOT) or ROOT.is_relative_to(target):
        parser.error("The source snapshot and Git checkout must be separate directories")
    if Path(git(target, "rev-parse", "--show-toplevel")).resolve() != target:
        parser.error("Target must be the root of its Git checkout")
    remote = git(target, "remote", "get-url", "origin").lower().removesuffix(".git")
    if remote not in REMOTES or git(target, "branch", "--show-current") != args.branch:
        parser.error(f"Target must be the clean {args.branch} branch of THY495/SceneWeaver")
    if git(target, "status", "--porcelain"):
        parser.error("Target checkout has changes; use a fresh clean clone")
    subprocess.run([sys.executable, str(ROOT / "scripts/verify_release.py")], check=True)
    manifest = json.loads((ROOT / "RELEASE-MANIFEST.json").read_text("utf-8"))
    desired = set(manifest["files"]) | {"RELEASE-MANIFEST.json"}
    tracked = set(git(target, "ls-files").splitlines())
    removed = 0
    for relative in sorted(tracked - desired):
        path = safe_file(target, relative)
        if path.exists():
            if not path.is_file():
                raise ValueError(f"Tracked path is not a regular file: {relative}")
            path.unlink()
            removed += 1
    for relative in sorted(desired):
        source = ROOT / relative
        destination = safe_file(target, relative)
        data = source.read_bytes()
        if relative in manifest["files"] and hashlib.sha256(data).hexdigest() != manifest["files"][relative]:
            raise ValueError(f"Snapshot changed: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    print(json.dumps({"copied": len(desired), "removed_old_tracked_files": removed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
