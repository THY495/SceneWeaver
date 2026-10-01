import base64
import json

import pytest

from chatbot import realtime_tts
from chatbot.providers import ProviderError


class Socket:
    def __init__(self, events):
        self.events = iter(events)
        self.sent = []
        self.closed = False

    async def recv(self):
        return json.dumps(next(self.events))

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def close(self):
        self.closed = True


def audio():
    return [
        {"type": "response.audio.delta", "delta": base64.b64encode(b"\x01\x00" * 100).decode()},
        {"type": "response.audio.done"},
        {"type": "response.done"},
    ]


@pytest.mark.asyncio
async def test_reuses_connection_without_replaying_previous_audio(config, monkeypatch):
    socket = Socket([{"type": "session.created"}, {"type": "session.updated"}, *audio(), *audio()])
    calls = []

    async def connect(*args, **kwargs):
        calls.append(args)
        return socket

    monkeypatch.setattr(realtime_tts, "connect", connect)
    service = realtime_tts.RealtimeTTS(config)
    for phrase in ["嗯。", "现在回答你的问题。"]:
        assert len(b"".join([part async for part in service.pcm(phrase, "Cherry")])) == 200
    assert len(calls) == 1
    assert [e["text"] for e in socket.sent if e["type"] == "input_text_buffer.append"] == [
        "嗯。",
        "现在回答你的问题。",
    ]
    await service.close()
    assert socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [{"type": "error", "message": "secret"}, {"type": "response.done"}])
async def test_failure_closes_connection_and_hides_provider_payload(config, monkeypatch, failure):
    socket = Socket([{"type": "session.created"}, {"type": "session.updated"}, failure])

    async def connect(*args, **kwargs):
        return socket

    monkeypatch.setattr(realtime_tts, "connect", connect)
    service = realtime_tts.RealtimeTTS(config)
    with pytest.raises(ProviderError) as err:
        _ = [part async for part in service.pcm("嗯。", "Cherry")]
    assert "secret" not in str(err.value)
    assert socket.closed and not service.connections


@pytest.mark.asyncio
async def test_cancel_mid_audio_discards_connection(config, monkeypatch):
    socket = Socket([{"type": "session.created"}, {"type": "session.updated"}, *audio()])

    async def connect(*args, **kwargs):
        return socket

    monkeypatch.setattr(realtime_tts, "connect", connect)
    service = realtime_tts.RealtimeTTS(config)
    stream = service.pcm("嗯。", "Cherry")
    await anext(stream)
    await stream.aclose()
    assert socket.closed and not service.connections
