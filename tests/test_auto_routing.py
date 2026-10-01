from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.auto_routing import assess, resolve_decision
from chatbot.experiment_templates import template
from chatbot.items.catalog import instantiate
from chatbot.items.models import ActorState, Placement, World
from chatbot.storage import ItemWorldState, Story


@pytest.mark.parametrize("statuses", [("met", "met"), ("met", "uncertain"), ("not_met", "not_met")])
def test_conflicting_or_uncertain_exits_never_transition(statuses):
    exits = [{"id": "a"}, {"id": "b"}]
    observations = [{"id": "e", "text": "我现在出发。"}]
    assessments = [
        {
            "edge_id": edge["id"],
            "status": status,
            "evidence": [{"event_id": "e", "quote": "现在出发"}],
            "reason": "test",
        }
        for edge, status in zip(exits, statuses)
    ]
    assert resolve_decision({"assessments": assessments}, exits, observations)["edge_id"] is None


def test_missing_duplicate_and_unique_exits():
    exits = [{"id": "a"}, {"id": "b"}]
    observations = [{"id": "e", "text": "出发"}]
    a = {"edge_id": "a", "status": "met", "evidence": [{"event_id": "e", "quote": "出发"}], "reason": "test"}
    b = {**a, "edge_id": "b", "status": "not_met"}
    for rows in [[a], [a, a], [a, {**b, "edge_id": "unknown"}]]:
        assert resolve_decision({"assessments": rows}, exits, observations)["edge_id"] is None
    assert resolve_decision({"assessments": [a, b]}, exits, observations)["edge_id"] == "a"
    old_only = resolve_decision({"assessments": [a, b]}, exits, observations, {"new-event"})
    assert old_only["edge_id"] is None
    assert old_only["reason"] == "仅引用历史事件，缺少本轮证据"


def prepare(client, runtime, db, *, holding=False):
    recipe = template("partial-hearing")
    recipe["narrative"] = {
        "entry": "start",
        "nodes": [
            {"id": "start", "title": "大厅", "text": "大家正在商量。", "observers": ["player"]},
            {"id": "end", "title": "码头", "text": "你来到码头。", "observers": ["player"]},
        ],
        "edges": [
            {
                "id": "leave",
                "source": "start",
                "target": "end",
                "label": "前往码头",
                "trigger": "ai",
                "condition": "玩家明确表示要去码头。",
            }
        ],
    }
    if holding:
        recipe["item_world"] = World(
            items=[
                instantiate(
                    "paper-sheet", "letter", Placement(kind="location", target=recipe["player"]["location"])
                )
            ],
            actors=[
                ActorState(id=p["alias"], location=p["location"])
                for p in [*recipe["people"], recipe["player"]]
            ],
        ).model_dump()
        recipe["narrative"]["edges"][0]["requires_holding"] = {"item": "letter", "actor": "player"}
    bid = str(uuid4())
    base = "/api/role/experiments"
    saved = client.put(base + "/blueprints/" + bid, json={"expected_version": 0, "recipe": recipe})
    assert saved.status_code == 200, saved.text
    run = client.post(
        base + "/start", json={"request_id": bid, "blueprint_id": bid, "blueprint_version": 1}
    ).json()
    with db.session() as s:
        owner = s.get(Story, run["story_id"]).owner_id
    client.put(f"/api/role/stories/{run['story_id']}/settings", json={"paused": False})
    runtime.submit(run["player_id"], owner, "我决定去码头。", "go")
    task = runtime.claim("routing")
    return run, task, base + f"/stories/{run['story_id']}/narrative"


@pytest.mark.parametrize("invalid", [False, True])
async def test_evidence_proposal_and_automatic_transition(db, config, runtime, fake, invalid):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, path = prepare(client, runtime, db)
        assert client.get(path).json()["choices"] == []
        assert (
            client.post(
                path + "/choose", json={"request_id": "bypass", "edge_id": "leave", "expected_revision": 1}
            ).status_code
            == 409
        )
        fake.replies["scene_routing"] = {
            "edge_id": "leave",
            "evidence": [
                {
                    "event_id": task.payload["event_id"],
                    "quote": "并不存在的原话" if invalid else "我决定去码头。",
                }
            ],
            "reason": "玩家明确离开",
        }
        fake.replies["scene_routing"] = {"assessments": [{**fake.replies["scene_routing"], "status": "met"}]}
        if invalid:
            await assess(db, fake, task)
            runtime.finish(task.id, task.lease_version, {})
        else:
            from chatbot.worker import Worker

            await Worker(db, config, fake).run_task(task)
            assert any(role == "scene_routing" for role, _, _ in fake.calls)
        frame = client.get(path).json()
        trace = client.get(f"/api/role/stories/{run['story_id']}/tasks/{task.id}/trace").json()
        assert trace["scene_routing"]["accepted"] is (not invalid)
        assert trace["scene_routing"]["decision"]["assessments"][0]["edge_id"] == "leave"
        if invalid:
            assert frame["auto_ready"] is None
            return
        assert frame["node_id"] == "start"  # Await reading, not a visible choice.
        assert "condition" not in str(frame) and "reason" not in str(frame)
        body = frame["auto_ready"]
        result = client.post(path + "/auto-continue", json=body)
        assert result.status_code == 200, result.text
        assert result.json()["node_id"] == "end"
        assert client.post(path + "/auto-continue", json=body).status_code == 409


