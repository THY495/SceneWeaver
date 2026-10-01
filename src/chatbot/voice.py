"""Optional caption-first voice output. Audio receipts never rewrite story facts."""

import asyncio
import base64
import hashlib
import io
import json
import re
import struct
import time
import wave
from contextlib import aclosing
from datetime import datetime, timedelta
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from .providers import Provider, ProviderError
from .storage import Conversation, Observation, SceneRecord, Task, VoiceDelivery, VoicePlayback, now, uid
from .tts_adapters import FishTTS, QwenAudioTTS, SpeechRequest, binding_scope, quote, selected_model

VOICES = {"Cherry": "芊悦", "Ethan": "晨煦", "Serena": "苏瑶", "Chelsie": "千雪"}
TERMINAL = {"completed", "interrupted", "failed"}


def abandoned_for_explicit_replay(prior: VoicePlayback, body: "PlaybackRequest") -> bool:
    """A new player page may take over only an inactive old-page attempt."""
    old_client = prior.timings.get("client_id")
    if not (body.replay and body.client_id and old_client and old_client != body.client_id):
        return False
    timestamp = prior.timings.get("last_receipt_at")
    try:
        last_activity = datetime.fromisoformat(timestamp) if timestamp else prior.created_at
    except ValueError:
        last_activity = prior.created_at
    return now() - last_activity > timedelta(seconds=3)


def validate_spoken_parts(text, parts):
    start = 0
    for part in parts:
        found = text.find(part, start)
        if not part.strip() or found < 0 or re.search(r"[<>\[\]（）()]", part):
            raise ProviderError("语音台词必须按顺序逐字来自已审核正文，且不含动作或控制标签。")
        start = found + len(part)


def chunks(record):
    if record.kind not in {"speech", "ack_candidate"}:
        return []
    parts = record.payload.get("spoken_parts", [])
    validate_spoken_parts(record.text, parts)
    result = []
    prefix = record.payload.get("voice_first_chunk", "")
    if prefix:
        if not parts or not parts[0].startswith(prefix) or len(prefix) > 80:
            raise ProviderError("预生成首句与朗读内容不一致。")
        result.append(prefix)
        parts = [parts[0][len(prefix) :], *parts[1:]]
    for part in parts:
        # Full text is already reviewed. Chunking here changes only synthesis, not approval.
        current = ""
        for sentence in re.findall(r"[^。！？!?；;\n]+[。！？!?；;\n]*", part):
            if len(current + sentence) > 500 and current:
                result.append(current)
                current = ""
            while len(sentence) > 500:
                result.append(sentence[:500])
                sentence = sentence[500:]
            current += sentence
        if current.strip():
            result.append(current)
    return result


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def utterance_epoch(s, record, sid):
    task = s.get(Task, record.task_id) if record.task_id else None
    if task:
        return task.epoch if task.session_id == sid else None
    if record.payload.get("source") == "作者固定对白":
        return record.payload.get("voice_epochs", {}).get(sid)
    return None


class PCMDecoder:
    """Accept raw PCM or streaming RIFF/WAV; never play container headers as samples."""

    def __init__(self):
        self.pending = b""
        self.mode = None
        self.valid_format = False

    def feed(self, raw):
        if self.mode == "pcm":
            return raw
        self.pending += raw
        if self.mode is None:
            if len(self.pending) < 12:
                return b""
            if self.pending[:4] != b"RIFF":
                self.mode = "pcm"
                result, self.pending = self.pending, b""
                return result
            if self.pending[8:12] != b"WAVE":
                raise ProviderError("不支持的音频容器")
            self.mode, self.pending = "wav", self.pending[12:]
        while len(self.pending) >= 8:
            kind, length = self.pending[:4], struct.unpack("<I", self.pending[4:8])[0]
            if kind == b"data":
                if not self.valid_format:
                    raise ProviderError("WAV 音频格式未验证")
                result, self.pending, self.mode = self.pending[8:], b"", "pcm"
                return result
            if length > 65536:
                raise ProviderError("WAV 头过长")
            size = 8 + length + length % 2
            if len(self.pending) < size:
                return b""
            if kind == b"fmt ":
                if length < 16:
                    raise ProviderError("WAV 格式无效")
                fmt, channels, rate, _, _, bits = struct.unpack("<HHIIHH", self.pending[8:24])
                if (fmt, channels, rate, bits) != (1, 1, 24000, 16):
                    raise ProviderError("语音格式必须为单声道 24kHz 16bit PCM")
                self.valid_format = True
            self.pending = self.pending[size:]
        return b""

    def finish(self):
        if self.mode == "wav" or self.pending.startswith(b"RIFF"):
            raise ProviderError("WAV 音频头不完整")
        result, self.pending = self.pending, b""
        return result


