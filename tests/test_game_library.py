import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.game_library import create_game_library


@pytest.mark.parametrize("fixed_opening", [False, True], ids=["scene-text", "fixed-closing"])
def test_ending_save_completion_timing_and_library_reopen(db, config, fake, tmp_path, fixed_opening):
    import asyncio
    import json

    from chatbot.experiment_templates import template
    from chatbot.game_seed import load_game_seed

    config.product = "roleplay"
    recipe = template("partial-hearing")
    node = dict(id="end", title="归航", ending=True, observers=["player"])
    if fixed_opening:
        node["opening"] = [dict(id="last", kind="narration", text="渡船驶离码头。故事结束。")]
    else:
        node["text"] = "渡船驶离码头。故事结束。"
    recipe["narrative"] = dict(entry="end", nodes=[node], edges=[])
    package = tmp_path / "ending.story.json"
    package.write_text(
        json.dumps(dict(format="sceneweaver.story.v3", recipe=recipe, assets={}, documents={})), "utf-8"
    )
    source = asyncio.run(load_game_seed(db, config, package))
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "game.html").write_text("Game", "utf-8")

    def build():
        return create_game_library(config, db, fake, source_story_id=source, game_web_dir=web)

    with TestClient(build()) as client:
        assert client.post("/api/runtime/check").status_code == 200
        created = client.post("/api/game/saves", json={"request_id": "ending", "label": "归航"})
        assert created.status_code == 201, created.text
        player = created.json()["id"]
        prefix = f"/runs/{player}"
        snapshot_path = prefix + f"/api/role/players/{player}/snapshot"
        before = client.get(snapshot_path).json()
        assert created.json()["finished"] is (not fixed_opening)
        if fixed_opening:
            assert before["frame"]["execution"]["phase"] == "opening"
            assert before["frame"]["finished_at"] is None
            assert client.get("/api/game/library").json()["saves"][0]["finished"] is False
            assert client.put(prefix + "/api/game/pause", json={"paused": False}).status_code == 200
            path = prefix + f"/api/game/stories/{before['story_id']}/narrative/advance"
            body = dict(request_id="finish", node_id="end", expected_version=1)
            response = client.post(path, json=body)
            assert response.status_code == 200, response.text
            assert response.json()["execution"]["phase"] == "finished"
            assert client.post(path, json=body).json() == response.json()
        else:
            assert before["frame"]["finished_at"]
            assert "execution" not in before["frame"]
        finished = client.get(snapshot_path).json()
        assert finished["frame"]["finished_at"]
        assert not finished["interaction"]["allowed"]
        catalogue = client.get("/api/game/library").json()
        assert catalogue["saves"][0]["id"] == player and catalogue["saves"][0]["finished"] is True
        assert (
            client.post(
                prefix + f"/api/sessions/{player}/messages",
                json={"content": "继续", "client_message_id": "after-ending"},
            ).status_code
            == 409
        )
        assert client.get("/api/role/projects").status_code == 404
    # Recreating the library proves persisted state, not a Windows process restart.
    with TestClient(build()) as reopened:
        assert reopened.get("/api/game/library").json() == catalogue
        assert reopened.get(snapshot_path).json()["frame"]["finished_at"] == finished["frame"]["finished_at"]
    assert fake.calls == []  # Only the local readiness fixture was consulted.


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
