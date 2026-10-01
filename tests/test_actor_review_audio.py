import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from support.story import launch

from chatbot.agents import Agents
from chatbot.api import create_app
from chatbot.npc.context import NPCServices, TurnContext
from chatbot.npc.performance import UnsafeDraftExhausted, perform_actor
from chatbot.npc.speech_projection import SpeechProjection
from chatbot.observability.timing import TimingTrace
from chatbot.providers import OutputTruncated
from chatbot.storage import Conversation, SceneRecord
from chatbot.voice_candidate import CandidateAudio


@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("projection", ["valid", "repaired", "failed", "silent"])
async def test_actual_actor_review_runs_alongside_private_audio(
    db, config, runtime, fake, accepted, projection
):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "你好", "review-audio")
    task = runtime.claim("test")
    fake.replies["dialogue"] = {
        "text": "（点头）" if projection == "silent" else "你好。",
        "spoken_parts": ["你好。"] if projection == "valid" else [],
        "claims": [],
    }
    audio_started, review_started = asyncio.Event(), asyncio.Event()
    original = fake.chat

    async def chat(task_id, role, messages, **kwargs):
        if kwargs.get("schema") is SpeechProjection:
            return {
                "has_dialogue": projection != "silent",
                "spoken_parts": []
                if projection == "silent"
                else ["你好。" if projection == "repaired" else "projection-invented-sentinel"],
            }
        if role == "actor_review":
            review_started.set()
            reviewed = json.dumps(messages, ensure_ascii=False)
            assert "projection-invented-sentinel" not in reviewed
            if projection in {"valid", "repaired"}:
                await asyncio.wait_for(audio_started.wait(), 2)
            return {"accepted": accepted, "issues": [] if accepted else ["需要修正"]}
        return await original(task_id, role, messages, **kwargs)

    fake.chat = chat

    async def audio(task, text, voice, direction):
        audio_started.set()
        await asyncio.wait_for(review_started.wait(), 2)
        return CandidateAudio(text, voice, direction, "test-scope", b"\x00\x00")

    agents = Agents(db, config, runtime, fake)
    services = NPCServices(db, config, runtime, fake, agents.context, agents.knowledge)
    with db.session() as s:
        actor = s.get(Conversation, actors[0])
        before = s.scalar(select(func.count()).select_from(SceneRecord))
    call = perform_actor(
        services,
        task,
        TurnContext(sid, player, tuple(actors)),
        actor,
        {"act": "speak", "purpose": "问好"},
        0,
        voice_service=SimpleNamespace(stream_candidate=audio, discard_candidate=AsyncMock()),
        voice="Cherry",
        director_assignment={"objective": "向另一NPC问好", "addressees": [actors[1]]},
    )
    if accepted:
        result = await call
        if projection in {"valid", "repaired"}:
            assert result["_candidate_audio"].text == "你好。"
            assert result["_audio_status"] == "ready"
        else:
            assert result["_candidate_audio"] is None
            assert result["_audio_status"] == ("failed" if projection == "failed" else "not_requested")
    else:
        with pytest.raises(UnsafeDraftExhausted):
            await call
    assert audio_started.is_set() == (projection in {"valid", "repaired"})
    assert review_started.is_set()
    dialogue = next(messages for role, messages, _ in fake.calls if role == "dialogue")
    assert any("向另一NPC问好" in message["content"] for message in dialogue)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == before


async def test_directed_actor_repairs_control_handoff_before_review(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "我选择旅行主题。", "handoff-leak")
    task = runtime.claim("test")
    drafts = 0

    def dialogue(messages):
        nonlocal drafts
        drafts += 1
        if drafts == 2:
            assert "director_control_leak" in messages[-1]["content"]
        text = "好的，旅行主题我记下了。场景交回给你。" if drafts == 1 else "好的，旅行主题我记下了。"
        return {"text": text, "spoken_parts": [text], "claims": []}

    fake.replies["dialogue"] = dialogue
    agents = Agents(db, config, runtime, fake)
    with db.session() as s:
        actor = s.get(Conversation, actors[0])
    result = await perform_actor(
        NPCServices(db, config, runtime, fake, agents.context, agents.knowledge),
        task,
        TurnContext(sid, player, tuple(actors)),
        actor,
        {"act": "speak", "purpose": "确认选择"},
        0,
        director_assignment={"objective": "确认玩家的旅行主题选择", "addressees": [player]},
    )
    assert drafts == 2
    assert result["answer"] == "好的，旅行主题我记下了。"
    assert len([role for role, _, _ in fake.calls if role == "actor_review"]) == 1


