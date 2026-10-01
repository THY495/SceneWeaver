import base64
import io
import wave
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from chatbot.api import create_app
from chatbot.experiment_templates import template
from chatbot.voice_references import restore_catalog


def sample():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\x00\x00" * 24000 * 3)
    return base64.b64encode(output.getvalue()).decode()


def test_local_reference_rights_registration_and_account_scope(db, config, fake):
    config.product = "roleplay"
    config.tts_provider = "qwen_audio"
    config.tts_adapter_model = "qwen-audio-3.0-tts-flash"
    config.tts_endpoint = "shared"
    config.tts_enabled = True
    config.tts_adapter_api_key = SecretStr("synthetic-key")
    config.tts_voices = {"longanfengyue": "test"}
    app = create_app(config, db, fake)
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(200, json={"output": {"voice_id": "qwen-audio-3.0-tts-flash-test-id"}})

    with TestClient(app) as client:
        old = app.state.voice.http
        client.portal.call(old.aclose)
        app.state.voice.http = httpx.AsyncClient(transport=httpx.MockTransport(response))
        pid = str(uuid4())
        assert (
            client.put(
                f"/api/role/projects/{pid}",
                json={"expected_version": 0, "recipe": template("ferry-narrative")},
            ).status_code
            == 200
        )
        url = f"/api/role/projects/{pid}/voice-references"
        body = {"name": "test", "character": "test", "transcript": "test", "wav_base64": sample()}
        assert client.post(url, json=body).status_code == 422
        asset = client.post(url, json={**body, "rights_confirmed": True})
        assert asset.status_code == 201
        assert not calls
        endpoint = url + "/" + asset.json()["id"] + "/enroll"
        payload = {"audio_url": "https://example.com/synthetic.wav"}
        assert client.post(endpoint, json=payload).status_code == 422
        payload.update(consent_aliyun=True, same_audio_confirmed=True)
        assert client.post(endpoint, json=payload).status_code == 200
        assert client.post(endpoint, json=payload).json()["reused"] is True
        assert len(calls) == 1
        assert "synthetic-key" not in client.get(url).text
        voice = "qwen-audio-3.0-tts-flash-test-id"
        config.tts_voices = {}
        restore_catalog(config)
        assert voice in config.tts_voices
        config.tts_adapter_api_key = SecretStr("another-account")
        config.tts_voices = {}
        restore_catalog(config)
        assert voice not in config.tts_voices
        assert client.get(url).json()[0]["voice_id"] is None
        config.tts_voices = {"longanfengyue": "test"}

        # An ambiguous provider failure must not trigger duplicate paid enrollment.
        def timeout(request):
            calls.append(request)
            raise httpx.ReadTimeout("simulated", request=request)

        client.portal.call(app.state.voice.http.aclose)
        app.state.voice.http = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        assert client.post(endpoint, json=payload).status_code == 409
        count = len(calls)
        assert client.post(endpoint, json=payload).status_code == 409
        assert len(calls) == count
        assert client.get(url).json()[0]["enrollment_status"] == "unknown"
