"""Provider wire formats. Input is reviewed speech, output is 24 kHz mono PCM16."""

import asyncio
import hashlib
import json
import re
import time
import uuid
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass

from websockets.asyncio.client import connect

from .providers import ProviderError
from .tts_connections import ConnectionLease
from .tts_diagnostics import failure_fields


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    voice: str
    direction: str = ""

    def __post_init__(self):
        if not self.text.strip() or len(self.text) > 500 or not self.voice.strip():
            raise ProviderError("语音需要非空短台词与已绑定音色")
        if len(self.direction) > 160 or re.search(r"[<>\[\]\r\n]", self.direction):
            raise ProviderError("语气描述含不支持的控制符或过长")


def fish_text(request):
    if re.search(r"[<>\[\]]", request.text):
        raise ProviderError("正文不能携带 Fish 控制标签")
    return (f"[{request.direction}] " if request.direction else "") + request.text


class FishTTS:
    def __init__(self, http, key, model="s2.1-pro"):
        if model not in {"s2-pro", "s2.1-pro", "s2.1-pro-free"}:
            raise ProviderError("未验证的 Fish 型号，拒绝供应商静默回退")
        self.http, self.key, self.model = http, key, model

    async def stream(self, request):
        body = {
            "text": fish_text(request),
            "reference_id": request.voice,
            "format": "pcm",
            "sample_rate": 24000,
            "latency": "normal",
        }
        async with self.http.stream(
            "POST",
            "https://api.fish.audio/v1/tts",
            json=body,
            headers={"Authorization": "Bearer " + self.key, "model": self.model},
        ) as response:
            if response.status_code != 200:
                raise ProviderError(f"Fish 语音服务返回 HTTP {response.status_code}")
            content_type = response.headers.get("content-type", "").lower()
            if not (content_type.startswith("audio/") or content_type.startswith("application/octet-stream")):
                raise ProviderError("Fish 返回了非音频响应")
            async for packet in response.aiter_bytes():
                if packet:
                    yield packet


