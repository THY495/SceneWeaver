"""Cross-turn reuse must carry actual dialogue dependencies without replaying speech."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import launch

from chatbot.agents import Agents
from chatbot.api import create_app
from chatbot.directing import DirectorPlan
from chatbot.director_state import install_plan
from chatbot.event_projection import requests_for
from chatbot.npc.beat_reuse import frame_key, remember_frame
from chatbot.npc.context import NPCServices, TurnContext
from chatbot.npc.directed_scene import remaining_in_beat
from chatbot.npc.directed_turn import execute_plan
from chatbot.npc.director import prepare_scene_plan, snapshot
from chatbot.npc.exchange_requirement import beat_committed_event_ids
from chatbot.observations import append_record
from chatbot.storage import (
    Conversation,
    ExperimentInitial,
    Observation,
    Operation,
    RequestTransition,
    SceneRecord,
    Story,
    Task,
)


@pytest.fixture
async def pending_beat(db, config, runtime, fake, monkeypatch, request):
    config.product, config.role_director_beat_reuse = "roleplay", True
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    with db.session.begin() as s:
        story = s.get(Story, sid)
        story.settings = {**story.settings, "narrative": {"node": "room", "event_id": "entry"}}
        s.add(ExperimentInitial(story_id=sid, owner_id=config.workspace_owner, request_id="reuse",
              request_hash="reuse", receipt={"actors": {"a": actors[0], "b": actors[1]}}, recipe={
                  "people": [], "narrative": {"nodes": [{"id": "room", "direction": {"objective": "讨论查阅公告",
                    "beats": [{"id": "beat", "title": "交谈", "objective": "先询问再交换看法",
                    "participants": ["a", "b"], "completion": "玩家回答后两人交流完毕",
                    "source_response_tasks": getattr(request, "param", False) is True,
                    "handoff": "on_question", "max_segments": 3, "npc_exchange_required": True}]}}]}}))
    runtime.submit(player, config.workspace_owner, "想听听你们的看法。", "first")
    task = runtime.claim("test")
    turn = TurnContext(sid, player, tuple(actors))
    plan = DirectorPlan(beat_id="beat", reason="先问访客，再让修复师回应馆长", jobs=[
        {"id": "ask", "actor": actors[0], "addressees": [player], "objective": "询问是否先讨论公告"},
        {"id": "answer", "actor": actors[1], "addressees": [actors[0]],
         "objective": "回应馆长与访客实际说过的话，表达自己的看法", "depends_on": ["ask"]},
    ])
    if getattr(request, "param", False):
        plan = DirectorPlan(beat_id="beat", reason="先询问玩家，再沿真实发言依赖继续交流", jobs=[
            plan.jobs[0],
            {"id": "answer", "actor": actors[1], "addressees": [actors[0]],
             "grounded_response": {"job_id": "ask", "delivery": "brief"}, "depends_on": ["ask"]},
            {"id": "close", "actor": actors[0], "addressees": [actors[1]],
             "grounded_response": {"job_id": "answer", "delivery": "brief"}, "depends_on": ["answer"]},
        ])
    with db.session() as s:
        seq = s.get(Story, sid).seq
    ledger = install_plan(db, task, sid, seq, plan, handoff_policy="on_question", segment_capacity=3)
    agents = Agents(db, config, runtime, fake)
    services = NPCServices(db, config, runtime, fake, agents.context, agents.knowledge)

    async def perform(services, task, turn, actor, intent, step, **kwargs):
        return {"answer": "要先谈公告吗？", "revision": actor.revision, "evidence": []}

    monkeypatch.setattr("chatbot.npc.directed_turn.authorize_assignment", AsyncMock(return_value={
        "objective": "询问", "addressees": [player]}))
    monkeypatch.setattr("chatbot.npc.directed_turn.plan_actor", AsyncMock(return_value={
        "act": "speak", "purpose": "询问", "addressee_id": player, "expects_reply": True,
        "awaiting_player_action": True, "wait_for_player": True}))
    monkeypatch.setattr("chatbot.npc.directed_turn.perform_actor", perform)
    result = await execute_plan(services, task, turn, ledger, None)
    assert result["stop_reason"] == "player_turn" and not result["director_plan_complete"]
    with db.session() as s:
        question = next(r["question_id"] for r in requests_for(s, player) if r["target_id"] == player)
    result["pending_player_question_ids"] = [question]
    remember_frame(services, task, turn, result)
    runtime.finish(task.id, task.lease_version, result)
    runtime.submit(player, config.workspace_owner, "是的，先谈公告。", "second")
    current = runtime.claim("test")
    fake.replies["director_reuse"] = {"decision": "resume", "response_kind": "answer",
        "player_quote": "先谈公告", "question_quote": "先谈公告", "reason": "玩家明确回答了当前问题"}
    fake.replies["director"] = {"beat_id": "beat", "jobs": [], "after_plan": "yield_player",
                                "remaining_work": [], "reason": "重规划"}
    return SimpleNamespace(services=services, task=current, turn=turn, question=question,
                           source=task, actors=actors, result=result)


async def test_direct_answer_resumes_suffix_without_new_director_or_old_candidates(pending_beat, db, fake):
    case = pending_beat
    ledger = await prepare_scene_plan(case.services, case.task, case.turn)
    assert [job["id"] for job in ledger["plan"]["jobs"]] == ["answer"]
    assert ledger["plan"]["jobs"][0]["depends_on"] == []
    assert ledger["candidates"] == {}
    assert ledger["inherited_event_ids"] == [case.question]
    assert [role for role, _, _ in fake.calls] == ["director_reuse"]
    with db.session() as s:
        assert s.get(Operation, frame_key(case.turn.story_id)).payload["state"] == "consumed"
        assert s.get(RequestTransition, (case.task.payload["event_id"], case.question)).status == "answer"
        assert beat_committed_event_ids(s, case.task.id, "beat") == {case.question}
        beat = snapshot(s, case.task, case.turn).direction.beats[0]
        exchanges = json.loads(snapshot(s, case.task, case.turn).public_context)["exchanges"]
        assert next(e for e in exchanges if e["question_id"] == case.question)["status"] == "answer"
        assert s.get(SceneRecord, case.task.payload["event_id"]).payload.get("exchange", {}).get("reply_to") is None
    assert remaining_in_beat(db, case.task.id, beat) == 2


async def test_old_turn_invitation_cannot_be_reused_after_new_player_input(pending_beat, db, fake):
    case = pending_beat
    with db.session.begin() as s:
        row = s.get(Operation, frame_key(case.turn.story_id))
        data = dict(row.payload)
        job = {**data["plan"]["jobs"][0], "objective": "", "delivery_constraints": [],
               "addressees": [case.turn.player_id], "grounded_response": {
                   "purpose": "invite_player", "event_id": case.source.payload["event_id"],
               }}
        data["plan"] = {**data["plan"], "jobs": [job]}
        row.payload = data
    await prepare_scene_plan(case.services, case.task, case.turn)
    assert [role for role, _, _ in fake.calls] == ["director"]


@pytest.mark.parametrize("pending_beat", [True], indirect=True)
async def test_typed_dependencies_survive_player_handoff_and_combined_generation(
    pending_beat, db, fake, monkeypatch
):
    from support.story import intention

    from chatbot.npc.assignment import authorize_assignment
    from chatbot.npc.performance import perform_actor
    from chatbot.npc.planning import plan_actor
    from chatbot.role_memory import RoleMemoryService

    case = pending_beat
    case.services.config.role_assignment_authorization = "grounded_response"
    case.services.config.role_actor_generation = "combined_context"
    ledger = await prepare_scene_plan(case.services, case.task, case.turn)
    first, second = ledger["plan"]["jobs"]
    assert first["grounded_response"]["event_id"] == case.question
    assert first["grounded_response"].get("job_id") is None and first["depends_on"] == []
    assert second["grounded_response"]["job_id"] == "answer" and second["depends_on"] == ["answer"]
    monkeypatch.setattr("chatbot.npc.directed_turn.authorize_assignment", authorize_assignment)
    monkeypatch.setattr("chatbot.npc.directed_turn.plan_actor", plan_actor)
    monkeypatch.setattr("chatbot.npc.directed_turn.perform_actor", perform_actor)
    targets = iter(case.actors)

    def draft(messages):
        target = next(targets)
        text = "可以，先谈公告吧。"
        return {"intent": intention(target), "speech": {"text": text, "claims": [],
                "spoken_parts": [text], "performance": []}}

    def review(messages):
        target = json.loads(messages[-1]["content"])["intent"]["addressee_id"]
        return {"accepted": True, "checks": [], "issues": [], "delivery": {
            "addressee_id": target, "expects_reply": False, "requires_player_action": False,
            "response_kind": "answer", "remaining_request": None}}

    fake.replies["dialogue"], fake.replies["actor_review"] = draft, review
    memory = RoleMemoryService(db, case.services.config, case.services.runtime, fake)
    result = await execute_plan(case.services, case.task, case.turn, ledger, memory)
    assert len(result["segments"]) == 2 and result["director_plan_complete"]
    assert [role for role, _, _ in fake.calls] == ["director_reuse", "dialogue", "actor_review", "dialogue", "actor_review"]
    with db.session() as s:
        records = list(s.scalars(select(SceneRecord).where(SceneRecord.story_id == case.turn.story_id,
                    SceneRecord.kind == "speech", SceneRecord.actor_id.in_(case.actors))))
        assert len(records) == 3 and sum(row.id == case.question for row in records) == 1
        audits = list(s.scalars(select(Operation).where(Operation.task_id == case.task.id,
                      Operation.id.contains("assignment-grounded-v1"))))
        assert {row.payload["source_event_id"] for row in audits} == {
            case.question, result["segments"][0]["event_id"]}


@pytest.mark.parametrize("pending_beat", ["legacy_source"], indirect=True)
async def test_old_source_suffix_without_author_permission_skips_reuse_model(pending_beat, fake):
    case = pending_beat
    with case.services.db.session() as s:
        frame = s.get(Operation, frame_key(case.turn.story_id))
        assert any(job.get("grounded_response") for job in frame.payload["plan"]["jobs"])
    result = await prepare_scene_plan(case.services, case.task, case.turn)
    assert result["plan"]["jobs"] == []
    assert [role for role, _, _ in fake.calls] == ["director"]


def mutate_typed_source(db, case, change):
    with db.session.begin() as s:
        ledger = s.get(Operation, f"{case.source.id}:director-ledger")
        if change in {"candidate_revision", "candidate_status", "candidate_event", "job"}:
            payload = json.loads(json.dumps(ledger.payload))
            if change == "job":
                payload["plan"]["jobs"][1]["grounded_response"]["delivery"] = "natural"
            else:
                candidate = payload["candidates"]["ask"]
                if change == "candidate_revision":
                    candidate["revision"] += 1
                elif change == "candidate_status":
                    candidate["status"] = "rejected"
                else:
                    candidate["event_id"] = case.task.payload["event_id"]
            ledger.payload = payload
        elif change == "ledger_deleted":
            s.delete(ledger)
        elif change == "commit":
            committed = s.get(Operation, f"{case.source.id}:committed:0")
            committed.payload = {**committed.payload, "actor_id": case.actors[1]}
        elif change in {"perception", "shared_text", "shared_identity"}:
            query = select(Observation).where(Observation.event_id == case.question)
            if change == "perception":
                query = query.where(Observation.actor_id == case.actors[1])
            for observation in s.scalars(query):
                update = {"speaker_known": False} if change == "shared_identity" else {"text": "只听见末尾。"}
                observation.perception = {**observation.perception, **update}
        else:
            event = s.get(SceneRecord, case.question)
            if change == "text":
                event.text = "要改谈另一件事吗？"
            elif change == "identity":
                event.actor_id = case.actors[1]
            elif change == "private":
                event.channel = "private"
            elif change == "redacted":
                event.redacted = True
            else:
                event.kind = "fact"


@pytest.mark.parametrize("pending_beat", [True], indirect=True)
@pytest.mark.parametrize("change", ["candidate_revision", "candidate_status", "candidate_event",
    "job", "ledger_deleted", "commit", "text", "identity", "private", "redacted", "kind", "perception",
    "shared_text", "shared_identity"])
async def test_changed_typed_source_replans_without_reply_link(pending_beat, db, fake, change):
    case = pending_beat
    mutate_typed_source(db, case, change)
    await prepare_scene_plan(case.services, case.task, case.turn)
    assert [role for role, _, _ in fake.calls] == ["director"]
    with db.session() as s:
        assert s.get(RequestTransition, (case.task.payload["event_id"], case.question)) is None
        assert s.get(Operation, f"{case.task.id}:beat-reuse-link") is None


@pytest.mark.parametrize("pending_beat", [True], indirect=True)
@pytest.mark.parametrize("change", ["candidate_revision", "commit", "perception"])
async def test_typed_source_change_during_gate_cannot_install_or_link(pending_beat, db, fake, change):
    from chatbot.runtime import StaleTask

    case, original = pending_beat, fake.chat

    async def chat(*args, **kwargs):
        verdict = await original(*args, **kwargs)
        mutate_typed_source(db, case, change)
        return verdict

    fake.chat = chat
    with pytest.raises(StaleTask):
        await prepare_scene_plan(case.services, case.task, case.turn)
    with db.session() as s:
        assert s.get(Operation, frame_key(case.turn.story_id)).payload["state"] == "ready"
        assert s.get(Operation, f"{case.task.id}:director-ledger") is None
        assert s.get(Operation, f"{case.task.id}:beat-reuse-link") is None
        assert s.get(RequestTransition, (case.task.payload["event_id"], case.question)) is None


@pytest.mark.parametrize("decision,kind,quote", [("uncertain", None, "先谈公告"),
    ("replan", "refuse", "先谈公告"), ("resume", "answer", "并不存在的原文")])
async def test_uncertain_or_refusal_or_bad_evidence_replans_without_resolving_question(
    pending_beat, db, fake, decision, kind, quote
):
    case = pending_beat
    fake.replies["director_reuse"].update(decision=decision, response_kind=kind, player_quote=quote)
    ledger = await prepare_scene_plan(case.services, case.task, case.turn)
    assert ledger["inherited_event_ids"] == []
    assert [role for role, _, _ in fake.calls] == ["director_reuse", "director"]
    with db.session() as s:
        assert s.get(RequestTransition, (case.task.payload["event_id"], case.question)) is None


@pytest.mark.parametrize("change", ["fact", "private", "npc_revision", "scene", "author", "privacy"])
async def test_changed_world_or_scope_skips_reuse_gate(pending_beat, db, fake, change):
    case = pending_beat
    with db.session.begin() as s:
        story = s.get(Story, case.turn.story_id)
        actor = s.get(Conversation, case.actors[0])
        if change in {"fact", "private"}:
            append_record(s, story_id=story.id, kind="fact" if change == "fact" else "speech",
                          text="新的变化", actor=actor, observer_ids=case.actors if change == "fact" else [actor.id],
                          channel="public" if change == "fact" else "private", dedupe="change")
        elif change == "npc_revision":
            actor.revision += 1
        elif change == "privacy":
            actor.privacy_version += 1
        elif change == "scene":
            story.settings = {**story.settings, "narrative": {**story.settings["narrative"], "event_id": "new-entry"}}
        else:
            origin = s.get(ExperimentInitial, story.id)
            recipe = json.loads(json.dumps(origin.recipe))
            recipe["narrative"]["nodes"][0]["direction"]["constraints"] = ["新作者约束"]
            origin.recipe = recipe
    await prepare_scene_plan(case.services, case.task, case.turn)
    assert [role for role, _, _ in fake.calls] == ["director"]


async def test_change_during_gate_cannot_consume_frame_or_link_answer(pending_beat, db, fake):
    from chatbot.runtime import StaleTask

    case = pending_beat
    original = fake.chat

    async def chat(*args, **kwargs):
        result = await original(*args, **kwargs)
        with db.session.begin() as s:
            s.get(Conversation, case.actors[0]).privacy_version += 1
        return result

    fake.chat = chat
    with pytest.raises(StaleTask):
        await prepare_scene_plan(case.services, case.task, case.turn)
    with db.session() as s:
        assert s.get(Operation, frame_key(case.turn.story_id)).payload["state"] == "ready"
        assert s.get(RequestTransition, (case.task.payload["event_id"], case.question)) is None
        assert s.get(Operation, f"{case.task.id}:director-ledger") is None


async def test_failed_source_cannot_be_resumed(pending_beat, db, fake):
    with db.session.begin() as s:
        source = s.get(Task, pending_beat.source.id)
        source.status = "failed"
    await prepare_scene_plan(pending_beat.services, pending_beat.task, pending_beat.turn)
    assert [role for role, _, _ in fake.calls] == ["director"]


async def test_completed_but_paused_source_cannot_be_resumed(pending_beat, db, fake):
    with db.session.begin() as s:
        source = s.get(Task, pending_beat.source.id)
        source.result = {**source.result, "stop_reason": "paused"}
    await prepare_scene_plan(pending_beat.services, pending_beat.task, pending_beat.turn)
    assert [role for role, _, _ in fake.calls] == ["director"]


async def test_malformed_gate_falls_back_without_linking_player_response(pending_beat, db, fake):
    fake.replies["director_reuse"] = {"decision": "resume"}
    await prepare_scene_plan(pending_beat.services, pending_beat.task, pending_beat.turn)
    assert [role for role, _, _ in fake.calls] == ["director_reuse", "director"]
    with db.session() as s:
        assert s.get(RequestTransition, (pending_beat.task.payload["event_id"], pending_beat.question)) is None


async def test_gate_budget_exhaustion_does_not_start_another_model(pending_beat, fake):
    from chatbot.providers import BudgetExceeded

    calls = []

    async def exhausted(*args, **kwargs):
        calls.append(args[1])
        raise BudgetExceeded("测试预算已耗尽")

    fake.chat = exhausted
    with pytest.raises(BudgetExceeded):
        await prepare_scene_plan(pending_beat.services, pending_beat.task, pending_beat.turn)
    assert calls == ["director_reuse"]


async def test_private_but_common_input_still_requires_fresh_direction(pending_beat, db, fake):
    case = pending_beat
    with db.session.begin() as s:
        task = s.get(Task, case.task.id)
        task.payload = {**task.payload, "channel": "private"}
        s.get(SceneRecord, task.payload["event_id"]).channel = "private"
    case.task.payload = {**case.task.payload, "channel": "private"}
    await prepare_scene_plan(case.services, case.task, case.turn)
    assert [role for role, _, _ in fake.calls] == ["director"]


async def test_lineage_survives_recovery_and_another_player_handoff(pending_beat, db, runtime, fake, monkeypatch):
    case = pending_beat
    first = await prepare_scene_plan(case.services, case.task, case.turn)
    # A recovery after a newly committed NPC exchange must carry that exchange
    # as well as the inherited question from the previous player round.
    monkeypatch.setattr("chatbot.npc.directed_turn.plan_actor", AsyncMock(return_value={
        "act": "speak", "purpose": "回应馆长", "addressee_id": case.actors[0], "expects_reply": False}))
    performed = await execute_plan(case.services, case.task, case.turn, first, None)
    middle_event = performed["segments"][0]["event_id"]
    monkeypatch.setattr("chatbot.npc.directed_turn.plan_actor", AsyncMock(return_value={
        "act": "speak", "purpose": "询问", "addressee_id": case.turn.player_id, "expects_reply": True,
        "awaiting_player_action": True, "wait_for_player": True}))
    with db.session.begin() as s:
        initial = s.get(ExperimentInitial, case.turn.story_id)
        recipe = json.loads(json.dumps(initial.recipe))
        recipe["narrative"]["nodes"][0]["direction"]["beats"][0]["max_segments"] = 5
        initial.recipe = recipe
    replacement = DirectorPlan(beat_id="beat", reason="仍需玩家补充", jobs=[
        {"id": "ask-again", "actor": case.actors[0], "addressees": [case.turn.player_id],
         "objective": "询问玩家本次意见"},
        {"id": "close", "actor": case.actors[1], "addressees": [case.actors[0]],
         "objective": "回应对方实际说过的话", "depends_on": ["ask-again"]},
    ])
    with db.session() as s:
        seq = s.get(Story, case.turn.story_id).seq
    recovered = install_plan(db, case.task, case.turn.story_id, seq, replacement,
                             expected_revision=first["revision"], recovery_reason="draft_rejected",
                             handoff_policy="on_question", segment_capacity=3)
    assert recovered["inherited_event_ids"] == [case.question, middle_event]
    result = await execute_plan(case.services, case.task, case.turn, recovered, None)
    with db.session() as s:
        second_question = next(r["question_id"] for r in requests_for(s, case.turn.player_id)
                               if r["target_id"] == case.turn.player_id and r["status"] == "open")
    result["pending_player_question_ids"] = [second_question]
    remember_frame(case.services, case.task, case.turn, result)
    runtime.finish(case.task.id, case.task.lease_version, result)
    runtime.submit(case.turn.player_id, case.task.owner_id, "是的，先谈公告。", "third")
    third = runtime.claim("test")
    next_plan = await prepare_scene_plan(case.services, third, case.turn)
    assert set(next_plan["inherited_event_ids"]) == {case.question, middle_event, second_question}
    assert [job["id"] for job in next_plan["plan"]["jobs"]] == ["close"]
    with db.session() as s:
        beat = snapshot(s, third, case.turn).direction.beats[0]
    assert remaining_in_beat(db, third.id, beat) == 2
