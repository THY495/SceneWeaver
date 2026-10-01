import os

import pytest
from pydantic import SecretStr

from chatbot.player_credentials import PlayerCredentials, apply, load, save


@pytest.mark.skipif(os.name != "nt", reason="Uses real Windows DPAPI")
def test_windows_vault_roundtrip_and_corruption_preserves_file(tmp_path):
    path = tmp_path / "player.dpapi"
    value = PlayerCredentials(llm_key="synthetic-test-secret", tts_key="synthetic-voice-secret")
    save(path, value)
    assert b"synthetic-test-secret" not in path.read_bytes()
    assert load(path).llm_key.get_secret_value() == "synthetic-test-secret"
    path.write_bytes(b"damaged-vault")
    with pytest.raises(RuntimeError):
        load(path)
    assert path.read_bytes() == b"damaged-vault"


def test_apply_and_clear_keys(config):
    config.api_key_file = "must-not-be-used"
    apply(config, PlayerCredentials(llm_key=SecretStr("synthetic"), tts_key=SecretStr("tts")))
    assert config.api_key_file == "" and config.tts_enabled
    assert config.tts_endpoint == "shared" and config.tts_region == "ap-southeast-1"
    apply(config, PlayerCredentials())
    assert not config.generation_key() and not config.tts_enabled


@pytest.mark.skipif(os.name != "nt", reason="Windows host only")
def test_native_endpoint_authenticated_and_no_secret_echo(config, db, fake, tmp_path):
    from fastapi.testclient import TestClient

    from chatbot.api import create_app
    from chatbot.player_credentials import install

    config.product = "roleplay"
    config.access_token = SecretStr("host-test-token")
    app = create_app(config, db, fake)
    install(app, config, db, tmp_path / "credentials.dpapi")
    with TestClient(app) as client:
        assert client.get("/api/player/credentials").status_code == 401
        headers = {"Authorization": "Bearer host-test-token"}
        response = client.put(
            "/api/player/credentials", headers=headers, json={"llm_key": "synthetic-secret"}
        )
        assert response.status_code == 200
        assert "synthetic-secret" not in client.get("/api/player/credentials", headers=headers).text
        response = client.patch(
            "/api/player/credentials",
            headers=headers,
            json={"tts_key": "synthetic-tts", "daily_budget_usd": 1.5},
        )
        assert response.status_code == 200
        value = load(tmp_path / "credentials.dpapi")
        assert value.llm_key.get_secret_value() == "synthetic-secret"
        assert value.tts_key.get_secret_value() == "synthetic-tts"
        assert value.daily_budget_usd == 1.5
        response = client.patch("/api/player/credentials", headers=headers, json={"tts_voice": "other-voice"})
        assert response.status_code == 200
        assert config.api_key.get_secret_value() == "synthetic-secret"
        assert config.tts_adapter_api_key.get_secret_value() == "synthetic-tts"
        assert client.patch("/api/player/credentials", json={"llm_key": ""}).status_code == 401
        assert (
            client.patch("/api/player/credentials", headers=headers, json={"tts_key": ""}).status_code == 200
        )
        assert not config.tts_enabled and config.api_key.get_secret_value() == "synthetic-secret"
        from chatbot.storage import Story, now

        with db.session.begin() as s:
            story = Story(owner_id=config.workspace_owner, title="结束存档", settings={"paused": False})
            s.add(story)
            s.flush()
            story_id = story.id
        assert client.put("/api/player/credentials", headers=headers, json={}).status_code == 409
        with db.session.begin() as s:
            story = s.get(Story, story_id)
            story.settings = {"paused": False, "narrative": {"finished_at": now().isoformat()}}
        assert client.put("/api/player/credentials", headers=headers, json={}).status_code == 200
