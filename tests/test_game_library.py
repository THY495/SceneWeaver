import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.game_library import create_game_library


@pytest.mark.parametrize("deep", [False, True])
def test_library_new_games_have_independent_routes_and_survive_host_restart(db, config, fake, tmp_path, deep):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake, serve_web=False)) as studio:
        source = studio.post(
            "/api/role/experiments/start",
            json={
                "request_id": "release-seed",
                "template_id": "partial-hearing",
            },
        ).json()
    web = tmp_path / "web"
    if deep:
        web = web / ("nested-project-" * 8) / ("release-content-" * 8)
        assert len(str(web / "assets/game.js")) > 260
    from chatbot.native_paths import filesystem_path

    storage = filesystem_path(web)
    storage.mkdir(parents=True)
    (storage / "game.html").write_text("Game", encoding="utf-8")
    (storage / "index.html").write_text("Studio", encoding="utf-8")
    (storage / "assets").mkdir()
    (storage / "assets/game.js").write_text("console.log('loaded')", encoding="utf-8")
    with TestClient(create_app(config, db, fake, web_dir=web)) as studio:
        assert studio.get("/assets/game.js").text == "console.log('loaded')"

    def build():
        return create_game_library(config, db, fake, source_story_id=source["story_id"], game_web_dir=web)

    app = build()

    async def ready():
        pass

    app.state.runtime.model_readiness.require = ready
    with TestClient(app) as client:
        asset = client.get("/assets/game.js")
        assert asset.status_code == 200
        assert asset.text == "console.log('loaded')"
        assert client.get("/api/game/library").json()["saves"] == []
        one = client.post("/api/game/saves", json={"request_id": "one", "label": "First"})
        assert one.status_code == 201, one.text
        two = client.post("/api/game/saves", json={"request_id": "two", "label": "Second"}).json()
        first, second = one.json()["id"], two["id"]
        first_path, second_path = f"/runs/{first}", f"/runs/{second}"
        assert client.get(first_path + "/").text == "Game"
        assert client.get(first_path + "/assets/game.js").text == "console.log('loaded')"
        assert client.get(first_path + "/api/game/session").json()["id"] == first
        assert client.get(second_path + "/api/game/session").json()["id"] == second
        # Reading the second save never changes the first save's binding.
        assert client.get(first_path + "/api/game/session").json()["id"] == first
        assert client.get(first_path + f"/api/role/players/{second}/snapshot").status_code == 404
        assert client.get(f"/runs/{source['player_id']}/api/game/session").status_code == 404
        assert client.get(first_path + "/api/debug/overview").status_code == 404
        assert client.get("/api/role/experiments/blueprints").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/api/player/credentials").status_code == 404
        assert client.get(first_path + "/api/player/credentials").status_code == 404
    with TestClient(build()) as restarted:
        assert {save["id"] for save in restarted.get("/api/game/library").json()["saves"]} == {first, second}
        assert restarted.get(first_path + "/api/game/session").json()["id"] == first

    # Native installation is explicit, authenticated and shared across save paths.
    import os

    if os.name == "nt":
        from pydantic import SecretStr

        config.access_token = SecretStr("synthetic-native-host-token")
        native = create_game_library(
            config,
            db,
            fake,
            source_story_id=source["story_id"],
            game_web_dir=web,
            player_credentials_path=tmp_path / "player-credentials.dpapi",
        )
        headers = {"Authorization": "Bearer synthetic-native-host-token"}
        with TestClient(native) as client:
            for prefix in ("", first_path, second_path):
                assert client.get(prefix + "/api/player/credentials").status_code == 401
                response = client.get(prefix + "/api/player/credentials", headers=headers)
                assert response.status_code == 200
                assert response.json()["available"] is True
                assert "llm_key" not in response.json() and "tts_key" not in response.json()
            assert client.get(first_path + "/api/debug/overview", headers=headers).status_code == 404
            # A new game is already running; credential changes require all saves to pause.
            for path in (first_path, second_path):
                paused = client.put(path + "/api/game/pause", headers=headers, json={"paused": True})
                assert paused.status_code == 200, paused.text
            saved = client.put(
                first_path + "/api/player/credentials",
                headers=headers,
                json={"llm_key": "synthetic-game-llm", "tts_key": "synthetic-game-tts"},
            )
            assert saved.status_code == 200, saved.text
            for prefix in ("", second_path):
                state = client.get(prefix + "/api/player/credentials", headers=headers).json()
                assert state["llm_configured"] and state["tts_configured"]
            assert b"synthetic-game-llm" not in (tmp_path / "player-credentials.dpapi").read_bytes()