def actor_voice(s, actor, config=None):
    if config and config.tts_provider != "qwen_legacy":
        profile = actor.current_state.get("voice_binding", {}) if actor else {}
        if profile:
            if profile.get("scope") != binding_scope(config):
                return ""
            return profile.get("voice", "") if profile.get("voice") in config.tts_voices else ""
        return next(iter(config.tts_voices), "")
    if actor is None:
        return "Ethan"
    if actor.current_state.get("voice_profile") in VOICES:
        return actor.current_state["voice_profile"]
    cast = [
        c.id
        for c in s.scalars(
            select(Conversation).where(Conversation.story_id == actor.story_id).order_by(Conversation.id)
        )
        if not c.current_state.get("player")
    ]
    return list(VOICES)[cast.index(actor.id) % len(VOICES)]


class PlaybackRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    event_id: str
    chunk_index: int = Field(ge=0)
    epoch: int
    replay: bool = False
    client_id: str = Field(default="", max_length=80)
    prefetch: bool = False


class PlaybackStart(BaseModel):
    send_to_release_ms: float | None = Field(default=None, ge=0, le=3600000)
    output_latency_ms: float | None = Field(default=None, ge=0, le=10000)


class Receipt(BaseModel):
    seq: int = Field(ge=1)
    played_samples: int = Field(ge=0)
    status: Literal["playing", "completed", "interrupted", "failed"]
    first_audio_ms: float | None = Field(default=None, ge=0, le=3600000)
    first_audio_scheduled: bool = False


class Profile(BaseModel):
    voice: str = Field(min_length=1, max_length=200)


class VoicePreview(BaseModel):
    voice: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=120)
    direction: str = Field(default="", max_length=160)


