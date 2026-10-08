"""The experiment must remove a call while retaining the original publication gates."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from support.story import intention, launch

from chatbot.agents import Agents
from chatbot.api import create_app
from chatbot.npc.combined_generation import basis_digest, fallback_reason
from chatbot.npc.context import NPCServices, TurnContext
from chatbot.npc.performance import UnsafeDraftExhausted, perform_actor
from chatbot.npc.planning import plan_actor
from chatbot.storage import Conversation, SceneRecord
from chatbot.voice_candidate import CandidateAudio


@pytest.fixture
def actor_case(db, config, runtime, fake, request):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, getattr(request, "param", "你好。"), "combined-actor")
    task = runtime.claim("test")
    agents = Agents(db, config, runtime, fake)
    with db.session() as s:
        actor = s.get(Conversation, actors[0])
    return SimpleNamespace(
        services=NPCServices(db, config, runtime, fake, agents.context, agents.knowledge),
        task=task, turn=TurnContext(sid, player, tuple(actors)), actor=actor,
        assignment={"objective": "向访客问好", "addressees": [player]},
    )


def speech(text="你好。"):
    return {"text": text, "spoken_parts": [text], "claims": [], "performance": []}


async def prepare(case, fake, *, combined=True, override=None):
    intent = intention(case.turn.player_id, **(override or {}))
    fake.replies.update(role_intent=intent, dialogue={"intent": intent, "speech": speech()})
    if not combined:
        fake.replies["dialogue"] = speech()

    async def recall(*args):
        pass

    return await plan_actor(
        case.services, case.task, case.turn, case.actor,
        memory=SimpleNamespace(prepare_recall=recall), replies=[], step=0, limit=1,
        last_actor=None, continuation="", director_assignment=case.assignment,
        combine_speech=combined,
    )


async def perform(case, fake, intent, *, repair_text="你好。", **kwargs):
    draft = intent.pop("_speech_draft", None)
    fake.replies["dialogue"] = speech(repair_text)
    return await perform_actor(
        case.services, case.task, case.turn, case.actor, intent, 0,
        director_assignment=case.assignment, initial_speech=draft, **kwargs,
    )


@pytest.mark.parametrize("combined,expected", [(False, ["role_intent", "dialogue", "actor_review"]),
                                            (True, ["dialogue", "actor_review"])])
async def test_first_pass_reduces_one_call_without_skipping_review(actor_case, fake, combined, expected):
    intent = await prepare(actor_case, fake, combined=combined)
    reply = await perform(actor_case, fake, intent)
    assert reply["answer"] == "你好。"
    assert [role for role, _, _ in fake.calls] == expected
    if combined:
        prompt = fake.calls[0][1][0]["content"]
        assert "不生成台词" not in prompt
        assert "ROLE_PERFORMANCE_POLICY" not in prompt


@pytest.mark.parametrize("override", [
    {"source_ids": ["unheard-private-event"]}, {"reply_to": "someone-elses-question"},
    {"goal_id": "someone-elses-goal"}, {"addressee_id": "absent-character"},
    {"deception": True},
])
async def test_combined_intent_uses_original_permission_checks(actor_case, fake, override):
    from chatbot.providers import ProviderError

    with pytest.raises(ProviderError):
        await prepare(actor_case, fake, override=override)
    assert not any(role == "actor_review" for role, _, _ in fake.calls)


async def test_review_rejection_cannot_commit_combined_candidate(actor_case, db, fake):
    intent = await prepare(actor_case, fake)
    fake.replies["actor_review"] = {"accepted": False, "issues": ["无据断言"]}
    with db.session() as s:
        before = s.scalar(select(func.count()).select_from(SceneRecord))
    with pytest.raises(UnsafeDraftExhausted):
        await perform(actor_case, fake, intent)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == before
    assert len([role for role, _, _ in fake.calls if role == "actor_review"]) == 3


async def test_stale_combined_basis_requires_new_speech(actor_case, fake):
    intent = await prepare(actor_case, fake)
    intent["_speech_draft"]["basis_digest"] = "old-snapshot"
    reply = await perform(actor_case, fake, intent)
    assert reply["answer"] == "你好。"
    assert [role for role, _, _ in fake.calls] == ["dialogue", "dialogue", "actor_review"]


async def test_knowledge_intent_does_not_reuse_early_speech(actor_case, fake):
    intent = await prepare(actor_case, fake, override={"retrieval": "knowledge"})
    assert "_speech_draft" not in intent
    await perform(actor_case, fake, intent)
    assert [role for role, _, _ in fake.calls] == ["dialogue", "dialogue", "actor_review"]


async def test_unavailable_inspection_does_not_rebind_its_draft_as_speech(actor_case, fake):
    # The old planner converts an unavailable inspect action to an explanation.
    # Its early speech must not become a claim that an inspection happened.
    intent = await prepare(actor_case, fake, override={"act": "inspect", "retrieval": "context"})
    assert intent["act"] == "speak"
    assert "_speech_draft" not in intent
    await perform(actor_case, fake, intent)
    assert [role for role, _, _ in fake.calls] == ["dialogue", "dialogue", "actor_review"]


async def test_exact_profile_alias_is_locally_repaired_and_still_reviewed(actor_case, fake):
    intent = await prepare(actor_case, fake)
    intent["_speech_draft"]["speech"] = {"text": "我是馆长。", "spoken_parts": ["我是馆长。"],
        "claims": [{"text": "我是馆长。", "source_id": actor_case.actor.id, "quote": "馆长", "mode": "fact"}]}
    result = await perform(actor_case, fake, intent)
    assert result["answer"] == "我是馆长。"
    assert result["claims"][0]["source_id"] == f"profile:{actor_case.actor.id}"
    assert [role for role, _, _ in fake.calls] == ["dialogue", "actor_review"]


def test_documents_and_item_handoffs_remain_on_original_path():
    task = SimpleNamespace(payload={})
    info = {"profile": {"document_ids": ["private-document"]}}
    assert fallback_reason(task, info, {}) == "knowledge_documents"
    task.payload["handoff_proposal_id"] = "offer"
    assert fallback_reason(task, info, {}) == "item_protocol"
    assert fallback_reason(task, info, None) == "not_directed"


def test_changed_scene_fact_invalidates_basis_without_actor_revision_change():
    before = {"profile": {}, "scene_facts": {"door": {"value": "closed"}}, "actor_revision": 1}
    after = {**before, "scene_facts": {"door": {"value": "open"}}}
    assert basis_digest(before) != basis_digest(after)


@pytest.mark.parametrize("field", ["known_agreements", "privacy_version", "snapshot_id", "memory_ids"])
def test_background_basis_changes_invalidate_private_draft(field):
    before = {"profile": {}, "actor_revision": 1, field: "before"}
    assert basis_digest(before) != basis_digest({**before, field: "after"})


def test_changed_memory_contents_invalidate_even_when_ids_are_stable():
    info = {"actor_revision": 1, "memory_ids": ["same-id"]}
    before = [{"role": "user", "content": "memory-before"}]
    after = [{"role": "user", "content": "memory-after"}]
    assert basis_digest(info, before) != basis_digest(info, after)


@pytest.mark.parametrize("accepted", [True, False])
async def test_combined_audio_remains_private_until_review(actor_case, fake, accepted):
    intent = await prepare(actor_case, fake)
    audio_started, review_started = asyncio.Event(), asyncio.Event()
    original = fake.chat

    async def chat(task_id, role, messages, **kwargs):
        if role == "actor_review":
            review_started.set()
            await asyncio.wait_for(audio_started.wait(), 2)
            return {"accepted": accepted, "issues": [] if accepted else ["无据断言"]}
        return await original(task_id, role, messages, **kwargs)

    async def audio(task, text, voice, direction):
        audio_started.set()
        await asyncio.wait_for(review_started.wait(), 2)
        return CandidateAudio(text, voice, direction, "test-scope", b"\x00\x00")

    fake.chat = chat
    discard = AsyncMock()
    call = perform(actor_case, fake, intent,
                   voice_service=SimpleNamespace(stream_candidate=audio, discard_candidate=discard),
                   voice="Cherry")
    if accepted:
        result = await call
        assert result["_candidate_audio"].text == "你好。"
        assert result["_audio_status"] == "ready"
        discard.assert_not_awaited()
    else:
        with pytest.raises(UnsafeDraftExhausted):
            await call
        assert discard.await_count == 1


async def test_cached_joint_operation_is_bound_to_original_basis(actor_case, fake):
    await prepare(actor_case, fake)
    first = fake.calls[0][2]["operation"]
    with actor_case.services.db.session.begin() as s:
        actor = s.get(Conversation, actor_case.actor.id)
        actor.privacy_version += 1
    await prepare(actor_case, fake)
    assert fake.calls[-1][2]["operation"] != first


async def test_joint_operation_changes_when_authorized_assignment_changes(actor_case, fake):
    await prepare(actor_case, fake)
    first = fake.calls[-1][2]["operation"]
    actor_case.assignment["objective"] = "询问访客愿意先谈公告还是船班"
    await prepare(actor_case, fake)
    assert fake.calls[-1][2]["operation"] != first


async def test_review_is_bound_to_actual_director_target_without_extra_model_call(actor_case, fake):
    import json

    intent = await prepare(actor_case, fake)
    await perform(actor_case, fake, intent)
    first = fake.calls[-1]
    requirement = json.loads(first[1][-1]["content"])["authorized_performance_requirement"]
    assert requirement["objective"] == actor_case.assignment["objective"]
    # An authorized target correction must not retrieve the old approval even
    # if the actor emits exactly the same wording and intention again.
    actor_case.assignment["objective"] = "询问访客是否先谈公告"
    intent = await prepare(actor_case, fake)
    await perform(actor_case, fake, intent)
    assert fake.calls[-1][2]["operation"] != first[2]["operation"]
    assert [role for role, _, _ in fake.calls] == ["dialogue", "actor_review", "dialogue", "actor_review"]


@pytest.mark.parametrize("failure", ["semantic", "local"])
@pytest.mark.parametrize("actor_case", ["便条上写了什么？"], indirect=True)
async def test_grounded_reply_repairs_instead_of_publishing_pruned_mood(actor_case, fake, monkeypatch, failure):
    import json

    actor_case.assignment.update(objective="回应访客的当前问题", response_turn_policy="answer_only", response_utterance={
        "event_id": actor_case.task.payload["event_id"], "speaker_id": actor_case.turn.player_id,
        "text": actor_case.task.payload["text"],
    })
    intent = await prepare(actor_case, fake)
    rejected = "我之前每天都在这里等你。" if failure == "semantic" else "便条我没看过逐字内容。"
    parts = [rejected, "我先歇一会儿。"]
    intent["_speech_draft"]["speech"] = {"text": "\n\n".join(parts), "spoken_parts": parts, "claims": []}

    def forbidden(*args):
        raise AssertionError("A grounded task cannot use whole-part deletion as its response repair")

    monkeypatch.setattr("chatbot.npc.speech_pruning.prune_rejected_parts", forbidden)
    monkeypatch.setattr("chatbot.npc.speech_pruning.prune_local_fact_parts", forbidden)
    original = fake.chat
    reviews = 0

    async def chat(task_id, role, messages, **kwargs):
        nonlocal reviews
        if role == "actor_review":
            reviews += 1
            if failure == "semantic" and reviews == 1:
                return {"accepted": False, "issues": ["无据过去经历"], "checks": [
                    {"text": rejected, "supported": False, "reason_code": "invented_history",
                     "basis": "没有这段过去经历的独立依据", "source_ids": []}]}
            # The existing review still receives the actual task after repair.
            requirement = json.loads(messages[-1]["content"])["authorized_performance_requirement"]
            assert requirement["response_utterance"]["event_id"] == actor_case.task.payload["event_id"]
        return await original(task_id, role, messages, **kwargs)

    fake.chat = chat
    repaired = "便条上写了什么，我现在不知道。"
    result = await perform(actor_case, fake, intent, repair_text=repaired)
    assert result["answer"] == repaired and result["answer"] != parts[1]
    assert [role for role, _, _ in fake.calls].count("dialogue") == 2
    assert reviews == (2 if failure == "semantic" else 1)


@pytest.mark.parametrize("actor_case,answer", [
    ("你好。", "你好。"),
    ("便条写了什么？", "便条上的文字，我现在不知道。"),
    ("能讲讲你的私人经历吗？", "这件事我不愿意谈。"),
], indirect=["actor_case"])
async def test_grounded_reply_allows_answer_unknown_or_refusal_without_extra_call(actor_case, fake, answer):
    actor_case.assignment.update(objective="回应访客的当前发言", response_turn_policy="answer_only", response_utterance={
        "event_id": actor_case.task.payload["event_id"], "speaker_id": actor_case.turn.player_id,
        "text": actor_case.task.payload["text"],
    })
    intent = await prepare(actor_case, fake)
    intent["_speech_draft"]["speech"] = speech(answer)
    result = await perform(actor_case, fake, intent)
    assert result["answer"] == answer
    assert [role for role, _, _ in fake.calls] == ["dialogue", "actor_review"]
