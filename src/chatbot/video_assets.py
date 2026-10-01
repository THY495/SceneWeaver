"""Owner-scoped, immutable MP4 files; no player access is granted by a hash.

Container checks catch truncated/renamed uploads, not codec support. Playback
must still report decode errors. No external process or transcoder is invoked.
"""

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import struct
import tempfile
import time
from pathlib import Path

from fastapi import Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

MAX_VIDEO = 256 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


def video_dir(config, owner):
    return config.data_dir / "videos" / hashlib.sha256(owner.encode()).hexdigest()


def validate_mp4(path: Path):
    size = path.stat().st_size
    if size > MAX_VIDEO:
        raise HTTPException(413, "单个视频不能超过 256 MB。")
    seen = set()
    offset = 0
    boxes = 0
    with path.open("rb") as stream:
        while offset < size:
            boxes += 1
            if boxes > 4096 or size - offset < 8:
                raise HTTPException(422, "MP4 容器无效或不完整。")
            stream.seek(offset)
            length, kind = struct.unpack(">I4s", stream.read(8))
            header = 8
            if length == 1:
                if size - offset < 16:
                    raise HTTPException(422, "MP4 容器无效或不完整。")
                length = struct.unpack(">Q", stream.read(8))[0]
                header = 16
            elif length == 0:
                length = size - offset
            if length < header or offset + length > size:
                raise HTTPException(422, "MP4 容器无效或不完整。")
            if kind == b"ftyp":
                if length < header + 8 or length > 4096:
                    raise HTTPException(422, "MP4 格式标识无效。")
                brands = stream.read(length - header)
                supported = {b"isom", b"iso2", b"mp41", b"mp42", b"avc1"}
                if not ({brands[:4]} | {brands[i : i + 4] for i in range(8, len(brands), 4)}) & supported:
                    raise HTTPException(422, "请使用 MP4 容器，建议 H.264 视频与 AAC 音频。")
            if kind in {b"moov", b"mdat"} and length == header:
                raise HTTPException(422, "视频轨道或数据为空。")
            seen.add(kind)
            offset += length
    if not {b"ftyp", b"moov", b"mdat"} <= seen:
        raise HTTPException(422, "请上传完整的 MP4 文件。")


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def store_video(file, config, identity):
    folder = video_dir(config, identity)
    folder.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix="upload-", suffix=".tmp", dir=folder)
    temporary = Path(name)
    total = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(handle, "wb") as output:
            while chunk := await file.read(CHUNK_SIZE):
                total += len(chunk)
                if total > MAX_VIDEO:
                    raise HTTPException(413, "单个视频不能超过 256 MB。")
                digest.update(chunk)
                output.write(chunk)
        await asyncio.to_thread(validate_mp4, temporary)
        key = digest.hexdigest()
        destination = folder / (key + ".mp4")
        if destination.exists():
            if await asyncio.to_thread(file_hash, destination) != key:
                raise HTTPException(409, "已有视频缓存损坏，请先修复素材缓存。")
        else:
            # Publish only after closing and validating the full upload.
            temporary.replace(destination)
        return {"id": key, "bytes": total, "media_type": "video/mp4", "codec_verified": False}
    finally:
        temporary.unlink(missing_ok=True)


def video_response(config, identity, key):
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise HTTPException(404)
    path = video_dir(config, identity) / (key + ".mp4")
    if not path.is_file():
        raise HTTPException(404, "视频素材不存在，请重新上传。")
    return FileResponse(path, media_type="video/mp4", headers={"X-Content-Type-Options": "nosniff"})


def install_video_routes(router, config, who):
    signing_key = secrets.token_bytes(32)

    def signature(identity, key, expiry):
        return hmac.new(signing_key, f"{identity}:{key}:{expiry}".encode(), hashlib.sha256).hexdigest()

    @router.post("/videos/{key}/ticket")
    def preview_ticket(key: str, identity=Depends(who)):
        video_response(config, identity, key)
        expiry = int(time.time()) + 3600
        return {"ticket": f"{expiry}.{signature(identity, key, expiry)}"}

    @router.get("/video-preview/{key}")
    def preview(key: str, ticket: str = ""):
        if len(ticket) > 100 or ticket.count(".") != 1:
            raise HTTPException(401)
        expiry, supplied = ticket.split(".")
        if (
            not expiry.isascii()
            or not expiry.isdecimal()
            or int(expiry) <= time.time()
            or len(supplied) != 64
            or any(c not in "0123456789abcdef" for c in supplied)
        ):
            raise HTTPException(401)
        if not hmac.compare_digest(supplied, signature(config.workspace_owner, key, int(expiry))):
            raise HTTPException(401)
        response = video_response(config, config.workspace_owner, key)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @router.post("/videos", status_code=201)
    async def upload_video(file: UploadFile = File(...), identity=Depends(who)):
        return await store_video(file, config, identity)

    @router.get("/videos/{key}")
    def read_video(key: str, identity=Depends(who)):
        return video_response(config, identity, key)
