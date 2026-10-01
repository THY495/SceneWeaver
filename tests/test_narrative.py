from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from chatbot.api import create_app
from chatbot.experiment_templates import template
from chatbot.narrative import Narrative
from chatbot.observations import visible_records
from chatbot.storage import SceneRecord, StoryEvent
from chatbot.story_blueprints import Recipe


def graph():
    return {
        "entry": "start",
        "nodes": [
            {"id": key, "title": key, "text": text, "observers": ["player"]}
            for key, text in [
                ("start", "暴雨将至。"),
                ("left", "发现红信。"),
                ("right", "发现蓝信。"),
                ("end", "渡船已经抵达。"),
            ]
        ],
        "edges": [
            {"id": a + b, "source": a, "target": b, "label": b}
            for a, b in [("start", "left"), ("start", "right"), ("left", "end"), ("right", "end")]
        ],
    }


def test_graph_validation():
    assert Narrative.model_validate(graph()).entry == "start"
    assert Narrative.model_validate(graph()).nodes[0].background is None
    invalid_asset = graph()
    invalid_asset["nodes"][0]["background"] = "../private.png"
    with pytest.raises(ValidationError):
        Narrative.model_validate(invalid_asset)
    bad = graph()
    bad["edges"].append({"id": "cycle", "source": "end", "target": "start", "label": "循环"})
    with pytest.raises(ValidationError, match="循环"):
        Narrative.model_validate(bad)
    bad = graph()
    bad["edges"] = []
    with pytest.raises(ValidationError, match="到达"):
        Narrative.model_validate(bad)
    bad = graph()
    bad["nodes"][0]["entry_locations"] = [
        {"actor": "player", "location": "大厅"},
        {"actor": "player", "location": "码头"},
    ]
    with pytest.raises(ValidationError, match="重复设置"):
        Narrative.model_validate(bad)
    bad = template("partial-hearing")
    bad["narrative"] = graph()
    bad["narrative"]["nodes"][0]["entry_locations"] = [{"actor": "unknown", "location": "大厅"}]
    with pytest.raises(ValidationError, match="入场地点只能引用"):
        Recipe.model_validate(bad)


def test_branch_commits_are_private_frozen_idempotent_and_ordered(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        base = "/api/role/experiments"
        recipe = template("partial-hearing")
        recipe["narrative"] = graph()
        recipe["narrative"]["nodes"][0]["background"] = "a" * 64
        recipe["narrative"]["nodes"][1]["background"] = "b" * 64
        bid = str(uuid4())
        assert (
            client.put(
                base + "/blueprints/" + bid, json={"expected_version": 0, "recipe": recipe}
            ).status_code
            == 200
        )
        run = client.post(
            base + "/start", json={"request_id": "graph", "blueprint_id": bid, "blueprint_version": 1}
        ).json()
        sid = run["story_id"]
        path = base + f"/stories/{sid}/narrative"
        view = client.get(path).json()
        assert view["title"] == "start"
        assert view["background"] == "a" * 64
        assert "b" * 64 not in str(view)
        assert "发现红信" not in str(view) and "target" not in str(view)
        choose = {"request_id": "pick", "edge_id": "startleft", "expected_revision": 1}
        assert client.post(path + "/choose", json=choose).status_code == 409
        settings = client.get(f"/api/role/stories/{sid}/settings").json()
        client.put(f"/api/role/stories/{sid}/settings", json={**settings, "paused": False})
        reply = client.post(path + "/choose", json=choose)
        assert reply.status_code == 200, reply.text
        assert reply.json()["node_id"] == "left"
        assert reply.json()["background"] == "b" * 64
        assert client.post(path + "/choose", json=choose).json() == reply.json()
        assert client.post(path + "/choose", json={**choose, "edge_id": "startright"}).status_code == 409
        assert client.post(path + "/choose", json={**choose, "request_id": "stale"}).status_code == 409
        assert (
            client.post(
                path + "/choose", json={"request_id": "end", "expected_revision": 2, "edge_id": "leftend"}
            ).json()["node_id"]
            == "end"
        )
        with db.session() as s:
            history = " ".join(r.text for r in visible_records(s, run["player_id"]))
            assert "发现红信" in history and "渡船已经抵达" in history and "发现蓝信" not in history
            npc = " ".join(r.text for r in visible_records(s, run["actors"]["curator"]))
            assert "暴雨将至" not in npc and "发现红信" not in npc
            records = list(s.scalars(select(SceneRecord).where(SceneRecord.story_id == sid)))
            assert len([r for r in records if r.payload.get("narrative_node")]) == 3
            assert len(list(s.scalars(select(StoryEvent).where(StoryEvent.story_id == sid)))) == 2
