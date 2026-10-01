import base64
import io
import wave

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from support.story import launch

from chatbot.api import create_app
from chatbot.providers import Provider
from chatbot.speech_input import decode_recording
from chatbot.storage import Task, Usage


def recording(seconds=1, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(seconds * rate))
    return base64.b64encode(buffer.getvalue()).decode()


@pytest.mark.parametrize(
    "encoded",
    ["bad", recording(0.1), recording(61), recording(rate=24000)],
    ids=["base64", "short", "long", "rate"],
)
def test_rejects_invalid_audio_before_provider(encoded):
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        decode_recording(encoded)


def test_transcription_is_private_draft_and_duplicate_is_not_billed_twice(db, config, runtime):
    config.product = "roleplay"
    config.asr_enabled = True
    config.tts_api_key = SecretStr("test")
    provider = Provider(db, config, runtime)
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "请听我说。"}}]})

    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    with TestClient(create_app(config, db, provider)) as client:
        _, _, player = launch(client)
        epoch = client.get(f"/api/voice/sessions/{player}/utterances").json()["epoch"]
        payload = {"audio": recording(), "request_id": "mic-1", "epoch": epoch}
        url = f"/api/speech-input/sessions/{player}"
        response = client.post(url, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["text"] == "请听我说。"
        assert client.post(url, json=payload).status_code == 409
        assert len([call for call in calls if call.url.host == "dashscope.aliyuncs.com"]) == 1
        with db.session() as s:
            task = s.scalar(select(Task).where(Task.kind == "asr"))
            assert task.payload == {} and task.result == {"audio_seconds": 1}
            usage = s.scalar(select(Usage).where(Usage.role == "asr"))
            assert usage.cost > 0 and usage.reserved == 0
        runtime.submit(player, config.workspace_owner, "换个问题", "next")
        assert client.post(url, json={**payload, "request_id": "mic-2"}).status_code == 409
        assert len([call for call in calls if call.url.host == "dashscope.aliyuncs.com"]) == 1
