"""Assemble a fresh internal P0 folder; never overwrite an existing release."""

import argparse
import json
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("desktop", type=Path)
    parser.add_argument("backend", type=Path)
    parser.add_argument("postgres", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output must be a fresh directory")
    for root, entry in (
        (args.desktop, "SceneweaverDesktopP0.exe"),
        (args.backend, "SceneweaverBackendP0.exe"),
        (args.postgres, "bin/postgres.exe"),
    ):
        if not (root / entry).is_file():
            raise FileNotFoundError(root / entry)
    shutil.copytree(args.desktop, args.output)
    shutil.copytree(args.backend, args.output / "SceneweaverBackendP0")
    shutil.copytree(args.postgres, args.output / "pgsql")
    (args.output / "README.txt").write_text(
        "Sceneweaver P0 - internal offline prototype. All generated replies are fixtures.\n"
        "Windows WebView2 Runtime and .NET are prerequisites; clean-machine compatibility is not validated.\n"
        "Keep this complete folder. Open SceneweaverDesktopP0.exe. Data defaults to LOCALAPPDATA/SceneweaverP0/desktop.\n"
        "This is the existing workbench UI, not the final game player. No paid model calls.\n"
        "Public redistribution/license review is incomplete.\n",
        encoding="utf-8",
    )
    files = [p for p in args.output.rglob("*") if p.is_file()]
    print(json.dumps({"files": len(files), "bytes": sum(p.stat().st_size for p in files)}, indent=2))


if __name__ == "__main__":
    main()
