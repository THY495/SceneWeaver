"""Recover task/turn conflicts without replacing factual review with heuristics."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from support.story import intention
from test_combined_actor_generation import actor_case as actor_case
from test_combined_actor_generation import speech
from test_director_recovery_feedback import recovery_case as recovery_case
from test_grounded_assignment import authorize, install

from chatbot.agents import Agents
from chatbot.directing import DirectorPlan, PerformanceJob
from chatbot.director_state import install_plan
from chatbot.npc.context import NPCServices
from chatbot.npc.directed_scene import execute_recoverable
from chatbot.npc.director import snapshot
from chatbot.npc.performance import ResponseContractConflict, perform_actor, requires_contract_replanning
from chatbot.observability.timing import TimingTrace
from chatbot.storage import ExperimentInitial, Operation, SceneRecord, Story
from chatbot.voice_candidate import CandidateAudio


def clean_review(player):
    return {"accepted": True, "issues": [], "checks": [],
        "delivery": {"addressee_id": player, "expects_reply": True, "response_kind": "answer"}}


def test_real_e_review_and_intent_replay_requires_task_recovery():
    # Actual first full review, not a newly invented verdict or paid rerun.
    case = json.loads((Path(__file__).parent / "fixtures/response-contract-conflict-e.json").read_text("utf-8"))
    review = case["review"]
    assert review["accepted"] and not review["issues"] and all(c["supported"] for c in review["checks"])
    assert ":review:1:" in case["review_operation"]
    player = review["delivery"]["addressee_id"]
    result = {**review, "provider_semantic_clean": True,
        "scheduling_issues": ["response_contract_reply_mismatch"]}
    assignment = {"response_utterance": {"text": "先只问我是否先聊公告"},
        "response_turn_policy": "answer_only", "addressees": [player]}
    assert requires_contract_replanning(result, case["intent"], assignment, player)
    # Private reviewer prose is irrelevant to, and never becomes, the hint.
    result["checks"] = deepcopy(result["checks"])
    result["checks"][0]["basis"] = "private-review-sentinel"
    assert requires_contract_replanning(result, case["intent"], assignment, player)


async def setup(case, fake):
    case.services.config.role_assignment_authorization = "grounded_response"
    job, _ = install(case)
    assignment = await authorize(case, job)
    fake.replies.update(dialogue=speech("那你现在要先聊公告吗？"), actor_review=clean_review(case.turn.player_id))
    return assignment, intention(case.turn.player_id, expects_reply=True)


async def test_clean_contract_conflict_stops_after_one_full_review_without_publication(actor_case, fake):
    case = actor_case
    assignment, intent = await setup(case, fake)
    with case.services.db.session() as s:
        before = s.scalar(select(func.count()).select_from(SceneRecord))
    trace = TimingTrace(case.task)
    with trace.activate(), pytest.raises(ResponseContractConflict):
        await perform_actor(case.services, case.task, case.turn, case.actor, intent, 0,
                            director_assignment=assignment)
    assert [role for role, _, _ in fake.calls] == ["dialogue", "actor_review"]
    assert any(span["name"] == "npc.response_contract_recovery" for span in trace.spans)
    with case.services.db.session() as s:
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == before
        assert not s.get(Operation, f"{case.task.id}:committed:0")


@pytest.mark.parametrize("change", ["issue", "unsupported", "rejected", "intent_not_question", "missing_delivery",
                                        "wrong_target", "open_contract", "invite_contract"])
async def test_other_rejections_keep_speech_repair_or_normal_review(actor_case, fake, change):
    case = actor_case
    assignment, intent = await setup(case, fake)
    first = clean_review(case.turn.player_id)
    if change == "issue":
        first["issues"] = ["private-factual-issue"]
    elif change == "unsupported":
        first["checks"] = [{"text": "待核查", "supported": False, "basis": "private-basis"}]
    elif change == "rejected":
        first["accepted"] = False
    elif change == "intent_not_question":
        intent["expects_reply"] = False
    elif change == "missing_delivery":
        first.pop("delivery")
    elif change == "wrong_target":
        first["delivery"]["addressee_id"] = case.turn.actor_ids[1]
    else:
        assignment["response_turn_policy"] = "open" if change == "open_contract" else "invite_player"
    reviews = 0

    def review(messages):
        nonlocal reviews
        reviews += 1
        if reviews == 1:
            return first
        return {**clean_review(case.turn.player_id),
                "delivery": {"addressee_id": case.turn.player_id, "expects_reply": False, "response_kind": "answer"}}

    fake.replies["actor_review"] = review
    result = await perform_actor(case.services, case.task, case.turn, case.actor, intent, 0,
                                 director_assignment=assignment)
    assert result["answer"]
    assert reviews == (1 if change in {"open_contract", "invite_contract"} else 2)


@pytest.mark.parametrize("state", ["ready", "inflight"])
async def test_conflict_discards_ready_audio_or_cancels_inflight_audio(actor_case, fake, state):
    case = actor_case
    assignment, intent = await setup(case, fake)
    started, cancelled = asyncio.Event(), asyncio.Event()
    candidate = CandidateAudio("那你现在要先聊公告吗？", "Cherry", "", "private", b"\x00\x00")

    async def audio(*args):
        started.set()
        if state == "inflight":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return candidate

    original = fake.chat

    async def chat(task_id, role, messages, **kwargs):
        if role == "actor_review":
            await asyncio.wait_for(started.wait(), 2)
        return await original(task_id, role, messages, **kwargs)

    fake.chat = chat
    discard = AsyncMock()
    with pytest.raises(ResponseContractConflict):
        await perform_actor(case.services, case.task, case.turn, case.actor, intent, 0,
            director_assignment=assignment, voice="Cherry",
            voice_service=SimpleNamespace(stream_candidate=audio, discard_candidate=discard))
    assert [role for role, _, _ in fake.calls] == ["dialogue", "actor_review"]
    if state == "ready":
        discard.assert_awaited_once_with(candidate)
    else:
        assert cancelled.is_set()
        discard.assert_not_awaited()


async def test_actual_directed_recovery_reauthorizes_reviews_and_commits_new_task(recovery_case, fake, config):
    c = recovery_case
    config.role_assignment_authorization = "grounded_response"
    with c.db.session.begin() as s:
        origin = s.get(ExperimentInitial, c.turn.story_id)
        recipe = deepcopy(origin.recipe)
        recipe["narrative"]["nodes"][0]["direction"]["beats"][0]["source_response_tasks"] = True
        origin.recipe = recipe
    job = PerformanceJob(id="answer_only", actor=c.actor.id, addressees=[c.turn.player_id],
                         grounded_response={"event_id": c.task.payload["event_id"], "delivery": "brief"})
    with c.db.session() as s:
        seq = s.get(Story, c.turn.story_id).seq
    ledger = install_plan(c.db, c.task, c.turn.story_id, seq,
        DirectorPlan(beat_id="beat", reason="回应", jobs=[job]), expected_revision=c.ledger["revision"])
    review = clean_review(c.turn.player_id)
    review["checks"] = [{"text": "你想先聊公告吗？", "supported": True, "basis": "private-basis-sentinel"}]
    fake.replies.update(director=c.proposal, dialogue=speech("你想先聊公告吗？"),
        role_intent=intention(c.turn.player_id, expects_reply=True), actor_review=review,
        director_assignment={"accepted": True, "checks": [], "issues": []})
    agents = Agents(c.db, config, c.services.runtime, fake)
    services = NPCServices(c.db, config, c.services.runtime, fake, agents.context, agents.knowledge)
    with c.db.session() as s:
        before = snapshot(s, c.task, c.turn)
    updated, result = await execute_recoverable(services, c.task, c.turn, ledger,
        SimpleNamespace(prepare_recall=AsyncMock()), before)
    assert updated["recovery_count"] == 1
    assert result["stop_reason"] == "player_turn" and len(result["segments"]) == 1
    roles = [role for role, _, _ in fake.calls]
    assert roles.count("actor_review") == 2  # rejected old task, accepted new task
    assert roles.count("dialogue") == 2 and roles.count("director") == 1
    assert roles.count("director_assignment") == 1  # new free task still authorized
    messages = next(messages for role, messages, _ in fake.calls if role == "director")
    payload = json.loads(messages[-1]["content"])
    assert payload["recovery_hint"] == "answer_only_with_intended_player_question"
    assert "private-basis-sentinel" not in json.dumps(payload)
    with c.db.session() as s:
        formal = list(s.scalars(select(SceneRecord).where(SceneRecord.task_id == c.task.id,
            SceneRecord.kind == "speech", SceneRecord.actor_id == c.actor.id)))
        assert len(formal) == 1 and formal[0].id == result["segments"][0]["event_id"]


@pytest.mark.parametrize("blocked", ["recovery_used", "cancelled", "scene_changed"])
async def test_conflict_cannot_bypass_existing_recovery_limits(recovery_case, monkeypatch, blocked):
    from chatbot.npc import directed_scene
    from chatbot.runtime import StaleTask

    c = recovery_case
    with c.db.session() as s:
        before = snapshot(s, c.task, c.turn)
    failure = ResponseContractConflict("private-review-sentinel")
    monkeypatch.setattr(directed_scene, "execute_plan", AsyncMock(side_effect=failure))
    plan = AsyncMock()
    monkeypatch.setattr(directed_scene, "prepare_scene_plan", plan)
    if blocked == "recovery_used":
        c.ledger["recovery_count"] = 1
    elif blocked == "cancelled":
        monkeypatch.setattr(c.services.runtime, "check", lambda *args: (_ for _ in ()).throw(StaleTask("cancelled")))
    else:
        monkeypatch.setattr(directed_scene, "snapshot", lambda *args: SimpleNamespace(node_id="other"))
    with pytest.raises((ResponseContractConflict, StaleTask)):
        await execute_recoverable(c.services, c.task, c.turn, c.ledger, None, before)
    plan.assert_not_awaited()


def test_other_scheduling_failures_cannot_use_contract_recovery():
    player = "player"
    review = {**clean_review(player), "provider_semantic_clean": True,
        "scheduling_issues": ["response_contract_reply_mismatch", "response_task_not_addressed"]}
    assignment = {"response_utterance": {"text": "你好"}, "response_turn_policy": "answer_only", "addressees": [player]}
    assert not requires_contract_replanning(review, intention(player, expects_reply=True), assignment, player)
