from uuid import uuid4

from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.experiment_templates import template
from chatbot.observations import append_record
from chatbot.player_presence import merge_presence
from chatbot.storage import Conversation, Story


def test_presence_expiry_hidden_pages_and_legacy():
    clients, value = merge_presence({}, "a", True, True, 100)
    clients, value = merge_presence(clients, "b", True, False, 101)
    assert value["visible"] and value["typing"]
    clients, value = merge_presence(clients, "b", False, False, 102)
    assert value["visible"] and value["typing"]
    assert value["until"] == 115, "hidden pages cannot prolong another page's visibility"
    clients, value = merge_presence(clients, "b", True, False, 116)
    assert value["visible"] and not value["typing"]
    assert "a" not in clients
    clients, value = merge_presence({}, None, True, False, 200)
    clients, value = merge_presence(clients, None, False, True, 201)
    assert not value["visible"] and not value["typing"]


def test_one_page_cannot_clear_another_pages_presence(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run = client.post(
            "/api/role/experiments/start",
            json={"request_id": "presence-pages", "template_id": "partial-hearing"},
        ).json()
        url = f"/api/role/stories/{run['story_id']}/presence"
        for body in [
            {"client_id": "reading", "visible": True, "typing": True},
            {"client_id": "other", "visible": True, "typing": False},
            {"client_id": "other", "visible": False, "typing": False},
        ]:
            assert client.post(url, json=body).json()["reason"] == "player_input"
        with db.session() as s:
            state = s.get(Conversation, run["player_id"]).current_state
            assert state["presence"]["visible"] and state["presence"]["typing"]
            assert s.get(Story, run["story_id"]).settings["activity_until"] > 0
        assert (
            client.post(url, json={"client_id": "reading", "visible": False, "typing": False}).json()[
                "reason"
            ]
            == "hidden"
        )


def test_idle_npc_does_not_answer_authored_question_before_player(db, config, fake):
    config.product = "roleplay"
    recipe = template("partial-hearing")
    recipe["narrative"] = {
        "entry": "hall",
        "nodes": [{
            "id": "hall",
            "title": "门厅",
            "observers": ["player", "reporter"],
            "opening": [
                {"id": "rain", "kind": "narration", "text": "下雨了。"},
                {"id": "ask", "kind": "dialogue", "speaker": "reporter", "text": "你愿意一起核对吗？我还不确定。"},
            ],
        }],
        "edges": [],
    }
    with TestClient(create_app(config, db, fake)) as client:
        prefix = "/api/role/experiments"
        blueprint = str(uuid4())
        saved = client.put(
            f"{prefix}/blueprints/{blueprint}",
            json={"expected_version": 0, "recipe": recipe},
        )
        assert saved.status_code == 200, saved.text
        started = client.post(
            f"{prefix}/start",
            json={"request_id": "question-wait", "blueprint_id": blueprint, "blueprint_version": 1},
        )
        assert started.status_code == 201, started.text
        run = started.json()
        path = f"{prefix}/stories/{run['story_id']}/narrative/advance"
        settings_path = f"/api/role/stories/{run['story_id']}/settings"
        settings = client.get(settings_path).json()
        assert client.put(settings_path, json={**settings, "paused": False}).status_code == 200
        for version in (1, 2):
            advanced = client.post(
                path,
                json={"request_id": f"question-{version}", "node_id": "hall", "expected_version": version},
            )
            assert advanced.status_code == 200, advanced.text
        with db.session.begin() as s:
            player = s.get(Conversation, run["player_id"])
            player.current_state = {
                **player.current_state,
                "last_input_at": 0,
                "resume_grace_until": 0,
            }
        presence = client.post(
            f"/api/role/stories/{run['story_id']}/presence",
            json={"visible": True, "typing": False},
        )
        assert presence.status_code == 200, presence.text
        assert presence.json() == {"task_id": None, "reason": "opening_question"}
        with db.session.begin() as s:
            player = s.get(Conversation, run["player_id"])
            npc = s.get(Conversation, run["actors"]["reporter"])
            append_record(
                s,
                story_id=run["story_id"],
                kind="speech",
                actor=player,
                text="我愿意一起核对。",
                observer_ids=[player.id, npc.id],
                dedupe="answer-opening-question",
            )
            append_record(
                s,
                story_id=run["story_id"],
                kind="speech",
                actor=npc,
                text="从哪一页开始？",
                observer_ids=[npc.id, player.id],
                dedupe="fresh-player-question",
                payload={"exchange": {"expects_reply": True, "target_id": player.id}},
            )
        pending = client.post(
            f"/api/role/stories/{run['story_id']}/presence",
            json={"visible": True, "typing": False},
        )
        assert pending.status_code == 200, pending.text
        assert pending.json() == {"task_id": None, "reason": "pending_player_reply"}
