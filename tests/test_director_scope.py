import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from support.story import launch

from chatbot.api import create_app
from chatbot.npc.context import TurnContext
from chatbot.npc.director import prepare_scene_plan, snapshot
from chatbot.observations import add_observation, append_record
from chatbot.storage import Conversation, ExperimentInitial, Story


@pytest.mark.parametrize("valid_evidence", [True, False])
async def test_frozen_scene_scope_and_actual_planner_install(db, config, runtime, fake, valid_evidence):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    with db.session.begin() as s:
        story = s.get(Story, sid)
        story.settings = {**story.settings, "narrative": {"node": "room", "event_id": "entry"}}
        s.add(
            ExperimentInitial(
                story_id=sid,
                owner_id=config.workspace_owner,
                request_id="scope",
                request_hash="scope",
                receipt={"actors": {"a": actors[0], "b": actors[1]}},
                recipe={
                    "people": [],
                    "narrative": {
                        "nodes": [
                            {
                                "id": "room",
                                "direction": {
                                    "objective": "讨论",
                                    "beats": [
                                        {
                                            "id": "beat",
                                            "title": "交谈",
                                            "objective": "交换意见",
                                            "participants": ["a", "b"],
                                            "completion": "玩家回应",
                                        }
                                    ],
                                },
                            }
                        ]
                    },
                },
            )
        )
        append_record(
            s,
            story_id=sid,
            kind="speech",
            text="共同听到的内容",
            actor=s.get(Conversation, actors[0]),
            observer_ids=[actors[0], actors[1]],
            dedupe="shared",
        )
        append_record(
            s,
            story_id=sid,
            kind="speech",
            text="只对自己说的秘密",
            actor=s.get(Conversation, actors[0]),
            observer_ids=[actors[0]],
            channel="private",
            dedupe="secret",
        )
    runtime.submit(player, config.workspace_owner, "聊一下", "scope-turn")
    task = runtime.claim("test")
    turn = TurnContext(sid, player, tuple(actors))
    with db.session() as s:
        scope = snapshot(s, task, turn)
        assert "共同听到的内容" in scope.public_context
        assert "秘密" not in scope.public_context
        assert scope.current_player_event_id == task.payload["event_id"]
        # A payload alone must never promote unseen content into director input.
        original = task.payload
        task.payload = {**original, "event_id": "unseen", "text": "未共同感知的秘密指令"}
        hidden_scope = snapshot(s, task, turn)
        assert hidden_scope.current_player_event_id is None
        assert "未共同感知的秘密指令" not in hidden_scope.public_context
        task.payload = original
        assert scope.eligible == set(actors[:2])
        assert scope.direction.beats[0].participants == actors[:2]
        identities = {p["id"]: p["name"] for p in json.loads(scope.public_context)["cast"]}
        for aid in actors[:2]:
            assert identities[aid] == s.get(Conversation, aid).current_state["profile"]["name"]
    proposal = {
        "beat_id": "beat",
        "after_plan": "yield_player",
        "remaining_work": [],
        "reason": "先询问",
        "jobs": [{"id": "j", "actor": actors[0], "objective": "询问"}],
    }

    async def plan_with_concurrent_ack(*_args, **_kwargs):
        with db.session.begin() as s:
            story = s.get(Story, sid)
            row = append_record(
                s,
                story_id=sid,
                kind="ack_candidate",
                text="嗯。",
                actor=s.get(Conversation, actors[0]),
                observer_ids=[],
                task_id=task.id,
                dedupe=f"ack:{task.id}",
                payload={"acknowledgement": True},
            )
            story.seq += 1
            row.seq, row.kind = story.seq, "speech"
            for aid in [*actors[:2], player]:
                add_observation(s, row, aid)
        return proposal

    provider = SimpleNamespace(
        chat=AsyncMock(side_effect=plan_with_concurrent_ack) if valid_evidence else AsyncMock(return_value=proposal)
    )
    services = SimpleNamespace(db=db, runtime=runtime, provider=provider)
    installed = await prepare_scene_plan(services, task, turn)
    assert installed["revision"] == 1 and installed["plan"]["jobs"][0]["actor"] == actors[0]
    if valid_evidence:
        with db.session() as s:
            assert installed["world_seq"] == s.get(Story, sid).seq
    messages = json.dumps(provider.chat.call_args.args[2], ensure_ascii=False)
    assert "只对自己说的秘密" not in messages
    from chatbot.npc.beat_progress import assess_beat

    provider.chat.side_effect = None
    provider.chat.return_value = {
        "status": "met",
        "reason": "条件已满足",
        "evidence": [
            {
                "event_id": task.payload["event_id"],
                "quote": "聊一下" if valid_evidence else "不存在的引文",
            }
        ],
    }
    assessed = await assess_beat(services, task, turn, "beat")
    assert assessed["advanced"] is valid_evidence
    calls = provider.chat.await_count
    assert await assess_beat(services, task, turn, "beat") == assessed
    assert provider.chat.await_count == calls
    with db.session() as s:
        assert snapshot(s, task, turn).finished is valid_evidence
    if valid_evidence:
        provider.chat.return_value = {
            "beat_id": "beat",
            "reason": "回应追问",
            "jobs": [],
            "after_plan": "yield_player",
            "remaining_work": [],
        }
        followup = await prepare_scene_plan(services, task, turn)
        assert followup["revision"] == 2
        assert "回应当前玩家的追问" in provider.chat.call_args.args[2][1]["content"]
    # Moving a character removes it from eligibility; no frozen roster override.
    with db.session.begin() as s:
        actor = s.get(Conversation, actors[1])
        actor.current_state = {**actor.current_state, "location": "elsewhere"}
    with db.session() as s:
        scope = snapshot(s, task, turn)
        assert scope.eligible == {actors[0]}
        assert actors[1] not in {p["id"] for p in json.loads(scope.public_context)["cast"]}


