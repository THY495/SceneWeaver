"""Bounded sidecar validation/copying shared by projects and independent games.

Only standard library imports: release validation also runs in the small host.
"""

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path


def entries(recipe):
    if not isinstance(recipe, dict):
        raise ValueError("作品视频清单无效")
    assets = recipe.get("video_assets", [])
    if not isinstance(assets, list) or len(assets) > 64:
        raise ValueError("视频清单无效")
    result = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("视频清单无效")
        key, size = asset.get("content_hash"), asset.get("bytes")
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[a-f0-9]{64}", key)
            or type(size) is not int
            or not 0 < size <= 256 * 1024 * 1024
            or key in result
        ):
            raise ValueError("视频清单无效")
        result[key] = size
    if sum(result.values()) > 2 * 1024 * 1024 * 1024:
        raise ValueError("作品视频总量超过 2 GB")
    return result


def checked_files(recipe, directory):
    directory = Path(directory)
    result = {}
    for key, size in entries(recipe).items():
        path = directory / (key + ".mp4")
        if not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
            raise ValueError("视频缺失或路径指向素材目录外")
        if path.stat().st_size != size:
            raise ValueError("视频长度与清单不一致")
        with path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != key:
                raise ValueError("视频校验失败")
        result[key] = path
    return result


def copy_videos(recipe, source, destination):
    files = checked_files(recipe, source)
    if not files:
        return
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for key, path in files.items():
        target = destination / (key + ".mp4")
        if target.exists():
            checked_files(
                {"video_assets": [{"content_hash": key, "bytes": path.stat().st_size}]}, destination
            )
            continue
        handle, name = tempfile.mkstemp(prefix="copy-", suffix=".tmp", dir=destination)
        temporary = Path(name)
        try:
            with os.fdopen(handle, "wb") as output, path.open("rb") as source_file:
                shutil.copyfileobj(source_file, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            # Recheck the copy before publication, including concurrent source edits.
            with temporary.open("rb") as copied:
                if hashlib.file_digest(copied, "sha256").hexdigest() != key:
                    raise ValueError("视频复制校验失败")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
