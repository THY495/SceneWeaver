import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from chatbot.api import create_app
from chatbot.auto_routing import assess, resolve_decision, routing_messages
from chatbot.experiment_templates import template
from chatbot.items.catalog import instantiate
from chatbot.items.models import ActorState, Placement, World
from chatbot.observations import append_record
from chatbot.storage import Conversation, ExperimentInitial, ItemWorldState, SceneRecord, Story, Task


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


def test_companion_agreement_requires_npc_own_scene_speech():
    exits = [{"id": "climb", "requires_agreement": ["companion"]}]
    observations = [
        {"id": "player", "text": "我和他一起上楼。", "kind": "speech", "actor_alias": "player"},
        {"id": "npc", "text": "好，我跟你上去。", "kind": "speech", "actor_alias": "companion"},
    ]
    row = {
        "edge_id": "climb",
        "status": "met",
        "evidence": [{"event_id": "player", "quote": "一起上楼"}],
        "reason": "同行",
    }
    decision = resolve_decision({"assessments": [row]}, exits, observations, {"player"})
    assert decision["edge_id"] is None
    assert "同行立场" in decision["reason"]
    row["evidence"].append({"event_id": "npc", "quote": "我跟你上去"})
    row["agreements"] = [
        {
            "actor": "companion",
            "status": "agreed",
            "evidence": [{"event_id": "npc", "quote": "我跟你上去"}],
            "reason": "本人明确同行",
        }
    ]
    assert resolve_decision({"assessments": [row]}, exits, observations, {"player"})["edge_id"] == "climb"
    row["evidence"][1]["quote"] = "好，我跟你上去。"
    assert resolve_decision({"assessments": [row]}, exits, observations, {"player"})["edge_id"] == "climb"
    row["evidence"][1]["quote"] = "好，我跟你"
    assert resolve_decision({"assessments": [row]}, exits, observations, {"player"})["edge_id"] is None
    observations[1]["text"] = "我在这里等着，不跟你上去。"
    row["evidence"][1]["quote"] = "不跟你上去"
    row["agreements"][0]["evidence"][0]["quote"] = "不跟你上去"
    row["agreements"][0]["status"] = "declined"
    assert resolve_decision({"assessments": [row]}, exits, observations, {"player"})["edge_id"] is None
    row["agreements"][0]["status"] = "agreed"
    observations[1]["kind"] = "host"
    assert resolve_decision({"assessments": [row]}, exits, observations, {"player"})["edge_id"] is None
    assert "requires_agreement" in routing_messages(exits, observations)[0]["content"]


