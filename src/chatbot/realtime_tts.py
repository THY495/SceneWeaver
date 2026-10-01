"""Official Beijing TTS, one reusable websocket per voice with exclusive synthesis."""

import asyncio
import base64
import json
import time
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.protocol import State

from .providers import ProviderError
from .storage import uid


class RealtimeTTS:
    def __init__(self, config):
        self.config = config
        self.connections = {}
        self.locks = {}
        self.used = {}

    async def close(self):
        for ws in self.connections.values():
            await ws.close()
        self.connections.clear()

    async def _receive(self, ws):
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if event.get("type") == "error":
            # Provider messages can include request content; keep only a fixed diagnostic.
            raise ProviderError("实时语音服务拒绝请求")
        return event

    async def _send(self, ws, kind, **data):
        await ws.send(json.dumps({"event_id": uid(), "type": kind, **data}, ensure_ascii=False))

    async def _connection(self, voice):
        ws = self.connections.get(voice)
        try:
            if ws and (
                time.monotonic() - self.used.get(voice, 0) > 30
                or getattr(ws, "state", State.OPEN) != State.OPEN
            ):
                await ws.close()
                ws = None
            if ws is None:
                url = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?" + urlencode(
                    {"model": self.config.tts_realtime_model}
                )
                ws = await connect(
                    url,
                    additional_headers={"Authorization": "Bearer " + self.config.tts_key()},
                    proxy=None,
                    open_timeout=8,
                    close_timeout=1,
                    max_queue=4,
                    max_size=2**20,
                )
                if (await self._receive(ws)).get("type") != "session.created":
                    raise ProviderError("实时语音会话初始化失败")
                await self._send(
                    ws,
                    "session.update",
                    session={
                        "mode": "commit",
                        "voice": voice,
                        "language_type": "Chinese",
                        "response_format": "pcm",
                        "sample_rate": 24000,
                    },
                )
                if (await self._receive(ws)).get("type") != "session.updated":
                    raise ProviderError("实时语音会话配置失败")
                self.connections[voice] = ws
            self.used[voice] = time.monotonic()
            return ws
        except BaseException:
            self.connections.pop(voice, None)
            if ws:
                await ws.close()
            raise

    async def warm(self, voice):
        async with self.locks.setdefault(voice, asyncio.Lock()):
            await self._connection(voice)

    async def pcm(self, text, voice):
        lock = self.locks.setdefault(voice, asyncio.Lock())
        async with lock:
            ws = self.connections.get(voice)
            try:
                ws = await self._connection(voice)
                await self._send(ws, "input_text_buffer.append", text=text)
                await self._send(ws, "input_text_buffer.commit")
                audio_done = False
                while True:
                    event = await self._receive(ws)
                    if event.get("type") == "response.audio.delta":
                        data = base64.b64decode(event["delta"], validate=True)
                        if data:
                            yield data
                    elif event.get("type") == "response.audio.done":
                        audio_done = True
                    elif event.get("type") == "response.done":
                        if not audio_done or event.get("response", {}).get("status") in {
                            "failed",
                            "cancelled",
                        }:
                            raise ProviderError("实时语音未完整合成")
                        self.used[voice] = time.monotonic()
                        return
            except BaseException:
                self.connections.pop(voice, None)
                if ws:
                    await ws.close()
                raise