async def test_actor_repairs_unexecuted_prop_stage_direction_before_review(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "你怎么看？", "stage-prop-repair")
    task = runtime.claim("test")
    drafts = 0

    def dialogue(messages):
        nonlocal drafts
        drafts += 1
        if drafts == 2:
            assert "unexecuted_stage_object_action" in messages[-1]["content"]
        text = "（馆长把烟按灭）我会等消息。" if drafts == 1 else "（馆长点头）我会等消息。"
        return {"text": text, "spoken_parts": ["我会等消息。"], "claims": []}

    fake.replies["dialogue"] = dialogue
    agents = Agents(db, config, runtime, fake)
    with db.session() as s:
        actor = s.get(Conversation, actors[0])
    result = await perform_actor(
        NPCServices(db, config, runtime, fake, agents.context, agents.knowledge),
        task,
        TurnContext(sid, player, tuple(actors)),
        actor,
        {"act": "speak", "purpose": "回答玩家"},
        0,
    )
    assert drafts == 2
    assert result["answer"] == "（馆长点头）我会等消息。"
    assert len([role for role, _, _ in fake.calls if role == "actor_review"]) == 1


async def test_actor_output_limit_gets_one_larger_retry_before_review(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "请回答。", "output-limit-retry")
    task = runtime.claim("test")
    answer = "我听到了，但还需要核实。"
    fake.replies["dialogue"] = {"text": answer, "spoken_parts": [answer], "claims": []}
    original = fake.chat
    limits = []

    async def chat(task_id, role, messages, **kwargs):
        if role == "dialogue":
            limits.append(kwargs["max_tokens"])
            if len(limits) == 1:
                raise OutputTruncated("model output limit")
        return await original(task_id, role, messages, **kwargs)

    fake.chat = chat
    agents = Agents(db, config, runtime, fake)
    with db.session() as s:
        actor = s.get(Conversation, actors[0])
    trace = TimingTrace(task)
    with trace.activate():
        result = await perform_actor(
            NPCServices(db, config, runtime, fake, agents.context, agents.knowledge),
            task,
            TurnContext(sid, player, tuple(actors)),
            actor,
            {"act": "speak", "purpose": "回应玩家"},
            0,
        )
    assert result["answer"] == answer
    assert limits == [1200, 2400]
    assert len([span for span in trace.spans if span["name"] == "npc.output_limit_retry"]) == 1
    assert len([role for role, _, _ in fake.calls if role == "actor_review"]) == 1


async def test_long_actor_history_leaves_capacity_for_repair(db, config, runtime, fake):
    from chatbot.observations import append_record
    from chatbot.providers import estimate_tokens

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    with db.session.begin() as s:
        actor = s.get(Conversation, actors[0])
        for index in range(40):
            append_record(
                s,
                story_id=sid,
                actor=actor,
                kind="speech",
                text=f"旧话{index}。" + "先听大家的意见，再讨论下一步。" * 15,
                observer_ids=[actor.id, player],
                dedupe=f"repair-tail:{index}",
            )
    runtime.submit(player, config.workspace_owner, "你愿意先听听吗？", "repair-headroom")
    task = runtime.claim("test")
    text = "我愿意先听听你的想法。"
    fake.replies["dialogue"] = {
        "text": text,
        "claims": [],
        "spoken_parts": [text],
        "performance": [
            {"text": text, "expression": "neutral", "pose": "neutral", "voice_direction": "平静"}
        ],
    }
    original = fake.chat
    reviews = 0
    generations = []

    async def chat(task_id, role, messages, **kwargs):
        nonlocal reviews
        with_schema = [
            *messages,
            {
                "role": "system",
                "content": "只返回符合下列 JSON Schema 的 JSON 对象，不要 Markdown："
                + json.dumps(kwargs["schema"].model_json_schema(), ensure_ascii=False),
            },
        ]
        assert estimate_tokens(with_schema) + kwargs["max_tokens"] <= config.context_hard_tokens
        if role == "dialogue":
            generations.append(messages)
        if role == "actor_review":
            reviews += 1
            return {"accepted": reviews > 1, "issues": [] if reviews > 1 else ["请更简短。"]}
        return await original(task_id, role, messages, **kwargs)

    fake.chat = chat
    agents = Agents(db, config, runtime, fake)
    trace = TimingTrace(task)
    with trace.activate():
        result = await perform_actor(
            NPCServices(db, config, runtime, fake, agents.context, agents.knowledge),
            task,
            TurnContext(sid, player, tuple(actors)),
            actor,
            {"act": "speak", "purpose": "回答是否愿意", "addressee_id": player},
            0,
        )
    assert result["answer"] == text and reviews == 2
    local = [s["attributes"] for s in trace.spans if s["name"] == "npc.claim_validation"]
    review = [s["attributes"] for s in trace.spans if s["name"] == "npc.semantic_review_result"]
    assert [row["candidate_revision"] for row in local] == [0, 1]
    assert [row["local_issue_count"] for row in local] == [0, 0]
    assert [row["review_accepted"] for row in review] == [False, True]
    assert [row["review_issue_count"] for row in review] == [1, 0]
    assert text not in json.dumps(trace.spans, ensure_ascii=False)
    repair = generations[1][-1]["content"]
    payload = json.loads(repair[repair.index("{") :])
    assert payload["rejected_candidate"] == {"text": text, "claims": []}