class VoiceService:
    def available(self):
        if self.config.tts_provider != "qwen_legacy":
            try:
                if self.config.tts_provider == "fish":
                    FishTTS(None, "", selected_model(self.config))
                else:
                    QwenAudioTTS(
                        "",
                        selected_model(self.config),
                        self.config.tts_workspace,
                        self.config.tts_region,
                        endpoint=self.config.tts_endpoint,
                    )
            except ProviderError:
                return False
        try:
            key = (
                self.config.tts_key()
                if self.config.tts_provider == "qwen_legacy"
                else self.config.tts_adapter_api_key.get_secret_value()
            )
        except OSError:
            return False
        return (
            self.config.tts_enabled
            and bool(key)
            and (self.config.tts_provider == "qwen_legacy" or bool(self.config.tts_voices))
        )

    def __init__(self, db, config, runtime):
        self.db, self.config = db, config
        from .voice_references import restore_catalog

        restore_catalog(config)
        self.runtime = runtime
        self.billing = Provider(db, config, runtime)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(25, connect=10), trust_env=False)
        from .realtime_tts import RealtimeTTS

        self.realtime = RealtimeTTS(config)
        self.ack_realtime = RealtimeTTS(config)

    async def close(self):
        try:
            if getattr(self, "_candidate_producers", None):
                await self._candidate_producers.close()
        finally:
            closers = [
                self.realtime.close(),
                self.ack_realtime.close(),
                self.http.aclose(),
                self.billing.close(),
            ]
            if getattr(self, "_tts_connections", None):
                closers.append(self._tts_connections.close())
            results = await asyncio.gather(*closers, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result

    def qwen_audio(self, key, model):
        from .tts_connections import TTSConnections

        if not getattr(self, "_tts_connections", None):
            self._tts_connections = TTSConnections()
        return QwenAudioTTS(
            key,
            model,
            self.config.tts_workspace,
            self.config.tts_region,
            endpoint=self.config.tts_endpoint,
            connections=self._tts_connections,
        )

    def session(self, s, sid, owner):
        c = s.get(Conversation, sid)
        if not c or c.owner_id != owner or not c.current_state.get("player"):
            raise HTTPException(404, "玩家会话不存在")
        return c

    def event(self, s, c, eid):
        record = s.get(SceneRecord, eid)
        candidate_task = s.get(Task, record.task_id) if record and record.kind == "ack_candidate" else None
        candidate_allowed = bool(
            candidate_task
            and candidate_task.session_id == c.id
            and candidate_task.epoch == c.epoch
            and candidate_task.status in {"running", "completed"}
        )
        if (
            not record
            or record.redacted
            or record.story_id != c.story_id
            or record.actor_id == c.id
            or not (s.get(Observation, (c.id, eid)) or candidate_allowed)
        ):
            raise HTTPException(404, "发言不存在或不可访问")
        if candidate_allowed:
            return record
        from .observations import observed_record

        return observed_record(s, c.id, eid)

    def active(self, s, row):
        if not self.available():
            raise HTTPException(409, "语音服务配置已失效，请配置 TTS API Key 后重试")
        c = s.get(Conversation, row.session_id)
        record = self.event(s, c, row.event_id)
        if c.epoch != row.epoch or row.status in TERMINAL:
            raise HTTPException(409, "播放已停止或输入已更新")
        if row.timings.get("provider", "qwen_legacy") != self.config.tts_provider or row.timings.get(
            "model", selected_model(self.config)
        ) != selected_model(self.config):
            raise HTTPException(409, "语音配置已改变，请重新准备播放")
        text = chunks(record)[row.chunk_index]
        if digest(text) != row.text_hash:
            raise HTTPException(409, "台词已变化")
        if self.config.tts_provider != "qwen_legacy" and row.timings.get("binding_scope") != binding_scope(
            self.config
        ):
            raise HTTPException(409, "语音账号或地域已改变，请重新绑定音色")
        return text

    async def pcm(self, text, voice):
        if self.config.tts_transport == "realtime":
            async with aclosing(self.realtime.pcm(text, voice)) as source:
                async for frame in source:
                    yield frame
            return
        if (
            self.config.tts_api_url
            != "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
        ):
            raise ProviderError("此版本仅接入阿里云北京官方语音端点")
        body = {
            "model": self.config.tts_model,
            "input": {"text": text, "voice": voice, "language_type": "Chinese"},
        }
        headers = {"Authorization": "Bearer " + self.config.tts_key(), "X-DashScope-SSE": "enable"}
        saw_end = False
        decoder = PCMDecoder()
        async with self.http.stream("POST", self.config.tts_api_url, json=body, headers=headers) as response:
            if response.status_code != 200:
                raise ProviderError(f"语音服务返回 HTTP {response.status_code}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                value = json.loads(line[5:].strip())
                if value.get("code"):
                    raise ProviderError("语音服务返回错误，保留文字回答")
                output = value.get("output", {})
                encoded = output.get("audio", {}).get("data")
                if encoded:
                    pcm = decoder.feed(base64.b64decode(encoded, validate=True))
                    if pcm:
                        yield pcm
                saw_end |= output.get("finish_reason") == "stop"
        if not saw_end:
            raise ProviderError("音频连接提前结束")
        remainder = decoder.finish()
        if remainder:
            yield remainder

    async def stream(self, pid, request):
        usage = None
        adapter = None
        source = None
        started = time.monotonic()
        status = "failed"
        error = None
        try:
            with self.db.session() as s:
                row = s.get(VoicePlayback, pid)
                text = self.active(s, row)
                # Playback occurs after generation has consumed its own call
                # allowance. Charge on-demand TTS to the separate per-utterance
                # delivery ledger, while the daily budget remains shared.
                task_id = row.timings.get("billing_task_id") or s.get(SceneRecord, row.event_id).task_id
                ack = bool(s.get(SceneRecord, row.event_id).payload.get("acknowledgement"))
                voice = row.voice
                direction = row.timings.get("voice_direction", "")
                from .voice_cache import SpoolReference, read

                cached_audio = read(s, row, self.config, text)
            model = selected_model(self.config)
            usage = (
                None
                if cached_audio is not None
                else self.billing.reserve(
                    task_id, "tts", model, text, 0, tts_quote=quote(self.config, text, direction)
                )
            )
            total = 0
            carry = b""
            async with asyncio.timeout(90):
                source = (
                    self.ack_realtime.pcm(text, voice)
                    if ack
                    and self.config.tts_transport == "realtime"
                    and self.config.tts_provider == "qwen_legacy"
                    else self.pcm(text, voice)
                )
                if self.config.tts_provider != "qwen_legacy":
                    key = self.config.tts_adapter_api_key.get_secret_value()
                    adapter = (
                        FishTTS(self.http, key, model)
                        if self.config.tts_provider == "fish"
                        else self.qwen_audio(key, model)
                    )
                    source = adapter.stream(SpeechRequest(text, voice, direction))
                if cached_audio is not None:
                    await source.aclose()

                    async def cached_source():
                        if not isinstance(cached_audio, SpoolReference):
                            yield cached_audio
                            return
                        from . import voice_spool
                        from .voice_cache import scope

                        offset = 0
                        last_data_at = time.monotonic()
                        while True:
                            if await request.is_disconnected():
                                raise asyncio.CancelledError()
                            with self.db.session() as s:
                                live = s.get(VoicePlayback, pid)
                                approved_text = self.active(s, live)
                                data, state = voice_spool.read(
                                    s,
                                    cached_audio.key,
                                    live,
                                    approved_text=approved_text,
                                    voice_scope=scope(self.config),
                                    offset=offset,
                                )
                            if state == "failed":
                                raise ProviderError("语音尾段生成失败，已播放内容保留。")
                            if data:
                                offset += len(data)
                                last_data_at = time.monotonic()
                                yield data
                            if state == "completed":
                                return
                            if time.monotonic() - last_data_at > 10:
                                raise ProviderError("语音尾段等待超时。")
                            await asyncio.sleep(0.05)

                    source = cached_source()
                async for raw in source:
                    if await request.is_disconnected():
                        status = "interrupted"
                        return
                    raw = carry + raw
                    carry = raw[-1:] if len(raw) % 2 else b""
                    raw = raw[: len(raw) - len(carry)]
                    for offset in range(0, len(raw), 9600):
                        packet = raw[offset : offset + 9600]
                        total += len(packet) // 2
                        if total > 24000 * 180:
                            raise ProviderError("音频超过单段时长上限")
                        with self.db.session.begin() as s:
                            row = s.scalar(
                                select(VoicePlayback).where(VoicePlayback.id == pid).with_for_update()
                            )
                            self.active(s, row)
                            row.sent_samples = total
                            if not row.timings.get("first_byte_ms"):
                                row.timings = {
                                    **row.timings,
                                    "approved_audio_cache_hit": cached_audio is not None,
                                    "approved_audio_streaming": isinstance(cached_audio, SpoolReference),
                                    "first_byte_ms": round((time.monotonic() - started) * 1000),
                                }
                        yield packet
            if not total or carry:
                raise ProviderError("音频为空或采样不完整")
            with self.db.session.begin() as s:
                row = s.scalar(select(VoicePlayback).where(VoicePlayback.id == pid).with_for_update())
                self.active(s, row)
                row.stream_done = True
                row.status = "buffered"
                row.timings = {**row.timings, "synthesis_ms": round((time.monotonic() - started) * 1000)}
            status = "completed"
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        except Exception as exc:
            # A truncated stream is not playback completion; client checks stream_done before ACK.
            status = "failed"
            error = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
        finally:
            if source is not None:
                await source.aclose()
            with self.db.session.begin() as s:
                row = s.scalar(select(VoicePlayback).where(VoicePlayback.id == pid).with_for_update())
                if row and status != "completed" and row.status not in TERMINAL:
                    row.status = status
                if row and error:
                    row.timings = {**row.timings, "error": error[:200]}
                if row and adapter is not None:
                    row.timings = {**row.timings, "provider_phases": getattr(adapter, "timings", {})}
            if usage:
                self.billing.settle(usage, None, time.monotonic() - started, status)

    async def stream_candidate(self, task, text, voice, direction=""):
        from .voice_producer import CandidateProducers

        if not getattr(self, "_candidate_producers", None):
            self._candidate_producers = CandidateProducers(self)
        return await self._candidate_producers.start(task, text, voice, direction)

    async def discard_candidate(self, audio):
        if audio.spool_id and getattr(self, "_candidate_producers", None):
            await self._candidate_producers.discard(audio)

    async def buffer_candidate(self, task, text, voice, direction="", *, on_audio=None):
        """Synthesize bounded unpublished audio using the same adapters and ledger.

        Return bytes to the candidate owner only. No SceneRecord, playback ID or
        public file exists, so rejection can discard them without a revoke race.
        """
        from .voice_candidate import CandidateAudio

        if not text or len(text) > 80:
            raise ProviderError("预生成只允许最多80字的首句。")
        if not self.available():
            raise ProviderError("语音服务不可用。")
        self.runtime.check(task.id, task.lease_version)
        model = selected_model(self.config)
        scope = (
            binding_scope(self.config)
            if self.config.tts_provider != "qwen_legacy"
            else digest(self.config.tts_key() + model + self.config.tts_transport + self.config.tts_api_url)
        )
        if self.config.tts_provider == "qwen_legacy":
            direction = ""
        request = SpeechRequest(text, voice, direction)
        source = self.pcm(text, voice)
        adapter = None
        if self.config.tts_provider != "qwen_legacy":
            key = self.config.tts_adapter_api_key.get_secret_value()
            adapter = (
                FishTTS(self.http, key, model)
                if self.config.tts_provider == "fish"
                else self.qwen_audio(key, model)
            )
            source = adapter.stream(request)
        started = time.monotonic()
        usage = self.billing.reserve(
            task.id, "tts", model, text, 0, tts_quote=quote(self.config, text, direction)
        )
        status = "failed"
        data = bytearray()
        try:
            async with asyncio.timeout(25), aclosing(source):
                async for raw in source:
                    self.runtime.check(task.id, task.lease_version)
                    if len(data) + len(raw) > 24000 * 2 * 30:
                        raise ProviderError("预生成语音超过30秒缓冲限制。")
                    data.extend(raw)
                    if on_audio:
                        on_audio(CandidateAudio(text, voice, direction, scope, bytes(data)))
            if not data or len(data) % 2:
                raise ProviderError("预生成音频为空或采样不完整。")
            self.runtime.check(task.id, task.lease_version)
            status = "completed"
            return CandidateAudio(text, voice, direction, scope, bytes(data))
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        finally:
            from .observability.timing import annotate

            phases = getattr(adapter, "timings", {})
            annotate(
                audio_bytes=len(data),
                **{
                    target: phases[source]
                    for source, target in (
                        ("connection_ready_ms", "tts_connection_ms"),
                        ("connection_reused", "tts_connection_reused"),
                        ("provider_task_ready_ms", "tts_task_ready_ms"),
                        ("text_submitted_ms", "tts_text_submitted_ms"),
                        ("provider_first_pcm_ms", "tts_first_pcm_ms"),
                        ("provider_complete_ms", "tts_complete_ms"),
                        ("receive_wait_ms", "tts_receive_wait_ms"),
                        ("max_receive_wait_ms", "tts_max_receive_wait_ms"),
                        ("consumer_hold_ms", "tts_consumer_hold_ms"),
                        ("last_pcm_ms", "tts_last_pcm_ms"),
                        ("pcm_packets", "tts_pcm_packets"),
                    )
                    if source in phases
                },
            )
            self.billing.settle(usage, None, time.monotonic() - started, status)


def voice_router(service, owner):
    db, config = service.db, service.config
    router = APIRouter(prefix="/api/voice")

    def who(identity=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404)
        return identity

    @router.get("/config")
    def settings(identity=Depends(who)):
        return {
            "available": service.available(),
            "voices": VOICES if config.tts_provider == "qwen_legacy" else config.tts_voices,
            "sample_rate": 24000,
            "mode": "captions_primary",
            "model": selected_model(config),
            "provider": config.tts_provider,
            "capabilities": {
                "streaming_output": True,
                "natural_language_direction": config.tts_provider != "qwen_legacy",
                "existing_voice_binding": True,
                "reference_upload": False,
                "automatic_enrollment": False,
            },
            "transport": config.tts_transport,
        }

    @router.put("/actors/{aid}/profile")
    def profile(aid: str, body: Profile, identity=Depends(who)):
        if body.voice not in (VOICES if config.tts_provider == "qwen_legacy" else config.tts_voices):
            raise HTTPException(400, "音色不在当前账号配置的音色列表中")
        with db.session.begin() as s:
            actor = s.scalar(select(Conversation).where(Conversation.id == aid).with_for_update())
            if (
                not actor
                or actor.owner_id != identity
                or not actor.story_id
                or actor.current_state.get("player")
            ):
                raise HTTPException(404)
            actor.current_state = {**actor.current_state, "voice_profile": body.voice}
            if config.tts_provider != "qwen_legacy":
                actor.current_state = {
                    **actor.current_state,
                    "voice_binding": {
                        "provider": config.tts_provider,
                        "model": selected_model(config),
                        "scope": binding_scope(config),
                        "voice": body.voice,
                    },
                }
        return {"voice": body.voice}

    @router.post("/preview")
    async def preview(body: VoicePreview, identity=Depends(who)):
        if not service.available():
            raise HTTPException(409, "请先配置 TTS API Key、模型和音色")
        if body.voice not in (VOICES if config.tts_provider == "qwen_legacy" else config.tts_voices):
            raise HTTPException(400, "音色不在当前账号列表中")
        if config.tts_provider == "qwen_legacy" and body.direction:
            raise HTTPException(400, "当前基础适配不支持语气指令，请切换支持该能力的服务")
        try:
            speech = SpeechRequest(body.text, body.voice, body.direction)
            validate_spoken_parts(body.text, [body.text])
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from exc
        task_id = uid()
        with db.session.begin() as s:
            s.add(
                Task(
                    id=task_id,
                    owner_id=identity,
                    kind="voice_preview",
                    status="completed",
                    deadline=now() + timedelta(seconds=60),
                )
            )
        usage, started, status = None, time.monotonic(), "failed"
        adapter = None
        try:
            model = selected_model(config)
            usage = service.billing.reserve(
                task_id, "tts", model, body.text, 0, tts_quote=quote(config, body.text, body.direction)
            )
            if config.tts_provider == "qwen_legacy":
                source = service.pcm(body.text, body.voice)
            else:
                key = config.tts_adapter_api_key.get_secret_value()
                adapter = (
                    FishTTS(service.http, key, model)
                    if config.tts_provider == "fish"
                    else service.qwen_audio(key, model)
                )
                source = adapter.stream(speech)
            audio = bytearray()
            async with asyncio.timeout(45), aclosing(source):
                async for packet in source:
                    audio.extend(packet)
                    if len(audio) > 24000 * 2 * 30:
                        raise ProviderError("试听超过 30 秒限制")
            if not audio or len(audio) % 2:
                raise ProviderError("试听未返回完整音频")
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(audio)
            status = "completed"
            return {
                "audio": base64.b64encode(output.getvalue()).decode(),
                "mime": "audio/wav",
                "duration_ms": len(audio) / 48,
                "provider": config.tts_provider,
                "timings": getattr(adapter, "timings", {}),
            }
        except Exception as exc:
            raise HTTPException(409, "语音试听失败，请检查服务配置、网络和预算。") from exc
        finally:
            if usage:
                service.billing.settle(usage, None, time.monotonic() - started, status)

    @router.post("/sessions/{sid}/warm")
    async def warm(sid: str, identity=Depends(who)):
        with db.session() as s:
            player = service.session(s, sid, identity)
            from .observations import members

            voices = sorted(
                {
                    actor_voice(s, a, config)
                    for a in members(s, player.story_id)
                    if a.id != player.id
                    and a.current_state.get("location", "main")
                    == player.current_state.get("location", "main")
                }
            )
        if (
            not service.available()
            or config.tts_provider != "qwen_legacy"
            or config.tts_transport != "realtime"
        ):
            return {"ready": False}

        async def connect(voice, pool):
            try:
                async with asyncio.timeout(10):
                    await pool.warm(voice)
                return True
            except Exception:
                return False

        results = await asyncio.gather(
            *(
                connect(voice, pool)
                for voice in voices[:4]
                for pool in (service.realtime, service.ack_realtime)
            )
        )
        return {"ready": bool(results) and all(results)}

    @router.get("/sessions/{sid}/utterances")
    def utterances(sid: str, after: int = 0, identity=Depends(who)):
        with db.session() as s:
            c = service.session(s, sid, identity)
            records = list(
                s.scalars(
                    select(SceneRecord)
                    .outerjoin(
                        Observation, (Observation.event_id == SceneRecord.id) & (Observation.actor_id == sid)
                    )
                    .where(
                        or_(
                            Observation.actor_id == sid,
                            (SceneRecord.kind == "ack_candidate")
                            & SceneRecord.task_id.in_(
                                select(Task.id).where(
                                    Task.session_id == sid,
                                    Task.epoch == c.epoch,
                                    Task.status.in_(["running", "completed"]),
                                )
                            ),
                        ),
                        or_(SceneRecord.seq > after, SceneRecord.kind == "ack_candidate"),
                        SceneRecord.redacted.is_(False),
                        SceneRecord.kind.in_(["speech", "ack_candidate"]),
                        SceneRecord.actor_id != sid,
                    )
                    .order_by(SceneRecord.seq.desc())
                    .limit(100)
                )
            )
            rows = []
            for record in reversed(records):
                if record.kind != "ack_candidate":
                    from .observations import observed_record

                    record = observed_record(s, sid, record.id)
                actor = s.get(Conversation, record.actor_id) if record.actor_id else None
                task = s.get(Task, record.task_id) if record.task_id else None
                voice_epoch = utterance_epoch(s, record, sid)
                if voice_epoch is None:
                    continue
                delivery = s.get(VoiceDelivery, (record.id, sid))
                from .player_release import pending

                withheld = pending(s, c, record)
                parts = chunks(record)
                rows.append(
                    {
                        "id": record.id,
                        "task_id": task.id if task else "",
                        # A pending acknowledgement has no world sequence yet.
                        # Zero is only a transport placeholder; the client keys
                        # its fast-channel queue by the submitted task ID.
                        "seq": record.seq if record.seq is not None else 0,
                        "actor_id": record.actor_id,
                        "actor_name": record.actor_name,
                        "epoch": voice_epoch,
                        "voice": actor_voice(s, actor, config),
                        "acknowledgement": bool(record.payload.get("acknowledgement")),
                        "chunks": ["" for _ in parts] if withheld else parts,
                        "presentation_pending": withheld,
                        "text_fallback": record.payload.get("presentation", {}).get("mode")
                        == "text_fallback",
                        "text_visible": bool(delivery and delivery.text_visible),
                    }
                )
            return {"epoch": c.epoch, "utterances": rows}

    @router.post("/sessions/{sid}/utterances/{eid}/visible")
    def visible(sid: str, eid: str, identity=Depends(who)):
        with db.session.begin() as s:
            c = service.session(s, sid, identity)
            s.execute(select(Conversation).where(Conversation.id == sid).with_for_update())
            record = service.event(s, c, eid)
            row = s.get(VoiceDelivery, (eid, sid))
            if not row:
                row = VoiceDelivery(event_id=eid, session_id=sid)
                s.add(row)
            row.text_visible, row.updated_at = True, now()
            from .player_release import grant

            release = grant(s, c, record, reason="text_mode")
        return {"ok": True, "release": release}

    @router.post("/sessions/{sid}/playbacks")
    def prepare(sid: str, body: PlaybackRequest, identity=Depends(who)):
        if not service.available():
            raise HTTPException(503, "尚未配置语音服务")
        with db.session.begin() as s:
            c = service.session(s, sid, identity)
            s.execute(select(Conversation).where(Conversation.id == sid).with_for_update())
            s.refresh(c)
            record = service.event(s, c, body.event_id)
            voice_epoch = utterance_epoch(s, record, sid)
            parts = chunks(record)
            if voice_epoch is None or body.chunk_index >= len(parts):
                raise HTTPException(400, "没有可播放台词")
            old = s.scalar(
                select(VoicePlayback).where(
                    VoicePlayback.session_id == sid, VoicePlayback.request_id == body.request_id
                )
            )
            if old:
                if (old.event_id, old.chunk_index, old.epoch) != (
                    body.event_id,
                    body.chunk_index,
                    body.epoch,
                ):
                    raise HTTPException(409, "重复请求不一致")
                return {"id": old.id, "status": old.status}
            if body.epoch != c.epoch or (not body.replay and voice_epoch != c.epoch):
                raise HTTPException(409, "旧轮次不会自动播放")
            if not body.replay and record.payload.get("presentation", {}).get("mode") == "text_fallback":
                raise HTTPException(409, "本段已改用文字，不自动重试语音；可手动重听")
            # One active audio attempt per session, including tabs; stale attempts expire.
            active = list(
                s.scalars(
                    select(VoicePlayback).where(
                        VoicePlayback.session_id == sid, VoicePlayback.status.not_in(TERMINAL)
                    )
                )
            )
            for prior in active:
                if prior.epoch != c.epoch or (now() - prior.created_at).total_seconds() > 180:
                    prior.status = "interrupted"
                elif abandoned_for_explicit_replay(prior, body):
                    prior.status = "interrupted"
                    prior.timings = {**prior.timings, "interrupted_reason": "explicit_replay_takeover"}
                elif not (
                    body.prefetch
                    and body.client_id
                    and prior.timings.get("client_id") == body.client_id
                    and len(active) < 2
                ):
                    raise HTTPException(409, "此会话已有待播放音频，请先停止")
            actor = s.get(Conversation, record.actor_id) if record.actor_id else None
            voice = actor_voice(s, actor, config)
            if not voice:
                raise HTTPException(400, "角色音色绑定失效，请选择当前服务可用的音色")
            direction = ""
            for segment in record.payload.get("performance", []):
                if parts[body.chunk_index] in segment.get("text", ""):
                    direction = segment.get("voice_direction", "")
                    break
            if config.tts_provider != "qwen_legacy":
                try:
                    SpeechRequest(parts[body.chunk_index], voice, direction)
                except ProviderError as exc:
                    raise HTTPException(400, str(exc)) from exc
            # Candidate synthesis belongs to the generation task; delivery and
            # manual replay have their own bounded call allowance. Otherwise a
            # long directed scene can exhaust generation calls before its final
            # approved sentence is audible. Keep one stable ledger per utterance.
            key = f"voice-delivery:{sid}:{record.id}"
            billing_task = s.scalar(select(Task).where(Task.dedupe_key == key))
            if not billing_task:
                billing_task = Task(
                    id=uid(),
                    owner_id=c.owner_id,
                    session_id=sid,
                    kind="voice_delivery",
                    status="completed",
                    epoch=voice_epoch,
                    deadline=now(),
                    dedupe_key=key,
                    payload={"event_id": record.id, "source_task_id": record.task_id},
                )
                s.add(billing_task)
            billing_task_id = billing_task.id
            row = VoicePlayback(
                id=uid(),
                event_id=record.id,
                session_id=sid,
                request_id=body.request_id,
                chunk_index=body.chunk_index,
                epoch=c.epoch,
                text_hash=digest(parts[body.chunk_index]),
                voice=voice,
                timings={
                    "provider": config.tts_provider,
                    "binding_scope": binding_scope(config) if config.tts_provider != "qwen_legacy" else "",
                    "model": selected_model(config),
                    "voice_direction": direction if config.tts_provider != "qwen_legacy" else "",
                    "billing_quote": dict(
                        zip(
                            ("units", "unit", "usd_per_million"),
                            quote(
                                config,
                                parts[body.chunk_index],
                                direction if config.tts_provider != "qwen_legacy" else "",
                            ),
                        )
                    ),
                    "billing_task_id": billing_task_id,
                    "input_characters": len(parts[body.chunk_index]),
                    "transport": config.tts_transport,
                    "client_id": body.client_id,
                    "replay": body.replay,
                    "prefetch": body.prefetch,
                    "reserved_usd_per_million_chars": config.tts_usd_per_million_chars,
                },
            )
            s.add(row)
        return {"id": row.id, "status": "queued"}

    def playback(s, pid, identity, lock=False):
        q = select(VoicePlayback).where(VoicePlayback.id == pid)
        row = s.scalar(q.with_for_update() if lock else q)
        if not row:
            raise HTTPException(404)
        c = service.session(s, row.session_id, identity)
        service.event(s, c, row.event_id)
        return row

    @router.get("/playbacks/{pid}")
    def state(pid: str, identity=Depends(who)):
        with db.session() as s:
            row = playback(s, pid, identity)
            return {
                "id": pid,
                "status": row.status,
                "stream_done": row.stream_done,
                "sent_samples": row.sent_samples,
                "played_samples": row.played_samples,
                "timings": row.timings,
            }

    @router.post("/playbacks/{pid}/start")
    def start(pid: str, body: PlaybackStart, identity=Depends(who)):
        from .acknowledgements import publish_acknowledgement
        from .storage import Story

        with db.session.begin() as s:
            row = playback(s, pid, identity)
            c = service.session(s, row.session_id, identity)
            s.scalar(select(Story).where(Story.id == c.story_id).with_for_update())
            s.scalar(select(Conversation).where(Conversation.id == c.id).with_for_update())
            s.refresh(c)
            row = playback(s, pid, identity, True)
            service.active(s, row)
            if not row.sent_samples:
                raise HTTPException(409, "音频尚未就绪")
            others = s.scalars(
                select(VoicePlayback).where(
                    VoicePlayback.session_id == c.id,
                    VoicePlayback.id != pid,
                    VoicePlayback.status.not_in(TERMINAL),
                )
            )
            if any(p.timings.get("started") for p in others):
                raise HTTPException(409, "另一段音频仍在播放")
            record = service.event(s, c, row.event_id)
            if record.kind == "ack_candidate":
                # Main speech wins until acknowledgement is released.
                if s.scalar(
                    select(SceneRecord.id).where(
                        SceneRecord.task_id == record.task_id,
                        SceneRecord.kind == "speech",
                        SceneRecord.actor_id != c.id,
                    )
                ):
                    raise HTTPException(409, "正文已就绪，跳过接话")
                publish_acknowledgement(s, service.runtime, c, record)
            row.timings = {**row.timings, "started": True, **body.model_dump(exclude_none=True)}
            from .player_release import grant

            release = grant(s, c, record, reason="audio_ready", playback_id=row.id)
        return {"ok": True, "release": release}

    @router.post("/playbacks/{pid}/receipt")
    def receipt(pid: str, body: Receipt, identity=Depends(who)):
        with db.session.begin() as s:
            row = playback(s, pid, identity, True)
            if body.seq <= row.receipt_seq or row.status == "completed":
                return {"ok": True, "status": row.status}
            if row.status in TERMINAL and body.status == "completed":
                return {"ok": True, "status": row.status}
            if body.played_samples < row.played_samples or body.played_samples > row.sent_samples:
                raise HTTPException(400, "播放水位无效")
            if body.status == "completed" and row.status not in TERMINAL:
                service.active(s, row)
                if not row.stream_done or body.played_samples != row.sent_samples or not row.sent_samples:
                    raise HTTPException(409, "音频尚未完整播放")
            row.receipt_seq, row.played_samples = body.seq, body.played_samples
            if body.status != "playing" and row.status not in TERMINAL:
                row.status = body.status
            row.timings = {**row.timings, "last_receipt_at": now().isoformat()}
            if body.first_audio_ms is not None and "first_audio_ms" not in row.timings:
                row.timings = {**row.timings, "first_audio_ms": body.first_audio_ms}
            if body.first_audio_scheduled and "first_audio_schedule_receipt_at" not in row.timings:
                row.timings = {**row.timings, "first_audio_schedule_receipt_at": now().isoformat()}
        return {"ok": True, "status": row.status}

    @router.post("/playbacks/{pid}/audio")
    async def audio(pid: str, request: Request, identity=Depends(who)):
        with db.session.begin() as s:
            row = playback(s, pid, identity, True)
            service.active(s, row)
            if row.status != "queued":
                raise HTTPException(409, "音频流已经消费；重听需显式创建新的播放")
            row.status = "streaming"
        return StreamingResponse(
            service.stream(pid, request),
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Audio-Sample-Rate": "24000",
                "X-Audio-Format": "pcm_s16le",
            },
        )

    return router
