"""Windows player-local credentials. Never part of a story package or server deployment."""

import ctypes
import json
import os
import secrets
from ctypes import wintypes
from pathlib import Path

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from .storage import Story, Task, VoicePlayback


class PlayerCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # First release uses the existing priced DeepSeek route; arbitrary endpoints
    # must not bypass provider pricing or silently receive credentials.
    llm_key: SecretStr = SecretStr("")
    tts_key: SecretStr = SecretStr("")
    tts_region: str = "ap-southeast-1"
    tts_voice: str = Field(default="longanfengyue", min_length=1, max_length=200)
    daily_budget_usd: float = Field(default=2, gt=0, le=100)


def protect(data: bytes, *, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("Player credential storage requires Windows DPAPI")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buffer), Blob()
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError("Windows credential encryption/decryption failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def load(path: Path):
    if not path.exists():
        return PlayerCredentials()
    # A broken/foreign vault must never be silently replaced with an empty one.
    return PlayerCredentials.model_validate_json(protect(path.read_bytes(), decrypt=True))


def save(path: Path, value: PlayerCredentials):
    data = value.model_dump()
    data.update(llm_key=value.llm_key.get_secret_value(), tts_key=value.tts_key.get_secret_value())
    encrypted = protect(json.dumps(data).encode())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temporary.write_bytes(encrypted)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def apply(config, value):
    if value.tts_region not in {"cn-beijing", "ap-southeast-1"}:
        raise ValueError("Unsupported TTS region")
    config.api_key = value.llm_key
    config.api_key_file = ""
    config.api_base = "https://api.deepseek.com"
    config.tts_provider = "qwen_audio"
    config.tts_adapter_model = "qwen-audio-3.0-tts-flash"
    config.tts_endpoint = "shared"
    config.tts_region = value.tts_region
    config.tts_adapter_api_key = value.tts_key
    config.tts_enabled = bool(value.tts_key.get_secret_value())
    config.tts_voices = {value.tts_voice: value.tts_voice}
    config.daily_budget_usd = value.daily_budget_usd


def install(app, config, db, path):
    """Only the authenticated single-process Windows host may install this API."""
    if os.name != "nt" or not config.access_token.get_secret_value():
        raise RuntimeError("Native credential API requires Windows and host authentication")

    def authorize(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization, "Bearer " + config.access_token.get_secret_value()):
            raise HTTPException(401, "需要本机游戏授权")

    async def status(_=Depends(authorize)):
        return {
            "available": True,
            "llm_configured": bool(config.api_key.get_secret_value()),
            "tts_configured": bool(config.tts_adapter_api_key.get_secret_value()),
            "tts_region": config.tts_region,
            "tts_voice": next(iter(config.tts_voices), "longanfengyue"),
            "daily_budget_usd": config.daily_budget_usd,
        }

    async def update(body: PlayerCredentials, _=Depends(authorize)):
        if body.tts_region not in {"cn-beijing", "ap-southeast-1"}:
            raise HTTPException(422, "请选择支持的 TTS 地域")
        with db.session() as s:
            if s.scalar(select(Task.id).where(Task.status.in_(["queued", "running"])).limit(1)):
                raise HTTPException(409, "请等待当前生成结束后保存模型设置")
            if s.scalar(
                select(VoicePlayback.id)
                .where(VoicePlayback.status.notin_(["completed", "interrupted", "failed"]))
                .limit(1)
            ):
                raise HTTPException(409, "请先停止语音播放")
            if any(
                not story.settings.get("paused", True)
                and not story.settings.get("narrative", {}).get("finished_at")
                for story in s.scalars(select(Story))
            ):
                raise HTTPException(409, "请先暂停剧情再修改模型设置")
        save(path, body)
        # No await between validation, persistence and application. API and worker
        # share this Settings instance on the native host's single event loop.
        apply(config, body)
        app.state.runtime.model_readiness.signature = ""
        return {"saved": True, "message": "已在本机加密保存；开始游戏前将检查 LLM 连接。"}

    async def patch(body: PlayerCredentials, _=Depends(authorize)):
        # Omitted fields preserve the encrypted value. Explicit empty keys clear it.
        # Load failures must not replace an unreadable vault with defaults.
        previous = load(path)
        merged = PlayerCredentials.model_validate(
            {**previous.model_dump(), **body.model_dump(exclude_unset=True)}
        )
        return await update(merged)

    for method, endpoint in [("GET", status), ("PUT", update), ("PATCH", patch)]:
        app.add_api_route("/api/player/credentials", endpoint, methods=[method])
        app.router.routes.insert(0, app.router.routes.pop())