async def test_item_gate_and_stale_proposal(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, path = prepare(client, runtime, db, holding=True)
        fake.replies["scene_routing"] = {
            "edge_id": "leave",
            "evidence": [{"event_id": task.payload["event_id"], "quote": "我决定去码头。"}],
            "reason": "离开",
        }
        fake.replies["scene_routing"] = {"assessments": [{**fake.replies["scene_routing"], "status": "met"}]}
        await assess(db, fake, task)
        assert not fake.calls  # Words cannot satisfy a physical holding requirement.
        with db.session.begin() as s:
            row = s.get(ItemWorldState, run["story_id"])
            state = dict(row.state)
            state["items"] = [
                {**i, "placement": {"kind": "actor", "target": run["player_id"]}} for i in state["items"]
            ]
            row.state = state
        await assess(db, fake, task)
        runtime.finish(task.id, task.lease_version, {})
        ready = client.get(path).json()["auto_ready"]
        assert ready
        with db.session.begin() as s:
            row = s.get(ItemWorldState, run["story_id"])
            state = dict(row.state)
            state["items"] = [
                {**i, "placement": {"kind": "location", "target": "大厅"}} for i in state["items"]
            ]
            row.state = state
        assert client.post(path + "/auto-continue", json=ready).status_code == 409

        with db.session.begin() as s:
            story = s.get(Story, run["story_id"])
            story.seq += 1
        assert client.get(path).json()["auto_ready"] is None
        assert client.post(path + "/auto-continue", json=ready).status_code == 409


async def test_cancelling_response_revokes_its_pending_auto_route(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, path = prepare(client, runtime, db)
        fake.replies["scene_routing"] = {
            "assessments": [
                {
                    "edge_id": "leave",
                    "status": "met",
                    "evidence": [{"event_id": task.payload["event_id"], "quote": "我决定去码头。"}],
                    "reason": "玩家当前决定",
                }
            ]
        }
        await assess(db, fake, task)
        ready = client.get(path).json()["auto_ready"]
        assert ready
        cancelled = client.post(f"/api/tasks/{task.id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert client.get(path).json()["auto_ready"] is None
        assert client.post(path + "/auto-continue", json=ready).status_code == 409
        assert client.get(path).json()["node_id"] == "start"


@pytest.mark.parametrize("question_state", ["current", "historical", "answered"])
async def test_new_player_question_defers_exit_but_old_or_answered_does_not(
    db, config, runtime, fake, question_state
):
    from chatbot.observations import append_record
    from chatbot.storage import Conversation, Operation

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, path = prepare(client, runtime, db)
        with db.session.begin() as s:
            actor = s.get(Conversation, next(iter(run["actors"].values())))
            player = s.get(Conversation, run["player_id"])
            question = append_record(
                s,
                story_id=run["story_id"],
                kind="speech",
                text="还有什么要说明的吗？",
                actor=actor,
                observer_ids=[actor.id, player.id],
                dedupe="closing-question",
                task_id=task.id if question_state != "historical" else None,
                payload={"exchange": {"target_id": player.id, "expects_reply": True}},
            )
            if question_state == "answered":
                append_record(
                    s,
                    story_id=run["story_id"],
                    kind="speech",
                    text="没有了，我决定出发。",
                    actor=player,
                    observer_ids=[actor.id, player.id],
                    dedupe="closing-answer",
                    task_id=task.id,
                    payload={"exchange": {"reply_to": question.id, "response_kind": "answer"}},
                )
        fake.replies["scene_routing"] = {
            "assessments": [
                {
                    "edge_id": "leave",
                    "status": "met",
                    "reason": "玩家明确离开",
                    "evidence": [{"event_id": task.payload["event_id"], "quote": "我决定去码头。"}],
                }
            ]
        }
        await assess(db, fake, task)
        with db.session() as s:
            audit = s.get(Operation, f"scene-routing-audit:{task.id}").payload
            route = s.get(Story, run["story_id"]).settings.get("auto_route")
        blocked = question_state == "current"
        assert audit["accepted"] is not blocked
        assert bool(route) is not blocked
        assert any(role == "scene_routing" for role, _, _ in fake.calls) is not blocked
