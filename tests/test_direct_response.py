import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.npc.context import TurnContext
from chatbot.npc.direct_response import compile_direct_response
from chatbot.npc.director import prepare_scene_plan, snapshot
from chatbot.observations import append_record
from chatbot.storage import Conversation, Operation, SceneRecord


def setup(db, config, runtime, fake, monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from accept_directed_ferry_live import directed_recipe

    recipe = directed_recipe()
    node = recipe["narrative"]["nodes"][0]
    node["direction"] = {"objective": "回应玩家当前发言", "beats": [{"id": "reply", "title": "直接回应",
        "objective": "根据自己的所知回应玩家", "completion": "回应后交还玩家", "participants": ["captain"],
        "max_segments": 1, "handoff": "after_beat", "direct_response_actor": "captain"}]}
    config.product = "roleplay"
    config.role_direct_response = True
    with TestClient(create_app(config, db, fake)) as client:
        key = str(uuid4())
        client.put("/api/role/experiments/blueprints/" + key,
                   json={"expected_version": 0, "recipe": recipe}).raise_for_status()
        launched = client.post("/api/role/experiments/start",
            json={"request_id": key, "blueprint_id": key, "blueprint_version": 1})
        launched.raise_for_status()
        receipt = launched.json()
        client.put(f"/api/role/stories/{receipt['story_id']}/settings", json={"paused": False}).raise_for_status()
    actor = receipt["actors"]["captain"]
    runtime.submit(receipt["player_id"], config.workspace_owner, "你知道今晚渡船几点开吗？", "direct-test", addressees=[actor])
    task = runtime.claim("direct-test")
    turn = TurnContext(receipt["story_id"], receipt["player_id"],
                       tuple(v for k, v in receipt["actors"].items() if k != "player"))
    services = SimpleNamespace(db=db, config=config, runtime=runtime, provider=fake)
    with db.session() as s:
        frozen = snapshot(s, task, turn)
    return services, task, turn, frozen, receipt


async def test_direct_compiler_uses_real_source_and_original_atomic_plan_install(db, config, runtime, fake, monkeypatch):
    from chatbot.npc import director

    services, task, turn, frozen, _ = setup(db, config, runtime, fake, monkeypatch)
    model = AsyncMock(side_effect=AssertionError("The authorized direct route needs no director model"))
    monkeypatch.setattr(director, "propose_plan", model)
    plan = compile_direct_response(services, task, turn, frozen)
    assert plan.jobs[0].grounded_response.event_id == task.payload["event_id"]
    assert plan.jobs[0].addressees == [turn.player_id]
    ledger = await prepare_scene_plan(services, task, turn)
    assert ledger["plan"] == plan.model_dump()
    with db.session() as s:
        assert s.get(Operation, f"{task.id}:direct-response-frame").payload["plan_revision"] == ledger["revision"]
        assert not list(s.query(SceneRecord).filter(SceneRecord.task_id == task.id, SceneRecord.actor_id != turn.player_id))
    model.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["disabled", "recipient", "constraints", "focus", "exchange", "capacity",
    "recovery", "prior_ledger", "private", "redacted", "partial"])
def test_direct_route_falls_back_instead_of_widening_scope(db, config, runtime, fake, monkeypatch, mutation):
    services, task, turn, frozen, _ = setup(db, config, runtime, fake, monkeypatch)
    beat = frozen.direction.beats[0]
    kwargs = {}
    if mutation == "disabled":
        config.role_direct_response = False
    elif mutation == "recipient":
        task.payload = {**task.payload, "addressees": []}
    elif mutation == "constraints":
        frozen.direction.constraints = ["需要先作另一段表演"]
    elif mutation == "focus":
        beat.role_focus = {beat.participants[0]: "先询问别人"}
    elif mutation == "exchange":
        beat.npc_exchange_required = True
    elif mutation == "capacity":
        beat.max_segments = 2
    elif mutation == "recovery":
        kwargs["recovering"] = True
    elif mutation == "prior_ledger":
        kwargs["previous"] = SimpleNamespace(payload={})
    elif mutation == "private":
        task.payload = {**task.payload, "channel": "private"}
    elif mutation == "partial":
        data = json.loads(frozen.public_context)
        next(e for e in data["events"] if e["event_id"] == task.payload["event_id"])["text"] = "只听见一部分"
        frozen = replace(frozen, public_context=json.dumps(data))
    elif mutation == "redacted":
        with db.session.begin() as s:
            s.get(SceneRecord, task.payload["event_id"]).redacted = True
    assert compile_direct_response(services, task, turn, frozen, **kwargs) is None


@pytest.mark.parametrize("to_player", [True, False])
def test_other_cast_pending_question_prevents_direct_route(db, config, runtime, fake, monkeypatch, to_player):
    services, task, turn, frozen, receipt = setup(db, config, runtime, fake, monkeypatch)
    target = turn.player_id if to_player else frozen.direction.beats[0].participants[0]
    with db.session.begin() as s:
        speaker = s.get(Conversation, receipt["actors"]["keeper"])
        append_record(s, story_id=turn.story_id, kind="speech", text="请先回答我的问题。", actor=speaker,
            observer_ids=[target], channel="public", addressees=[target], dedupe="old-question",
            payload={"exchange": {"expects_reply": True, "target_id": target}})
    assert compile_direct_response(services, task, turn, frozen) is None
