import json

import httpx
import pytest

from chatbot.config import Settings
from chatbot.providers import ProviderError
from chatbot.tts_adapters import FishTTS, QwenAudioTTS, SpeechRequest, quote
from chatbot.tts_connections import TTSConnections


@pytest.mark.asyncio
async def test_fish_preserves_caption_and_counts_encoded_instruction():
    request = SpeechRequest("你好。", "voice-test", "轻声")

    def handler(req):
        body = json.loads(req.content)
        assert body["text"] == "[轻声] 你好。"
        assert body["sample_rate"] == 24000
        assert req.headers["model"] == "s2.1-pro"
        return httpx.Response(200, headers={"content-type": "audio/pcm"}, content=b"\x01\x00" * 40)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = b"".join([p async for p in FishTTS(http, "test").stream(request)])
    assert len(result) == 80
    assert request.text == "你好。"
    units, unit, rate = quote(Settings(tts_provider="fish"), request.text, request.direction)
    assert units == len("[轻声] 你好。".encode()) and unit == "utf8_bytes" and rate == 15


@pytest.mark.asyncio
async def test_fish_does_not_play_json_errors_as_pcm():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"error": "private"}))
    ) as http:
        with pytest.raises(ProviderError, match="非音频"):
            _ = [p async for p in FishTTS(http, "test").stream(SpeechRequest("你好", "v"))]


@pytest.mark.asyncio
async def test_qwen_task_order_and_close_on_cancel(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("chatbot.tts_adapters.time.monotonic", lambda: clock[0])

    class Socket:
        def __init__(self):
            self.sent, self.closed, self.count = [], False, 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def send(self, text):
            self.sent.append(json.loads(text))

        async def recv(self):
            clock[0] += 0.2
            self.count += 1
            if self.count == 1:
                assert len(self.sent) == 1
                return json.dumps(
                    {"header": {"event": "task-started", "task_id": self.sent[0]["header"]["task_id"]}}
                )
            assert [v["header"]["action"] for v in self.sent] == ["run-task", "continue-task", "finish-task"]
            return b"\x01\x00"

    socket = Socket()
    adapter = QwenAudioTTS(
        "test", "qwen-audio-3.0-tts-flash", "workspace", "cn-beijing", connector=lambda *a, **k: socket
    )
    source = adapter.stream(SpeechRequest("你好", "voice", "轻声"))
    assert await anext(source) == b"\x01\x00"
    clock[0] += 0.7  # Consumer work must not be attributed to socket receive.
    await source.aclose()
    assert socket.closed
    assert socket.sent[0]["payload"]["parameters"]["instruction"] == "轻声"
    assert adapter.timings["receive_wait_ms"] == 400
    assert adapter.timings["max_receive_wait_ms"] == 200
    assert adapter.timings["consumer_hold_ms"] == 700
    assert adapter.timings["pcm_packets"] == 1


def test_unknown_models_and_control_tags_are_rejected():
    with pytest.raises(ProviderError):
        FishTTS(None, "test", "typo-model")
    with pytest.raises(ProviderError):
        SpeechRequest("你好", "v", "[speaker:other]")
    with pytest.raises(ProviderError):
        QwenAudioTTS("test", "qwen-audio-3.1-tts-flash", "ws", "ap-southeast-1")


class ReusableSocket:
    def __init__(self, fail=False):
        self.sent, self.events, self.closed, self.fail = [], [], False, fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, text):
        value = json.loads(text)
        self.sent.append(value)
        if value["header"]["action"] == "run-task":
            task = value["header"]["task_id"]
            self.events = [
                json.dumps({"header": {"event": "task-started", "task_id": task}}),
                b"\x01\x00",
                json.dumps(
                    {"header": {"event": "task-failed" if self.fail else "task-finished", "task_id": task}}
                ),
            ]

    async def recv(self):
        return self.events.pop(0)


async def test_qwen_reuses_only_finished_tasks_with_new_ids_and_voice_parameters():
    pool, socket = TTSConnections(), ReusableSocket()
    created = []

    def connector(*args, **kwargs):
        created.append(1)
        return socket

    adapters = []
    for voice in ["voice-a", "voice-b"]:
        adapter = QwenAudioTTS(
            "key", "qwen-audio-3.0-tts-flash", "ws", "cn-beijing", connector=connector, connections=pool
        )
        assert b"".join([p async for p in adapter.stream(SpeechRequest("你好", voice))]) == b"\x01\x00"
        adapters.append(adapter)
    assert len(created) == 1 and not socket.closed
    runs = [v for v in socket.sent if v["header"]["action"] == "run-task"]
    assert len({v["header"]["task_id"] for v in runs}) == 2
    assert [v["payload"]["parameters"]["voice"] for v in runs] == ["voice-a", "voice-b"]
    assert [a.timings["connection_reused"] for a in adapters] == [False, True]
    await pool.close()
    assert socket.closed


@pytest.mark.parametrize("failed", [False, True])
async def test_qwen_pool_drops_partial_or_failed_task(failed):
    pool, socket = TTSConnections(), ReusableSocket(fail=failed)
    adapter = QwenAudioTTS(
        "key",
        "qwen-audio-3.0-tts-flash",
        "ws",
        "cn-beijing",
        connector=lambda *a, **k: socket,
        connections=pool,
    )
    source = adapter.stream(SpeechRequest("你好", "voice"))
    assert await anext(source) == b"\x01\x00"
    if failed:
        with pytest.raises(ProviderError):
            await anext(source)
    else:
        await source.aclose()
    assert socket.closed and not pool.idle and not pool.active
    await pool.close()


def test_qwen_explicit_shared_endpoint_stays_in_selected_region():
    for region, host in [
        ("cn-beijing", "dashscope.aliyuncs.com"),
        ("ap-southeast-1", "dashscope-intl.aliyuncs.com"),
    ]:
        adapter = QwenAudioTTS("test", "qwen-audio-3.0-tts-flash", "", region, endpoint="shared")
        assert adapter.url == f"wss://{host}/api-ws/v1/inference"
    with pytest.raises(ProviderError):
        QwenAudioTTS("test", "qwen-audio-3.0-tts-flash", "", "ap-southeast-1")
