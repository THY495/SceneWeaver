from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.storage import Conversation


def test_reading_survives_new_app_and_does_not_change_story_state(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run = client.post(
            "/api/role/experiments/start",
            json={"request_id": "restart-reading", "template_id": "partial-hearing"},
        ).json()
        url = f"/api/role/players/{run['player_id']}/reading"
        with db.session() as s:
            player = s.get(Conversation, run["player_id"])
            before = (player.epoch, player.seq, player.revision, player.current_state.copy())
        progress = {
            "version": 1,
            "entry": "entry",
            "selected": "reply:0",
            "acknowledged": False,
            "fingerprint": "abc",
        }
        assert (
            client.put(
                url, json={"epoch": before[0], "expected_revision": 0, "progress": progress}
            ).status_code
            == 200
        )
    # New application and HTTP client, no localStorage, cookies or reused response.
    with TestClient(create_app(config, db, fake)) as restarted:
        assert restarted.get(url).json()["progress"] == progress
        with db.session() as s:
            player = s.get(Conversation, run["player_id"])
            assert (player.epoch, player.seq, player.revision, player.current_state) == before


def test_reading_persistence_conflict_epoch_and_scope(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run = client.post(
            "/api/role/experiments/start", json={"request_id": "reading", "template_id": "partial-hearing"}
        ).json()
        url = f"/api/role/players/{run['player_id']}/reading"
        initial = client.get(url).json()
        assert initial["progress"] is None
        body = {
            "epoch": initial["epoch"],
            "expected_revision": 0,
            "progress": {
                "version": 1,
                "entry": "entry",
                "selected": "reply:0",
                "acknowledged": True,
                "fingerprint": "abc123",
            },
        }
        assert client.put(url, json=body).json() == {"revision": 1}
        assert client.put(url, json=body).json() == {"revision": 1}
        assert client.get(url).json()["progress"] == body["progress"]
        with db.session.begin() as s:
            s.get(Conversation, run["player_id"]).epoch += 1
        assert client.get(url).json()["progress"] == body["progress"]
        body["progress"]["selected"] = "reply:1"
        assert client.put(url, json=body).status_code == 409
        body["expected_revision"] = 1
        assert client.put(url, json=body).json() == {"revision": 2}
        with db.session.begin() as s:
            player = s.get(Conversation, run["player_id"])
            player.privacy_version += 1
        assert client.get(url).json()["progress"] is None
        assert client.put(url, json=body).status_code == 409
        npc_url = f"/api/role/players/{run['actors']['reporter']}/reading"
        assert client.get(npc_url).status_code == 404
        assert client.put(npc_url, json=body).status_code == 404
        with db.session.begin() as s:
            s.get(Conversation, run["player_id"]).owner_id = "other"
        assert client.get(url).status_code == 404
        assert client.put(url, json=body).status_code == 404