def prepare(client, runtime, db, *, holding=False, companion_line=None):
    recipe = template("partial-hearing")
    if companion_line is not None:
        recipe["people"][0]["location"] = recipe["player"]["location"]
    recipe["narrative"] = {
        "entry": "start",
        "nodes": [
            {"id": "start", "title": "大厅", "text": "大家正在商量。", "observers": ["player"]},
            {
                "id": "end",
                "title": "码头",
                "text": "你来到码头。",
                "observers": ["player"],
                **(
                    {"entry_locations": [{"actor": "curator", "location": "码头"}]}
                    if companion_line is not None
                    else {}
                ),
            },
        ],
        "edges": [
            {
                "id": "leave",
                "source": "start",
                "target": "end",
                "label": "前往码头",
                "trigger": "ai",
                "condition": "玩家明确表示要去码头。",
                **({"requires_agreement": ["curator"]} if companion_line is not None else {}),
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
    if companion_line is not None:
        with db.session.begin() as s:
            origin = s.get(ExperimentInitial, run["story_id"])
            companion = s.get(Conversation, origin.receipt["actors"]["curator"])
            append_record(
                s,
                story_id=run["story_id"],
                kind="speech",
                actor=companion,
                text=companion_line,
                observer_ids=[run["player_id"], companion.id],
                dedupe="companion-route-intent",
            )
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


async def test_offscreen_scene_run_does_not_trap_player_auto_transition(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, path = prepare(client, runtime, db)
        fake.replies["scene_routing"] = {
            "assessments": [
                {
                    "edge_id": "leave",
                    "status": "met",
                    "evidence": [{"event_id": task.payload["event_id"], "quote": "我决定去码头。"}],
                    "reason": "玩家决定离开",
                }
            ]
        }
        await assess(db, fake, task)
        runtime.finish(task.id, task.lease_version, {})
        ready = client.get(path).json()["auto_ready"]
        assert ready
        with db.session.begin() as s:
            scene = runtime.enqueue(
                s,
                "scene_run",
                config.workspace_owner,
                {"story_id": run["story_id"], "scene": "隔壁", "audience": []},
            )
            scene_id = scene.id
        result = client.post(path + "/auto-continue", json=ready)
        assert result.status_code == 200, result.text
        assert result.json()["node_id"] == "end"
        with db.session() as s:
            assert s.get(Task, scene_id).status == "queued"


@pytest.mark.parametrize(
    ("line", "status", "should_move"),
    [
        ("好，我跟你去码头。", "met", True),
        ("我在这里等着，不跟你去。", "not_met", False),
    ],
)
async def test_companion_route_respects_own_decision_and_world_location(
    db, config, runtime, fake, line, status, should_move
):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, path = prepare(client, runtime, db, companion_line=line)
        with db.session() as s:
            origin = s.get(ExperimentInitial, run["story_id"])
            companion_id = origin.receipt["actors"]["curator"]
            companion_record = s.scalar(
                select(SceneRecord).where(
                    SceneRecord.story_id == run["story_id"], SceneRecord.dedupe == "companion-route-intent"
                )
            )
        fake.replies["scene_routing"] = {
            "assessments": [
                {
                    "edge_id": "leave",
                    "status": status,
                    "evidence": [
                        {"event_id": task.payload["event_id"], "quote": "我决定去码头。"},
                        {"event_id": companion_record.id, "quote": line},
                    ]
                    if should_move
                    else [],
                    "agreements": [
                        {
                            "actor": "curator",
                            "status": "agreed" if should_move else "declined",
                            "evidence": [{"event_id": companion_record.id, "quote": line}],
                            "reason": "本人表态",
                        }
                    ],
                    "reason": "角色自己决定是否同行",
                }
            ]
        }
        await assess(db, fake, task)
        runtime.finish(task.id, task.lease_version, {})
        frame = client.get(path).json()
        assert bool(frame["auto_ready"]) is should_move
        if should_move:
            result = client.post(path + "/auto-continue", json=frame["auto_ready"])
            assert result.status_code == 200, result.text
            assert result.json()["node_id"] == "end"
        with db.session() as s:
            location = s.get(Conversation, companion_id).current_state["location"]
        assert location == ("码头" if should_move else "门厅")


@pytest.mark.parametrize("agrees", [True, False])
async def test_directed_companion_speech_routes_to_matching_narration_and_location(
    db, config, runtime, fake, agrees
):
    """Run actor generation, review, routing, opening and world movement together."""
    from chatbot.observations import visible_records
    from chatbot.worker import Worker

    config.product = "roleplay"
    recipe = template("partial-hearing")
    recipe["people"] = [recipe["people"][0]]
    recipe["people"][0]["location"] = "门厅"
    recipe["people"][0]["unnoticed"] = []
    recipe["speech"] = []
    recipe["narrative"] = {
        "entry": "hall",
        "nodes": [
            {
                "id": "hall",
                "title": "门厅",
                "text": "你们站在门厅。",
                "observers": ["player", "curator"],
                "direction": {
                    "objective": "回应玩家的出行邀请",
                    "beats": [
                        {
                            "id": "invitation",
                            "title": "同行决定",
                            "objective": "林岚本人明确表达是否同行",
                            "participants": ["curator"],
                            "completion": "林岚表达决定后交还玩家",
                        }
                    ],
                },
            },
            {
                "id": "together",
                "title": "码头同行",
                "text": "你和林岚抵达码头。",
                "observers": ["player", "curator"],
                "entry_locations": [
                    {"actor": "player", "location": "码头"},
                    {"actor": "curator", "location": "码头"},
                ],
            },
            {
                "id": "alone",
                "title": "码头独行",
                "text": "你独自抵达码头，林岚仍留在门厅。",
                "observers": ["player"],
                "entry_locations": [{"actor": "player", "location": "码头"}],
            },
        ],
        "edges": [
            {
                "id": "with-curator",
                "source": "hall",
                "target": "together",
                "label": "一起去码头",
                "trigger": "ai",
                "condition": "玩家去码头，林岚明确同意同行。",
                "requires_agreement": ["curator"],
            },
            {
                "id": "without-curator",
                "source": "hall",
                "target": "alone",
                "label": "独自去码头",
                "trigger": "ai",
                "condition": "玩家去码头，林岚明确拒绝同行。",
            },
        ],
    }
    bid = str(uuid4())
    speech = "好，我和你去码头。" if agrees else "我留在门厅，你自己去吧。"
    with TestClient(create_app(config, db, fake)) as client:
        base = "/api/role/experiments"
        saved = client.put(base + "/blueprints/" + bid, json={"expected_version": 0, "recipe": recipe})
        assert saved.status_code == 200, saved.text
        run = client.post(
            base + "/start", json={"request_id": bid, "blueprint_id": bid, "blueprint_version": 1}
        ).json()
        companion_id = run["actors"]["curator"]
        fake.replies.update(
            {
                "director": {
                    "beat_id": "invitation",
                    "after_plan": "yield_player",
                    "remaining_work": [],
                    "reason": "决定已表达",
                    "jobs": [{"kind": "free", "id": "curator-reply", "actor": companion_id, "objective": "表达本人决定"}],
                },
                "director_assignment": {"checks": [], "accepted": True, "issues": []},
                "role_intent": {
                    "act": "speak",
                    "purpose": "回答邀请",
                    "addressee_id": run["player_id"],
                    "source_ids": [],
                    "wait_for_player": False,
                    "deception": False,
                    "retrieval": "context",
                },
                "dialogue": {"text": speech, "spoken_parts": [speech], "claims": []},
                "director_progress": {"status": "met", "reason": "本人已答复", "evidence": []},
            }
        )

        def route(messages):
            observations = json.loads(messages[-1]["content"])["observations"]
            player_event = next(row for row in observations if row["is_player"] and "码头" in row["text"])
            npc_event = next(
                row for row in observations if row["actor_alias"] == "curator" and row["text"] == speech
            )
            evidence = [
                {"event_id": player_event["id"], "quote": "去码头"},
                {"event_id": npc_event["id"], "quote": speech},
            ]
            agreement = {
                "actor": "curator",
                "status": "agreed" if agrees else "declined",
                "evidence": [evidence[1]],
                "reason": "本人表达决定",
            }
            return {
                "assessments": [
                    {
                        "edge_id": "with-curator",
                        "status": "met" if agrees else "not_met",
                        "evidence": evidence if agrees else [],
                        "agreements": [agreement],
                        "reason": "同行决定",
                    },
                    {
                        "edge_id": "without-curator",
                        "status": "not_met" if agrees else "met",
                        "evidence": [] if agrees else evidence,
                        "agreements": [],
                        "reason": "独行决定",
                    },
                ]
            }

        fake.replies["scene_routing"] = route
        client.put(f"/api/role/stories/{run['story_id']}/settings", json={"paused": False})
        runtime.submit(run["player_id"], config.workspace_owner, "我想去码头，你要一起吗？", "invite")
        task = runtime.claim("companion-journey")
        await Worker(db, config, fake).run_task(task)
        path = base + f"/stories/{run['story_id']}/narrative"
        frame = client.get(path).json()
        with db.session() as s:
            selected_edge = s.get(Story, run["story_id"]).settings["auto_route"]["edge_id"]
        assert selected_edge == ("with-curator" if agrees else "without-curator")
        assert frame["auto_ready"]
        result = client.post(path + "/auto-continue", json=frame["auto_ready"])
        assert result.status_code == 200, result.text
        assert result.json()["node_id"] == ("together" if agrees else "alone")
        with db.session() as s:
            player_lines = [record.text for record in visible_records(s, run["player_id"])]
            curator = s.get(Conversation, companion_id)
            player = s.get(Conversation, run["player_id"])
        assert speech in player_lines
        assert ("你和林岚抵达码头。" if agrees else "你独自抵达码头，林岚仍留在门厅。") in player_lines
        assert curator.current_state["location"] == ("码头" if agrees else "门厅")
        assert player.current_state["location"] == "码头"


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


@pytest.mark.parametrize("question_state", ["current", "historical", "answered", "optional", "required"])
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
                payload={
                    "exchange": {"target_id": player.id, "expects_reply": True},
                    **(
                        {"intent": {"requires_player_action": question_state == "required"}}
                        if question_state in {"optional", "required"}
                        else {}
                    ),
                },
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
        blocked = question_state in {"current", "required"}
        assert audit["accepted"] is not blocked
        assert bool(route) is not blocked
        assert any(role == "scene_routing" for role, _, _ in fake.calls) is not blocked
