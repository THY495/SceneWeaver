"""Assemble a new standalone game folder; never overwrite an existing release."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from uuid import UUID

from game_manifest import load_release, validate_manifest


def main():
    parser = argparse.ArgumentParser()
    for name in ("desktop", "backend", "postgres", "package", "output"):
        parser.add_argument(name, type=Path)
    parser.add_argument(
        "--game-id", required=True, type=UUID, help="Stable project UUID, retained across builds"
    )
    args = parser.parse_args()
    # Reject overlapping trees before copytree can recursively copy its own output.
    output = args.output.resolve()
    for root in (args.desktop, args.backend, args.postgres):
        source = root.resolve(strict=True)
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError("Output and input directories must not overlap")
    if args.output.exists():
        raise ValueError("Output must be a fresh directory")
    for root, entry in (
        (args.desktop, "Game.exe"),
        (args.backend, "GameBackend.exe"),
        (args.postgres, "bin/postgres.exe"),
    ):
        if not (root / entry).is_file():
            raise FileNotFoundError(root / entry)
    with args.package.open("rb") as source:
        raw = source.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("Story package exceeds 32 MB")
    value = json.loads(raw)
    title = value["recipe"]["title"]
    manifest = {
        "format": "ai-novel-game.v1",
        "id": str(args.game_id),
        "title": title,
        "package_sha256": hashlib.sha256(raw).hexdigest(),
    }
    validate_manifest(manifest)
    shutil.copytree(args.desktop, args.output)
    shutil.copytree(args.backend, args.output / "GameBackend")
    shutil.copytree(args.postgres, args.output / "pgsql")
    (args.output / "story.json").write_bytes(raw)
    (args.output / "game.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    load_release(args.output)
    (args.output / "README.txt").write_text(
        f"{title}\n\n双击 Game.exe 开始。请保留完整游戏文件夹。\n"
        "开始游戏前需要配置自己的 LLM API Key；语音另需 TTS 配置。\n"
        "存档与加密凭据保存在本机用户目录，移动游戏文件夹不会删除进度。\n"
        "需要 Windows WebView2 Runtime 和 .NET。\n",
        encoding="utf-8",
    )
    print(json.dumps({"game_id": str(args.game_id), "output": str(args.output)}))


if __name__ == "__main__":
    main()