async def test_legacy_scene_does_not_call_director(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "你好", "legacy")
    task = runtime.claim("test")
    provider = SimpleNamespace(chat=AsyncMock())
    assert (
        await prepare_scene_plan(
            SimpleNamespace(db=db, runtime=runtime, provider=provider),
            task,
            TurnContext(sid, player, tuple(actors)),
        )
        is None
    )
    provider.chat.assert_not_called()


@pytest.mark.parametrize("split_plan", [False, True])
async def test_directed_player_turn_runs_actual_actor_pipeline(db, config, runtime, fake, split_plan):
    """Only the provider is simulated; dispatch, context, review and publication are real."""
    from chatbot.agents import Agents

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    with db.session.begin() as s:
        story = s.get(Story, sid)
        story.settings = {**story.settings, "narrative": {"node": "room", "event_id": "entry"}}
        s.add(
            ExperimentInitial(
                story_id=sid,
                owner_id=config.workspace_owner,
                request_id="pipeline",
                request_hash="pipeline",
                receipt={"actors": dict(zip(["a", "b", "c"], actors))},
                recipe={
                    "people": [],
                    "narrative": {
                        "edges": [],
                        "nodes": [
                            {
                                "id": "room",
                                "direction": {
                                    "objective": "交换意见",
                                    "beats": [
                                        {
                                            "id": "discussion",
                                            "title": "讨论",
                                            "objective": "依次发表意见",
                                            "participants": ["a", "b", "c"],
                                            "completion": "三人表达后等待玩家",
                                        }
                                    ],
                                },
                            }
                        ],
                    },
                },
            )
        )
    fake.replies.update(
        {
            "director": {
                "beat_id": "discussion",
                "after_plan": "yield_player",
                "remaining_work": [],
                "reason": "三人讨论后交还玩家",
                "jobs": [
                    {
                        "id": str(i),
                        "actor": actor,
                        "objective": "表达意见",
                        "depends_on": [str(i - 1)] if i else [],
                    }
                    for i, actor in enumerate(actors)
                ],
            },
            "director_assignment": {"checks": [], "accepted": True, "issues": []},
            "role_intent": {
                "act": "speak",
                "purpose": "回应",
                "wait_for_player": False,
                "source_ids": [],
                "deception": False,
                "retrieval": "context",
                "remaining_request": "稍后由另一人询问玩家",
            },
            "dialogue": {
                "text": "我愿意先听听大家的意见。",
                "claims": [],
                "spoken_parts": ["我愿意先听听大家的意见。"],
            },
            "director_progress": {"status": "not_met", "reason": "等待玩家回应", "evidence": []},
        }
    )
    if split_plan:
        first_plan = fake.replies["director"]
        plans = iter(
            [
                {
                    **first_plan,
                    "jobs": first_plan["jobs"][:2],
                    "after_plan": "continue_scene",
                    "remaining_work": ["第三人发表意见"],
                },
                # Even if the model asks to continue, the author's three-part cap wins.
                {
                    **first_plan,
                    "jobs": [{**first_plan["jobs"][2], "depends_on": []}],
                    "after_plan": "continue_scene",
                    "remaining_work": ["继续讨论"],
                },
            ]
        )
        fake.replies["director"] = lambda messages: next(plans)
    runtime.submit(player, config.workspace_owner, "请大家谈谈意见。", "pipeline-turn")
    task = runtime.claim("test")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["actor_ids"] == actors
    assert len({segment["event_id"] for segment in result["segments"]}) == 3
    roles = [role for role, _, _ in fake.calls]
    assert roles.count("director") == (2 if split_plan else 1)
    assert roles.count("director_assignment") == 3
    assert roles.count("director_progress") == 1
    assert all(segment["intent"].get("remaining_request", "") == "" for segment in result["segments"])
