"""NPC questions cannot introduce an implicit dependency for an independent exit."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.voice import app as app  # noqa: F401
from test_auto_routing import prepare
from test_player_action_routing import decision, npc_line

from chatbot.api import create_app
from chatbot.npc.context import NPCServices
from chatbot.observations import visible_records
from chatbot.providers import BudgetExceeded
from chatbot.storage import Conversation, ExperimentInitial, RoleMemory, SceneRecord, Story, Task
from chatbot.worker import Worker


def services(worker):
    return NPCServices(worker.db, worker.config, worker.runtime, worker.provider,
                       worker.agents.context, worker.agents.knowledge)


async def test_independent_exit_skips_npc_generation_but_waits_for_player_continuation(db, config, runtime, fake):
    config.product, config.role_player_action_preflight = "roleplay", True
    with TestClient(create_app(config, db, fake)) as client:
        run, task, path = prepare(client, runtime, db)
        fake.replies["scene_routing"] = decision(task)
        worker = Worker(db, config, fake)
        await worker.run_task(task)
        assert [role for role, _, _ in fake.calls] == ["scene_routing"]
        with db.session() as s:
            assert s.get(Story, run["story_id"]).settings["narrative"]["node"] == "start"
        frame = client.get(path).json()
        assert frame["auto_ready"]
        result = client.post(path + "/auto-continue", json=frame["auto_ready"])
        assert result.status_code == 200 and result.json()["node_id"] == "end"


@pytest.mark.parametrize("action", ["dependent", "uncertain"])
async def test_dependent_or_uncertain_exit_keeps_npc_performance(db, config, runtime, fake, monkeypatch, action):
    from chatbot.npc.player_action import preflight
    from chatbot.npc.turns import respond_player

    config.product, config.role_player_action_preflight = "roleplay", True
    with TestClient(create_app(config, db, fake)) as client:
        _, task, _ = prepare(client, runtime, db)
        fake.replies["scene_routing"] = decision(task, action=action)
        worker = Worker(db, config, fake)
        assert await preflight(services(worker), task) is None
        # Normal generation remains available; do not force an inferred action.
        planned = AsyncMock(side_effect=RuntimeError("normal NPC path reached"))
        monkeypatch.setattr("chatbot.npc.turns.plan_actor", planned)
        with pytest.raises(RuntimeError, match="normal NPC path reached"):
            await respond_player(services(worker), task)
        assert planned.await_count == 1


async def test_holding_constraint_prevents_preflight_without_a_model(db, config, runtime, fake):
    from chatbot.npc.player_action import preflight

    config.product, config.role_player_action_preflight = "roleplay", True
    with TestClient(create_app(config, db, fake)) as client:
        _, task, _ = prepare(client, runtime, db, holding=True)
        assert await preflight(services(Worker(db, config, fake)), task) is None
        assert not fake.calls


@pytest.mark.parametrize("protected", ["default_disabled", "private", "handoff_proposal_id", "autonomous"])
async def test_protected_turns_do_not_enter_preflight(db, config, runtime, fake, protected):
    from chatbot.npc.player_action import preflight

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, task, _ = prepare(client, runtime, db)
        if protected != "default_disabled":
            config.role_player_action_preflight = True
            task.payload = {**task.payload, **({"channel": "private"} if protected == "private" else {protected: True})}
        assert await preflight(services(Worker(db, config, fake)), task) is None
        assert not fake.calls


async def test_preflight_budget_exhaustion_does_not_start_npc_generation(db, config, runtime, fake):
    from chatbot.npc.turns import respond_player

    config.product, config.role_player_action_preflight = "roleplay", True
    with TestClient(create_app(config, db, fake)) as client:
        _, task, _ = prepare(client, runtime, db)
        calls = []

        async def exhausted(*args, **kwargs):
            calls.append(args[1])
            raise BudgetExceeded("预算已用尽")

        fake.chat = exhausted
        with pytest.raises(BudgetExceeded):
            await respond_player(services(Worker(db, config, fake)), task)
        assert calls == ["scene_routing"]


async def test_unagreed_companion_and_published_prefix_are_not_bypassed(db, config, runtime, fake):
    from chatbot.npc.player_action import preflight

    config.product, config.role_player_action_preflight = "roleplay", True
    with TestClient(create_app(config, db, fake)) as client:
        run, task, _ = prepare(client, runtime, db, companion_line="我还没决定是否一起去。")
        fake.replies["scene_routing"] = decision(task)
        assert await preflight(services(Worker(db, config, fake)), task) is None
        assert len(fake.calls) == 1
        npc_line(db, run, task, "雨还在下。", committed=True)
        assert await preflight(services(Worker(db, config, fake)), task) is None
        assert len(fake.calls) == 1


async def test_world_change_after_assessment_does_not_skip_performance(db, config, runtime, fake, monkeypatch):
    from chatbot.npc import player_action
    from chatbot.observations import append_record

    config.product, config.role_player_action_preflight = "roleplay", True
    with TestClient(create_app(config, db, fake)) as client:
        run, task, _ = prepare(client, runtime, db)
        fake.replies["scene_routing"] = decision(task)
        original = player_action.assess

        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            with db.session.begin() as s:
                player = s.get(Conversation, run["player_id"])
                append_record(s, story_id=run["story_id"], kind="fact", actor=player,
                              text="现场发生了新的变化。", observer_ids=[player.id], dedupe="preflight-race")
            return result

        monkeypatch.setattr(player_action, "assess", changed)
        assert await player_action.preflight(services(Worker(db, config, fake)), task) is None
        assert not client.get(f"/api/role/experiments/stories/{run['story_id']}/narrative").json()["auto_ready"]


@pytest.mark.parametrize("mode", ["prepared", "generating", "already_playing"])
async def test_preflight_discards_only_unplayed_ack_without_invalidating_exit(app, db, config, runtime, fake, mode):
    from chatbot.acknowledgements import generate_acknowledgement
    from chatbot.agents import Agents
    from chatbot.npc.player_action import preflight

    config.role_player_action_preflight = True
    with TestClient(app) as client:
        run, task, path = prepare(client, runtime, db, companion_line="雨还在下。")
        with db.session.begin() as s:
            origin = s.get(ExperimentInitial, run["story_id"])
            recipe = json.loads(json.dumps(origin.recipe))
            recipe["narrative"]["edges"][0].pop("requires_agreement", None)
            origin.recipe = recipe
            live = s.get(Task, task.id)
            live.payload = {**live.payload, "acknowledgement": True}
            task.payload = live.payload
        fake.replies["scene_routing"] = decision(task)
        fake.replies["acknowledgement"] = "嗯，我听着。"
        generator = None
        if mode == "generating":
            original, entered, release = fake.chat, asyncio.Event(), asyncio.Event()

            async def delayed(*args, **kwargs):
                if args[1] == "acknowledgement":
                    entered.set()
                    await release.wait()
                return await original(*args, **kwargs)

            fake.chat = delayed
            generator = asyncio.create_task(generate_acknowledgement(Agents(db, config, runtime, fake), task))
            await asyncio.wait_for(entered.wait(), timeout=2)
        else:
            await generate_acknowledgement(Agents(db, config, runtime, fake), task)
            with db.session() as s:
                row = s.scalar(select(SceneRecord).where(SceneRecord.task_id == task.id,
                                                         SceneRecord.kind == "ack_candidate"))
                assert row
                ack_id = row.id
            prepared = client.post(f"/api/voice/sessions/{run['player_id']}/playbacks",
                json={"request_id": "preflight-ack", "event_id": ack_id, "epoch": task.epoch, "chunk_index": 0})
            assert prepared.status_code == 200
            playback_id = prepared.json()["id"]
            assert client.post(f"/api/voice/playbacks/{playback_id}/audio").status_code == 200
            if mode == "already_playing":
                assert client.post(f"/api/voice/playbacks/{playback_id}/start", json={}).status_code == 200
        result = await preflight(services(Worker(db, config, fake)), task)
        assert result and result["player_action_route_accepted"]
        if generator:
            release.set()
            await generator
        runtime.finish(task.id, task.lease_version, result)
        frame = client.get(path).json()
        assert frame["auto_ready"]
        if mode == "prepared":
            assert client.post(f"/api/voice/playbacks/{playback_id}/start", json={}).status_code == 404
            with db.session() as s:
                assert s.get(SceneRecord, ack_id).redacted and s.get(SceneRecord, ack_id).seq is None
                assert ack_id not in {row.id for row in visible_records(s, run["player_id"])}
                assert not s.scalar(select(RoleMemory).where(RoleMemory.source_ids.contains([ack_id])))
        elif mode == "generating":
            with db.session() as s:
                assert not s.scalar(select(SceneRecord).where(SceneRecord.task_id == task.id,
                                                              SceneRecord.kind == "ack_candidate"))
        else:
            with db.session() as s:
                assert not s.get(SceneRecord, ack_id).redacted and s.get(SceneRecord, ack_id).kind == "speech"
        transitioned = client.post(path + "/auto-continue", json=frame["auto_ready"])
        assert transitioned.status_code == 200 and transitioned.json()["node_id"] == "end"
