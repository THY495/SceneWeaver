import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.game_api import create_game_app


def test_game_web_mount_never_falls_back_to_studio(db, config, fake, tmp_path):
    config.product = "roleplay"
    studio = tmp_path / "studio"
    studio.mkdir()
    (studio / "index.html").write_text("Studio secret editor", encoding="utf-8")
    (studio / "assets").mkdir()
    with pytest.raises(ValueError, match="independent game build"):
        create_game_app(config, db, fake, player_id="bound", game_web_dir=studio)
    game = tmp_path / "game"
    game.mkdir()
    (game / "game.html").write_text("A standalone story", encoding="utf-8")
    (game / "assets").mkdir()
    (game / "assets" / "game.js").write_text("/* player */", encoding="utf-8")
    with TestClient(create_game_app(config, db, fake, player_id="bound", game_web_dir=game)) as client:
        assert client.get("/").text == "A standalone story"
        assert client.get("/assets/game.js").status_code == 200
        assert client.get("/studio").status_code == 404
        assert client.get("/index.html").status_code == 404
        assert client.get("/api/projects").status_code == 404


def test_game_surface_is_bound_to_player_and_excludes_studio(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake, serve_web=False)) as studio:
        first = studio.post(
            "/api/role/experiments/start",
            json={
                "request_id": "game-one",
                "template_id": "partial-hearing",
            },
        ).json()
        second = studio.post(
            "/api/role/experiments/start",
            json={
                "request_id": "game-two",
                "template_id": "partial-hearing",
            },
        ).json()
    player = first["player_id"]
    from chatbot.observations import append_record
    from chatbot.storage import Conversation, VoicePlayback

    with db.session.begin() as s:
        foreign = s.get(Conversation, second["player_id"])
        event = append_record(
            s,
            story_id=foreign.story_id,
            kind="speech",
            actor=foreign,
            text="Other save's private speech",
            observer_ids=[foreign.id],
            dedupe="foreign-voice",
        )
        playback = VoicePlayback(
            session_id=foreign.id,
            event_id=event.id,
            request_id="foreign-voice",
            chunk_index=0,
            epoch=foreign.epoch,
            text_hash="a" * 64,
            voice="test",
        )
        s.add(playback)
        s.flush()
        foreign_pid = playback.id
    game = create_game_app(config, db, fake, player_id=player)
    with TestClient(game) as client:
        snapshot = client.get(f"/api/role/players/{player}/snapshot")
        assert snapshot.status_code == 200, snapshot.text
        assert client.get(f"/api/role/players/{player}/reading").status_code == 200
        for other in (second["player_id"], first["actors"]["reporter"]):
            assert client.get(f"/api/role/players/{other}/snapshot").status_code == 404
            assert client.get(f"/api/role/items/actors/{other}").status_code == 404
            assert client.get(f"/api/voice/sessions/{other}/utterances").status_code == 404
            assert (
                client.post(
                    f"/api/sessions/{other}/messages",
                    json={
                        "content": "Do not run a model",
                        "client_message_id": "forbidden",
                    },
                ).status_code
                == 404
            )
        for path in (
            "/",
            "/docs",
            "/openapi.json",
            "/api/config",
            "/api/sessions",
            f"/api/sessions/{player}",
            f"/api/role/presentation/stage/{player}",
            f"/api/role/actors/{first['actors']['reporter']}/memories",
            f"/api/role/stories/{first['story_id']}/timeline",
            f"/api/role/stories/{first['story_id']}/timing-overview",
            "/api/debug/overview",
            "/api/documents",
        ):
            assert client.get(path).status_code == 404, path
        assert client.post("/api/role/experiments/start", json={}).status_code == 404
        assert client.post("/api/role/presentation/sprites").status_code == 404
        assert client.post("/api/voice/playbacks/missing/start").status_code == 404
        assert client.get(f"/api/voice/playbacks/{foreign_pid}").status_code == 404
        for action in ("start", "audio", "receipt"):
            assert client.post(f"/api/voice/playbacks/{foreign_pid}/{action}", json={}).status_code == 404


def test_game_keeps_authentication_and_rechecks_bound_run(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake, serve_web=False)) as studio:
        run = studio.post(
            "/api/role/experiments/start",
            json={
                "request_id": "game-auth",
                "template_id": "partial-hearing",
            },
        ).json()
    from pydantic import SecretStr

    from chatbot.storage import Conversation

    config.access_token = SecretStr("test-local-token")
    with TestClient(create_game_app(config, db, fake, player_id=run["player_id"])) as client:
        url = f"/api/role/players/{run['player_id']}/snapshot"
        assert client.get(url).status_code == 401
        headers = {"Authorization": "Bearer test-local-token"}
        assert client.get(url, headers=headers).status_code == 200
        with db.session.begin() as s:
            s.get(Conversation, run["player_id"]).owner_id = "different-owner"
        assert client.get(url, headers=headers).status_code == 404


