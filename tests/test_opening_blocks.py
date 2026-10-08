from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from chatbot.api import create_app
from chatbot.experiment_templates import template
from chatbot.narrative import Node
from chatbot.observations import visible_records
from chatbot.provenance import source_class
from chatbot.storage import SceneRecord


def node():
    return {
        "id": "opening",
        "title": "候船亭",
        "observers": ["player", "curator"],
        "opening": [
            {"id": "rain", "kind": "narration", "text": "雨敲打着窗户。"},
            {
                "id": "claim",
                "kind": "dialogue",
                "speaker": "curator",
                "target": "player",
                "text": "我已经把信交给船长了。",
            },
        ],
    }


def test_opening_validation():
    assert Node.model_validate(node()).text == ""
    bad = node()
    bad["opening"][1]["speaker"] = None
    with pytest.raises(ValidationError, match="固定对白"):
        Node.model_validate(bad)
    bad = node()
    bad["opening"][1]["id"] = "rain"
    with pytest.raises(ValidationError, match="不能重复"):
        Node.model_validate(bad)
    bad = node()
    bad["opening"][0]["target"] = "player"
    with pytest.raises(ValidationError, match="旁白不能指定对白接收者"):
        Node.model_validate(bad)
    bad = node()
    bad["opening"][1]["target"] = "curator"
    with pytest.raises(ValidationError, match="不能把说话者设为接收者"):
        Node.model_validate(bad)


def test_fixed_speech_is_ordered_scoped_and_not_fact(db, config, fake, runtime):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        base = "/api/role/experiments"
        recipe = template("partial-hearing")
        recipe["narrative"] = {"entry": "opening", "nodes": [node()], "edges": []}
        bid = str(uuid4())
        saved = client.put(base + "/blueprints/" + bid, json={"expected_version": 0, "recipe": recipe})
        assert saved.status_code == 200, saved.text
        body = {"request_id": "blocks", "blueprint_id": bid, "blueprint_version": 1}
        started = client.post(base + "/start", json=body)
        assert started.status_code == 201, started.text
        run = started.json()
        assert client.post(base + "/start", json=body).json() == run
        path = base + f"/stories/{run['story_id']}/narrative"
        first = client.get(path).json()
        assert first["execution"]["phase"] == "opening"
        assert "我已经把信" not in str(first)
        with db.session() as s:
            visible = " ".join(r.text for r in visible_records(s, run["actors"]["curator"]))
            assert "我已经把信交给船长了" not in visible
        advance = {"request_id": "second", "node_id": "opening", "expected_version": 1}
        assert client.post(path + "/advance", json=advance).status_code == 409
        assert (
            client.post(
                f"/api/role/stories/{run['story_id']}/steps",
                json={"request_id": "blocked-step", "actor_id": run["actors"]["curator"]},
            ).status_code
            == 409
        )
        settings_path = f"/api/role/stories/{run['story_id']}/settings"
        settings = client.get(settings_path).json()
        client.put(settings_path, json={**settings, "paused": False})
        presence = client.post(
            f"/api/role/stories/{run['story_id']}/presence", json={"visible": True, "typing": False}
        )
        assert presence.json() == {"task_id": None, "reason": "opening"}
        # The execution gate is server-side, not merely a disabled text box.
        assert (
            client.post(
                f"/api/sessions/{run['player_id']}/messages",
                json={"content": "跳过开场", "client_message_id": "blocked"},
            ).status_code
            == 409
        )
        from chatbot.storage import Story, Task

        with db.session.begin() as s:
            owner = s.get(Story, run["story_id"]).owner_id
            pending = runtime.enqueue(s, "respond", owner, {}, session_id=run["player_id"])
            s.flush()
            pending_id = pending.id
        assert client.post(path + "/advance", json=advance).status_code == 409
        with db.session.begin() as s:
            s.get(Task, pending_id).status = "completed"
            runtime.enqueue(s, "compact", owner, {}, session_id=run["player_id"])
            runtime.enqueue(
                s,
                "scene_run",
                owner,
                {"story_id": run["story_id"], "scene": "隔壁", "audience": []},
            )
        requests = [advance, {**advance, "request_id": "parallel-second"}]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda payload: client.post(path + "/advance", json=payload), requests))
        assert sorted(r.status_code for r in responses) == [200, 409]
        winner = next(i for i, response in enumerate(responses) if response.status_code == 200)
        advance, second = requests[winner], responses[winner]
        assert second.status_code == 200, second.text
        assert second.json()["execution"]["version"] == 2
        assert client.post(path + "/advance", json=advance).json() == second.json()
        assert client.post(path + "/advance", json={**advance, "request_id": "stale"}).status_code == 409
        assert client.post(path + "/advance", json={**advance, "expected_version": 2}).status_code == 409
        finished = client.post(
            path + "/advance", json={**advance, "request_id": "finish", "expected_version": 2}
        )
        assert finished.status_code == 200, finished.text
        assert finished.json()["execution"]["phase"] == "interactive"
        with db.session() as s:
            rows = list(
                s.scalars(
                    select(SceneRecord)
                    .where(SceneRecord.story_id == run["story_id"])
                    .order_by(SceneRecord.seq)
                )
            )
            rows = [r for r in rows if r.payload.get("opening_block")]
            assert [r.text for r in rows] == [b["text"] for b in node()["opening"]]
            assert [source_class({"kind": r.kind, "detail": r.payload}) for r in rows] == [
                "confirmed_event",
                "utterance",
            ]
            assert rows[1].actor_id == run["actors"]["curator"]
            assert rows[1].addressees == [run["player_id"]]
            assert "basis" not in rows[1].payload
            visible = {r.id for r in visible_records(s, run["player_id"])}
            assert {r.id for r in rows} <= visible
            others = [v for k, v in run["actors"].items() if k not in {"player", "curator"}]
            for actor in others:
                assert not {r.id for r in rows} & {r.id for r in visible_records(s, actor)}
        recipe["narrative"]["nodes"][0]["opening"][1]["speaker"] = "missing"
        assert (
            client.put(
                base + "/blueprints/" + str(uuid4()), json={"expected_version": 0, "recipe": recipe}
            ).status_code
            == 422
        )
        recipe["narrative"]["nodes"][0]["opening"][1].update(speaker="curator", target="missing")
        assert (
            client.put(
                base + "/blueprints/" + str(uuid4()), json={"expected_version": 0, "recipe": recipe}
            ).status_code
            == 422
        )