class QwenAudioTTS:
    def __init__(
        self, key, model, workspace, region, connector=connect, *, endpoint="workspace", connections=None
    ):
        if model not in {"qwen-audio-3.0-tts-flash", "qwen-audio-3.0-tts-plus", "qwen-audio-3.1-tts-flash"}:
            raise ProviderError("未验证的 Qwen-Audio-TTS 型号")
        if region not in {"cn-beijing", "ap-southeast-1"} or endpoint not in {"workspace", "shared"}:
            raise ProviderError("无效的阿里云地域或端点类型")
        if endpoint == "workspace" and not re.fullmatch(r"[A-Za-z0-9-]{1,80}", workspace):
            raise ProviderError("请配置有效的阿里云业务空间与地域")
        if model == "qwen-audio-3.1-tts-flash" and region != "cn-beijing":
            raise ProviderError("当前仅验证北京地域支持此型号")
        self.key, self.model, self.connector = key, model, connector
        self.connections = connections
        self.url = f"wss://{workspace}.{region}.maas.aliyuncs.com/api-ws/v1/inference"
        self.timings = {}
        if endpoint == "shared":
            host = "dashscope-intl.aliyuncs.com" if region == "ap-southeast-1" else "dashscope.aliyuncs.com"
            self.url = f"wss://{host}/api-ws/v1/inference"

    @asynccontextmanager
    async def connection(self):
        def factory():
            return self.connector(
                self.url,
                additional_headers={"Authorization": "Bearer " + self.key},
                proxy=None,
                open_timeout=8,
                close_timeout=1,
                max_size=2**20,
            )

        if self.connections is not None:
            async with self.connections.lease(self.url, self.key, factory) as lease:
                yield lease
        else:
            async with factory() as ws:
                yield ConnectionLease(None, ws, "")

    async def warm(self):
        """Open one idle connection without submitting text or incurring TTS usage."""
        if self.connections is None:
            return False
        async with self.connection() as lease:
            lease.reusable = True
        return True

    async def stream(self, request):
        self.timings = {"phase": "connect"}
        try:
            async with aclosing(self._stream(request)) as source:
                async for packet in source:
                    yield packet
            self.timings["phase"] = "completed"
        except Exception as exc:
            self.timings.update(failure_fields(exc, self.timings["phase"]))
            raise

    async def _stream(self, request):
        task = str(uuid.uuid4())
        start = time.monotonic()

        def mark(name):
            self.timings.setdefault(name, round((time.monotonic() - start) * 1000, 1))

        def message(action, payload):
            return json.dumps(
                {"header": {"action": action, "task_id": task, "streaming": "duplex"}, "payload": payload},
                ensure_ascii=False,
            )

        # Each running task has an exclusive lease; incomplete streams are never recycled.
        async with self.connection() as lease:
            ws = lease.socket
            mark("connection_ready_ms")
            self.timings["connection_reused"] = lease.reused
            self.timings["phase"] = "submit_task"
            await ws.send(
                message(
                    "run-task",
                    {
                        "task_group": "audio",
                        "task": "tts",
                        "function": "SpeechSynthesizer",
                        "model": self.model,
                        "parameters": {
                            "text_type": "PlainText",
                            "voice": request.voice,
                            "format": "pcm",
                            "sample_rate": 24000,
                            "instruction": request.direction,
                        },
                        "input": {},
                    },
                )
            )
            started = False
            while True:
                self.timings["phase"] = "receive_audio" if started else "await_start"
                waiting = time.monotonic()
                try:
                    event = await asyncio.wait_for(ws.recv(), 20)
                finally:
                    elapsed = (time.monotonic() - waiting) * 1000
                    self.timings["receive_wait_ms"] = round(
                        self.timings.get("receive_wait_ms", 0) + elapsed, 1
                    )
                    self.timings["max_receive_wait_ms"] = round(
                        max(self.timings.get("max_receive_wait_ms", 0), elapsed), 1
                    )
                if isinstance(event, bytes):
                    if not started:
                        raise ProviderError("语音协议在任务开始前返回音频")
                    mark("provider_first_pcm_ms")
                    self.timings["pcm_packets"] = self.timings.get("pcm_packets", 0) + 1
                    self.timings["last_pcm_ms"] = round((time.monotonic() - start) * 1000, 1)
                    consumed = time.monotonic()
                    try:
                        self.timings["phase"] = "deliver_audio"
                        yield event
                    finally:
                        self.timings["consumer_hold_ms"] = round(
                            self.timings.get("consumer_hold_ms", 0) + (time.monotonic() - consumed) * 1000,
                            1,
                        )
                    continue
                header = json.loads(event).get("header", {})
                if header.get("task_id") != task:
                    raise ProviderError("语音响应任务标识不匹配")
                kind = header.get("event")
                if kind == "task-started" and not started:
                    mark("provider_task_ready_ms")
                    started = True
                    self.timings["phase"] = "submit_text"
                    await ws.send(message("continue-task", {"input": {"text": request.text}}))
                    await ws.send(message("finish-task", {"input": {}}))
                    mark("text_submitted_ms")
                elif kind == "task-finished" and started:
                    mark("provider_complete_ms")
                    lease.reusable = True
                    return
                elif kind == "task-failed":
                    raise ProviderError("阿里云语音任务失败；保留字幕")


def selected_model(config):
    if config.tts_provider != "qwen_legacy":
        return config.tts_adapter_model
    return config.tts_realtime_model if config.tts_transport == "realtime" else config.tts_model


def binding_scope(config):
    # Local binding fingerprint only; never export credentials with a voice asset.
    value = [
        config.tts_provider,
        selected_model(config),
        config.tts_region,
        config.tts_workspace,
        config.tts_endpoint,
        config.tts_adapter_api_key.get_secret_value(),
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def quote(config, text, direction=""):
    if config.tts_provider == "fish":
        value = (f"[{direction}] " if direction else "") + text
        return len(value.encode("utf-8")), "utf8_bytes", config.tts_usd_per_million_bytes
    return len(text), "characters", config.tts_usd_per_million_chars
