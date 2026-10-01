from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from chatbot.api import create_app
from chatbot.experiment_templates import template
from chatbot.observations import current_knowledge, visible_records
from chatbot.storage import Conversation, SceneRecord, Story, StoryBlueprint, Task


def test_blueprint_revision_frozen_start_and_private_knowledge(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        base = "/api/role/experiments"
        recipe = template("partial-hearing")
        recipe["knowledge"] = [
            {"kind": "fact", "text": "秘密编号是蓝鹭", "observers": ["curator"]},
            {"kind": "belief", "text": "周澈猜测馆长明天离开", "observers": ["restorer"]},
        ]
        bid = str(uuid4())
        body = {"expected_version": 0, "recipe": recipe}
        saved = client.put(base + "/blueprints/" + bid, json=body)
        assert saved.status_code == 200, saved.text
        assert client.put(base + "/blueprints/" + bid, json=body).json() == saved.json()
        start = {"request_id": "launch", "blueprint_id": bid, "blueprint_version": 1}
        response = client.post(base + "/start", json=start)
        assert response.status_code == 201, response.text
        run = response.json()
        assert client.post(base + "/start", json=start).json() == run
        grouped = client.get(base + "/runs", params={"blueprint_id": bid}).json()["runs"]
        assert [r["id"] for r in grouped] == [run["story_id"]]
        assert client.get(base + "/runs", params={"blueprint_id": str(uuid4())}).json()["runs"] == []
        frame = client.get(f"/api/role/stories/{run['story_id']}/observation-frame").json()
        joining = {
            "request_id": "arrival",
            "expected_seq": frame["story_seq"],
            "profile": {"name": "新访客", "core": "第一次来到档案馆。"},
            "location": "阅览室",
            "observer_ids": [run["actors"]["curator"]],
        }
        path = base + f"/stories/{run['story_id']}/characters"
        arrival = client.post(path, json=joining)
        assert arrival.status_code == 201, arrival.text
        assert client.post(path, json=joining).json() == arrival.json()
        assert client.post(path, json={**joining, "request_id": "stale-join"}).status_code == 409
        with db.session() as s:
            new_actor = s.get(Conversation, arrival.json()["actor_id"])
            assert "蓝鹭" not in str(current_knowledge(new_actor, visible_records(s, new_actor.id)))
            listener = s.get(Conversation, run["actors"]["restorer"])
            assert new_actor.id in listener.current_state["unnoticed_ids"]
        with db.session() as s:
            curator = s.get(Conversation, run["actors"]["curator"])
            listener = s.get(Conversation, run["actors"]["restorer"])
            facts, _ = current_knowledge(curator, visible_records(s, curator.id))
            other_facts, claims = current_knowledge(listener, visible_records(s, listener.id))
            assert "蓝鹭" in str(facts)
            assert "蓝鹭" not in str(other_facts) + str(claims)
            assert "明天离开" in str(claims) and "明天离开" not in str(other_facts)
            assert not list(s.scalars(select(Task).where(Task.kind != "connection_check")))
        recipe["title"] = "更新后的设定"
        assert (
            client.put(
                base + "/blueprints/" + bid, json={"expected_version": 1, "recipe": recipe}
            ).status_code
            == 200
        )
        assert client.put(base + "/blueprints/" + bid, json=body).status_code == 409
        assert client.post(base + "/start", json={**start, "request_id": "stale"}).status_code == 409
        assert (
            client.get(base + f"/stories/{run['story_id']}/initial").json()["recipe"]["title"]
            != recipe["title"]
        )
        with db.session.begin() as s:
            s.get(StoryBlueprint, bid).owner_id = "other"
        assert client.get(base + "/blueprints").json() == []
        assert client.post(base + "/start", json={**start, "request_id": "foreign"}).status_code == 404


def test_event_notes_idempotency_source_change_and_privacy(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        base = "/api/role/experiments"
        run = client.post(
            base + "/start", json={"request_id": "notes", "template_id": "partial-hearing"}
        ).json()
        record = client.get(base + f"/runs/{run['story_id']}/record").json()
        eid = record["events"][-1]["id"]
        path = base + f"/runs/{run['story_id']}/events/{eid}/notes"
        initial = client.get(path).json()
        body = {
            "request_id": "note1",
            "source_hash": initial["source_hash"],
            "category": "good",
            "note": "听闻范围符合设定",
        }
        saved = client.post(path, json=body)
        assert saved.status_code == 201, saved.text
        assert client.post(path, json=body).json() == saved.json()
        assert len(client.get(path).json()["notes"]) == 1
        assert client.post(path, json={**body, "note": "不同内容"}).status_code == 409
        with db.session.begin() as s:
            s.get(SceneRecord, eid).text = "更正后的文本"
        assert client.get(path).json()["notes"] == []
        assert client.post(path, json={**body, "request_id": "stale"}).status_code == 409
        with db.session.begin() as s:
            s.get(SceneRecord, eid).redacted = True
        assert client.get(path).status_code == 404
        with db.session.begin() as s:
            s.get(SceneRecord, eid).redacted = False
            s.get(Story, run["story_id"]).owner_id = "other"
        assert client.get(path).status_code == 404


def test_blueprint_rejects_invalid_references(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        recipe = template("partial-hearing")
        recipe["speech"][0]["heard"][0]["text"] = "原话没有的秘密"
        assert (
            client.post(
                "/api/role/experiments/validate", json={"expected_version": 0, "recipe": recipe}
            ).status_code
            == 422
        )
        recipe = template("partial-hearing")
        recipe["knowledge"] = [{"text": "信息", "observers": ["missing"]}]
        assert (
            client.post(
                "/api/role/experiments/validate", json={"expected_version": 0, "recipe": recipe}
            ).status_code
            == 422
        )
        recipe = template("reply-lifecycle")
        recipe["speech"][0]["heard"][0]["text"] = "沈砚"
        assert (
            client.post(
                "/api/role/experiments/validate", json={"expected_version": 0, "recipe": recipe}
            ).status_code
            == 422
        )