def test_game_controls_preserve_rules_and_do_not_accept_author_settings(db, config, fake):
    from datetime import timedelta
    from uuid import uuid4

    from chatbot.experiment_templates import template
    from chatbot.presentation import asset_dir
    from chatbot.storage import Conversation, Story, Task, now

    config.product = "roleplay"
    images = asset_dir(config, config.workspace_owner)
    images.mkdir(parents=True)
    for key in ("a" * 64, "b" * 64):
        (images / (key + ".png")).write_bytes(b"fixture-image")
    recipe = template("partial-hearing")
    recipe["narrative"] = {
        "entry": "end",
        "nodes": [
            {
                "id": "end",
                "title": "归航",
                "background": "a" * 64,
                "ending": True,
                "observers": ["player"],
                "opening": [{"id": "last", "kind": "narration", "text": "故事结束。"}],
            }
        ],
        "edges": [],
    }
    with TestClient(create_app(config, db, fake, serve_web=False)) as studio:
        bid = str(uuid4())
        saved = studio.put(
            f"/api/role/experiments/blueprints/{bid}",
            json={
                "expected_version": 0,
                "recipe": recipe,
            },
        )
        assert saved.status_code == 200, saved.text
        run = studio.post(
            "/api/role/experiments/start",
            json={
                "request_id": "game-finale",
                "blueprint_id": bid,
                "blueprint_version": 1,
            },
        ).json()
    game = create_game_app(config, db, fake, player_id=run["player_id"])

    async def ready():
        return None

    game.state.runtime.model_readiness.require = ready  # No real provider in this contract test.
    with TestClient(game) as client:
        assert client.get("/api/game/assets/" + "a" * 64).status_code == 200
        assert client.get("/api/game/assets/" + "b" * 64).status_code == 404
        assert client.get("/api/role/presentation/sprites/" + "b" * 64).status_code == 404
        info = client.get("/api/game/session").json()
        assert set(info) == {"id", "title", "story_id", "epoch", "tasks"}
        assert info["id"] == run["player_id"]
        with db.session.begin() as s:
            s.add(
                Task(
                    owner_id=config.workspace_owner,
                    session_id=run["player_id"],
                    kind="respond",
                    status="completed",
                    epoch=info["epoch"],
                    deadline=now() + timedelta(minutes=1),
                    result={"stop_reason": "safety_rejected", "internal": "hidden"},
                )
            )
        visible_task = client.get("/api/game/session").json()["tasks"][0]
        assert visible_task["status"] == "completed"
        assert visible_task["stop_reason"] == "safety_rejected"
        assert "internal" not in visible_task
        before_epoch = info["epoch"]
        assert client.put("/api/game/pause", json={"paused": False, "chain_limit": 99}).status_code == 422
        assert client.put("/api/game/pause", json={"paused": False}).status_code == 200
        new_epoch = client.get("/api/game/session").json()["epoch"]
        assert new_epoch == before_epoch + 1
        with db.session() as s:
            resume_grace = s.get(Conversation, run["player_id"]).current_state["resume_grace_until"]
        assert resume_grace > 0
        assert client.put("/api/game/pause", json={"paused": False}).status_code == 200
        assert client.get("/api/game/session").json()["epoch"] == new_epoch
        with db.session() as s:
            assert s.get(Conversation, run["player_id"]).current_state["resume_grace_until"] == resume_grace
        sid = run["story_id"]
        assert (
            client.post(
                "/api/game/stories/other/presence", json={"visible": True, "typing": False}
            ).status_code
            == 404
        )
        body = {"request_id": "finish", "node_id": "end", "expected_version": 1}
        path = f"/api/game/stories/{sid}/narrative/advance"
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["finished_at"]
        assert client.post(path, json=body).json() == response.json()
        assert client.put("/api/game/pause", json={"paused": False}).status_code == 409
        presence = client.post(f"/api/game/stories/{sid}/presence", json={"visible": True, "typing": False})
        assert presence.json()["reason"] == "finished"
        assert (
            client.post(f"/api/role/experiments/stories/{sid}/narrative/advance", json=body).status_code
            == 404
        )
        with db.session() as s:
            assert s.get(Story, sid).settings["narrative"]["finished_at"]
            assert s.get(Conversation, run["player_id"]).epoch == new_epoch
