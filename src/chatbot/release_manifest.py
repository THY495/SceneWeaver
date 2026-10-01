"""Small, strict release identity understood before the backend starts."""

import hashlib
import json
from pathlib import Path
from uuid import UUID


def validate_manifest(data):
    if not isinstance(data, dict) or set(data) != {"format", "id", "title", "package_sha256"}:
        raise ValueError("游戏清单格式无效。")
    if (
        data["format"] != "ai-novel-game.v1"
        or not isinstance(data["id"], str)
        or str(UUID(data["id"])) != data["id"]
    ):
        raise ValueError("游戏清单版本或标识无效。")
    title = data["title"]
    if not isinstance(title, str) or not title.strip() or len(title) > 160 or any(ord(c) < 32 for c in title):
        raise ValueError("游戏名称无效。")
    digest = data["package_sha256"]
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("作品校验码无效。")


def load_release(directory: Path):
    path = directory / "game.json"
    if not path.exists():
        return None
    with path.open("rb") as source:
        raw = source.read(8193)
    if len(raw) > 8192:
        raise ValueError("游戏清单过大。")
    data = json.loads(raw)
    validate_manifest(data)
    package = (directory / "story.json").resolve(strict=True)
    if not package.is_relative_to(directory.resolve()):
        raise ValueError("作品文件不能指向发行目录外。")
    with package.open("rb") as source:
        raw = source.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != data["package_sha256"]:
        raise ValueError("作品文件损坏或与清单不一致。")
    from .video_sidecars import checked_files

    videos = directory / "videos"
    if not videos.resolve().is_relative_to(directory.resolve()):
        raise ValueError("视频目录不能指向发行目录外。")
    checked_files(json.loads(raw).get("recipe", {}), videos)
    return {**data, "package": package}
