from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.experiment_templates import template
from chatbot.game_api import create_game_app
from chatbot.video_assets import video_dir


@pytest.mark.parametrize("allow_skip, outcome", [(False, "ended"), (True, "skipped")])
def test_video_gates_actions_and_scoped_asset_then_commits_opening(db, config, fake, allow_skip, outcome):
    config.product = "roleplay"
    recipe = template("ferry-narrative")
    recipe["video_assets"] = [{"id": "intro", "name": "开场", "content_hash": "a" * 64, "bytes": 4}]
    node = recipe["narrative"]["nodes"][0]
    node.update(
        visual_mode="video",
        video={"asset": "intro", "allow_skip": allow_skip},
        opening=[{"id": "after", "kind": "narration", "text": "影片后的秘密结果。"}],
    )
    cache = video_dir(config, config.workspace_owner)
    cache.mkdir(parents=True)
    (cache / ("a" * 64 + ".mp4")).write_bytes(b"test")
    with TestClient(create_app(config, db, fake)) as client:
        bid = str(uuid4())
        saved = client.put(
            "/api/role/experiments/blueprints/" + bid, json={"expected_version": 0, "recipe": recipe}
        )
        assert saved.status_code == 200, saved.text
        response = client.post(
            "/api/role/experiments/start",
            json={"request_id": "video", "blueprint_id": bid, "blueprint_version": 1},
        )
        assert response.status_code == 201, response.text
        run = response.json()
        sid, player = run["story_id"], run["player_id"]
        base = f"/api/role/experiments/stories/{sid}/narrative"
        frame = client.get(base).json()
        assert frame["execution"]["phase"] == "video"
        assert frame["video"]["asset"] == "intro"
        assert "影片后的秘密结果" not in str(frame)
        snapshot = client.get(f"/api/role/players/{player}/snapshot").json()
        assert not snapshot["interaction"]["allowed"]
        assert "影片后的秘密结果" not in str(snapshot)
        url = f"/api/role/presentation/player-videos/{player}/{node['id']}"
        stream_url = url.replace("player-videos", "player-video-stream")
        paused_ticket = client.post(url + "/ticket").json()["ticket"]
        settings_path = f"/api/role/stories/{sid}/settings"
        settings = client.get(settings_path).json()
        assert client.put(settings_path, json={**settings, "paused": False}).status_code == 200
        assert client.get(stream_url, params={"ticket": paused_ticket}).status_code == 401
        assert (
            client.post(
                f"/api/sessions/{player}/messages", json={"content": "提前行动", "client_message_id": "early"}
            ).status_code
            == 409
        )
        assert client.get(url, headers={"Range": "bytes=0-1"}).content == b"te"
        assert client.get(url.replace(node["id"], "future-node")).status_code == 404
        from pydantic import SecretStr

        config.access_token = SecretStr("synthetic-private-token")
        assert client.post(url + "/ticket").status_code == 401
        issued = client.post(url + "/ticket", headers={"Authorization": "Bearer synthetic-private-token"})
        assert issued.status_code == 200
        capability = issued.json()["ticket"]
        assert "synthetic-private-token" not in capability
        assert (
            client.get(stream_url, params={"ticket": capability}, headers={"Range": "bytes=0-1"}).content
            == b"te"
        )
        assert client.get(stream_url, params={"ticket": capability + "x"}).status_code == 401
        assert client.get(stream_url, params={"ticket": "1." + "a" * 64}).status_code == 401
        config.access_token = SecretStr("")
        body = {"request_id": "watch", "node_id": node["id"], "expected_version": 1}
        assert client.post(base + "/advance", json=body).status_code == 409
        if not allow_skip:
            assert client.post(base + "/advance", json={**body, "video_result": "skipped"}).status_code == 409
        with TestClient(create_game_app(config, db, fake, player_id=player)) as game:
            assert game.get(url).status_code == 200
            assert (
                game.get(url.replace(player, run["actors"][recipe["people"][0]["alias"]])).status_code == 404
            )
        body["video_result"] = outcome
        result = client.post(base + "/advance", json=body)
        assert result.status_code == 200, result.text
        assert result.json()["execution"]["phase"] == "opening"
        assert result.json()["video"] is None
        assert "影片后的秘密结果" in str(result.json())
        assert client.post(base + "/advance", json=body).json() == result.json()
        assert client.get(url).status_code == 404
        assert client.get(stream_url, params={"ticket": capability}).status_code == 404
        assert client.post(base + "/advance", json={**body, "request_id": "stale"}).status_code == 409
