"""Opt-in short WAV transcription. Audio and draft transcript are never persisted."""

import asyncio
import base64
import io
import json
import time
import wave
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from .providers import ProviderError
from .storage import Conversation, Task, now


class Recording(BaseModel):
    audio: str = Field(max_length=2_600_000)
    request_id: str = Field(min_length=1, max_length=80)
    epoch: int = Field(ge=0)


def decode_recording(encoded):
    try:
        raw = base64.b64decode(encoded, validate=True)
        with wave.open(io.BytesIO(raw)) as wav:
            frames = wav.getnframes()
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
                raise ValueError()
            if not 3200 <= frames <= 960000 or len(wav.readframes(frames)) != frames * 2:
                raise ValueError()
        return raw, frames / 16000
    except (ValueError, wave.Error, EOFError):
        raise HTTPException(400, "需要0.2～60秒、16kHz单声道16位WAV录音") from None


def speech_input_router(db, config, runtime, provider, who):
    router = APIRouter(prefix="/api/speech-input")
    slot = asyncio.Lock()

    @router.get("/config")
    def status(identity=Depends(who)):
        return {
            "available": config.product == "roleplay" and config.asr_enabled and bool(config.tts_key()),
            "mode": "record_review_send",
            "max_seconds": 60,
        }

    @router.post("/sessions/{sid}")
    async def transcribe(sid: str, request: Request, identity=Depends(who)):
        if config.product != "roleplay" or not config.asr_enabled or not config.tts_key():
            raise HTTPException(503, "语音输入尚未配置")
        if slot.locked():
            raise HTTPException(429, "正在识别另一段录音")
        async with slot:
            buffer = bytearray()
            async for part in request.stream():
                buffer.extend(part)
                if len(buffer) > 2_700_000:
                    raise HTTPException(413, "录音过长")
            try:
                body = Recording.model_validate(json.loads(buffer))
            except (ValueError, ValidationError):
                raise HTTPException(400, "无效录音请求") from None
            raw, seconds = decode_recording(body.audio)
            with db.session.begin() as s:
                person = s.scalar(select(Conversation).where(Conversation.id == sid).with_for_update())
                if not person or person.owner_id != identity or not person.current_state.get("player"):
                    raise HTTPException(404, "玩家会话不存在")
                if person.epoch != body.epoch:
                    raise HTTPException(409, "输入已更新，请重新录音")
                dedupe = f"asr:{sid}:{body.request_id}"
                if s.scalar(select(Task.id).where(Task.dedupe_key == dedupe)):
                    raise HTTPException(409, "此录音请求已处理，请勿重复提交")
                task = runtime.enqueue(s, "asr", identity, {}, session_id=sid, dedupe=dedupe)
                task.status = "running"
                task.lease_until = now() + timedelta(seconds=60)
                task.deadline = now() + timedelta(seconds=45)
            started, charge, state = time.monotonic(), None, "failed"
            try:
                charge = provider.reserve(task.id, "asr", config.asr_model, seconds, 0)
                response = await provider.http.post(
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    headers={"Authorization": "Bearer " + config.tts_key()},
                    json={
                        "model": config.asr_model,
                        "stream": False,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_audio",
                                        "input_audio": {
                                            "data": "data:audio/wav;base64," + base64.b64encode(raw).decode()
                                        },
                                    }
                                ],
                            }
                        ],
                        "asr_options": {"language": "zh", "enable_itn": False},
                    },
                    timeout=30,
                )
                if response.status_code != 200:
                    raise ProviderError(f"语音识别服务 HTTP {response.status_code}")
                transcript = response.json()["choices"][0]["message"]["content"]
                if not isinstance(transcript, str) or len(transcript) > 6000:
                    raise ProviderError("语音识别返回格式异常")
                with db.session() as s:
                    person = s.get(Conversation, sid)
                    if not person or person.epoch != body.epoch:
                        raise HTTPException(409, "输入已更新，识别草稿已丢弃")
                state = "completed"
                return {"text": transcript.strip(), "seconds": seconds, "epoch": body.epoch}
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(
                    502, str(exc) if isinstance(exc, ProviderError) else "语音识别失败，请重试"
                ) from None
            finally:
                if charge:
                    provider.settle(charge, None, time.monotonic() - started, state)
                with db.session.begin() as s:
                    current = s.get(Task, task.id)
                    current.status = state
                    current.result = {"audio_seconds": seconds}

    return router
