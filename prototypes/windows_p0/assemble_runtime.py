"""Assemble a fresh Windows game runtime from frozen components and PostgreSQL."""

import argparse
import shutil
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path)
    parser.add_argument("postgres", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    sources = (args.dist / "Game", args.dist / "GameBackend", args.postgres)
    if output.exists():
        raise ValueError("Use a fresh game runtime output directory")
    for source in sources:
        root = source.resolve(strict=True)
        if output.is_relative_to(root) or root.is_relative_to(output):
            raise ValueError("Output must not overlap input directories")
    for entry in (
        args.dist / "Game/Game.exe",
        args.dist / "GameBackend/GameBackend.exe",
        args.postgres / "bin/postgres.exe",
    ):
        if not entry.is_file():
            raise FileNotFoundError(entry)
    staging = output.with_name(output.name + ".building-" + uuid4().hex[:8])
    staging.mkdir()
    for name, source in zip(("Game", "GameBackend", "pgsql"), sources, strict=True):
        shutil.copytree(source, staging / name)
    staging.rename(output)
    print(output)


if __name__ == "__main__":
    main()
