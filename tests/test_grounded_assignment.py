"""Typed tasks reduce authorization calls without promoting utterances to facts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_combined_actor_generation import actor_case as actor_case

from chatbot.directing import DirectorPlan, PerformanceJob
from chatbot.director_state import install_plan
from chatbot.npc.assignment import AssignmentRejected, authorize_assignment
from chatbot.npc.directed_turn import execute_plan
from chatbot.npc.grounded_assignment import compile_response, record_authorization
from chatbot.npc.performance import ResponseContractConflict, UnsafeDraftExhausted
from chatbot.observations import append_record
from chatbot.runtime import StaleTask
from chatbot.storage import Conversation, ExperimentInitial, Operation, SceneRecord, Story


def install(case, *, event_id=None, jobs=None):
    job = PerformanceJob(
        id="response",
        actor=case.actor.id,
        addressees=[case.turn.player_id],
        grounded_response={"event_id": event_id or case.task.payload["event_id"], "delivery": "brief"},
    )
    with case.services.db.session.begin() as s:
        story = s.get(Story, case.turn.story_id)
        seq = story.seq
        if not s.get(ExperimentInitial, story.id):
            story.settings = {**story.settings, "narrative": {"node": "test-node", "event_id": "entry"}}
            s.add(ExperimentInitial(story_id=story.id, owner_id=story.owner_id,
                request_id=story.id, request_hash="0" * 64,
                recipe={"people": [], "narrative": {"nodes": [{"id": "test-node", "direction": {
                    "objective": "回应真实言论", "beats": [{"id": "b", "title": "回应",
                    "objective": "回应真实言论", "completion": "交还玩家",
                    "source_response_tasks": True, "participants": list(case.turn.actor_ids)}]}}]}},
                receipt={"actors": {a: a for a in case.turn.actor_ids}}))
        old = s.get(Operation, f"{case.task.id}:director-ledger")
        revision = old.payload["revision"] if old else 0
    ledger = install_plan(
        case.services.db,
        case.task,
        case.turn.story_id,
        seq,
        DirectorPlan(beat_id="b", reason="回应真实言论", jobs=jobs or [job]),
        expected_revision=revision,
    )
    return job, ledger


async def authorize(case, job):
    return await authorize_assignment(
        case.services, case.task, case.turn, case.actor, job, operation_scope="test:"
    )


@pytest.mark.parametrize(
    "extra", [{"objective": "你已读过账册，说明内容。"}, {"delivery_constraints": ["说明你已接过钥匙。"]}]
)
def test_typed_task_cannot_carry_free_prose(extra):
    with pytest.raises(ValidationError, match="夹带"):
        PerformanceJob(id="j", actor="a", grounded_response={"event_id": "e"}, **extra)


def test_typed_future_job_requires_real_dependency_and_cannot_predict_quote():
    with pytest.raises(ValidationError, match="依赖"):
        PerformanceJob(id="j", actor="a", grounded_response={"job_id": "earlier"})
    with pytest.raises(ValidationError, match="预写"):
        PerformanceJob(
            id="j",
            actor="a",
            depends_on=["earlier"],
            grounded_response={"job_id": "earlier", "focus_quote": "尚未说出"},
        )


async def test_same_actor_assignment_with_and_without_semantic_authorization(actor_case, fake):
    case = actor_case
    job, _ = install(case)
    fake.replies["director_assignment"] = {"checks": [], "accepted": True, "issues": []}
    case.services.config.role_assignment_authorization = "semantic"
    original = await authorize(case, job)
    assert [role for role, _, _ in fake.calls] == ["director_assignment"]
    case.services.config.role_assignment_authorization = "grounded_response"
    fast = await authorize(case, job)
    assert original == fast
    assert len(fake.calls) == 1
    assert fast["response_utterance"]["text"] == "你好。"
    with case.services.db.session() as s:
        audit = s.scalar(select(Operation).where(Operation.id.contains("assignment-grounded-v1")))
        assert audit.payload["source_event_id"] == case.task.payload["event_id"]


async def test_maximum_job_id_authorization_is_bounded_and_attempts_remain_distinct(actor_case, fake):
    case = actor_case
    case.services.config.role_assignment_authorization = "grounded_response"
    job = PerformanceJob(id="respond-to-player-" + "x" * 62, actor=case.actor.id,
        addressees=[case.turn.player_id], grounded_response={"event_id": case.task.payload["event_id"]})
    assert len(job.id) == 80
    install(case, jobs=[job])
    for attempt in [0, 0, 1]:
        await authorize_assignment(case.services, case.task, case.turn, case.actor, job,
            operation_scope=f"director:2:{job.id}:{attempt}:")
    with case.services.db.session() as s:
        rows = list(s.scalars(select(Operation).where(Operation.id.contains("assignment-grounded-v1"))))
        assert len(rows) == 2 and all(len(row.id) <= 200 for row in rows)
        assert all(row.payload["job_id"] == job.id for row in rows)
    assert not fake.calls


async def test_free_form_tasks_still_use_semantic_review_in_fast_mode(actor_case, fake):
    case = actor_case
    case.services.config.role_assignment_authorization = "grounded_response"
    fake.replies["director_assignment"] = {"checks": [], "accepted": True, "issues": []}
    await authorize(case, PerformanceJob(id="free", actor=case.actor.id, objective="表达个人看法"))
    assert [role for role, _, _ in fake.calls] == ["director_assignment"]


@pytest.mark.parametrize("source", ["missing", "private", "deleted", "unselected", "quote"])
async def test_invalid_or_unavailable_source_is_not_locally_authorized(actor_case, fake, monkeypatch, source):
    case = actor_case
    case.services.config.role_assignment_authorization = "grounded_response"
    event_id = case.task.payload["event_id"]
    if source == "missing":
        event_id = "missing-event"
    elif source == "private":
        with case.services.db.session.begin() as s:
            other = s.get(Conversation, case.turn.actor_ids[1])
            event_id = append_record(
                s,
                story_id=case.turn.story_id,
                kind="speech",
                actor=other,
                text="我藏着钥匙。",
                observer_ids=[other.id],
                channel="private",
                addressees=[other.id],
                dedupe="private-test",
            ).id
    elif source == "deleted":
        with case.services.db.session.begin() as s:
            s.get(SceneRecord, event_id).redacted = True
    job, _ = install(case, event_id=event_id)
    if source == "quote":
        job.grounded_response.focus_quote = "没有说过"
        install(case, jobs=[job])
    if source == "unselected":
        original = case.services.context.assemble

        def clipped(*args, **kwargs):
            messages, info = original(*args, **kwargs)
            return messages, {**info, "observations": []}

        monkeypatch.setattr(case.services.context, "assemble", clipped)
    with pytest.raises(AssignmentRejected):
        await authorize(case, job)
    assert not fake.calls


async def test_partial_unknown_speaker_is_not_expanded_and_focus_keeps_full_heard_text(actor_case):
    case = actor_case
    with case.services.db.session.begin() as s:
        other = s.get(Conversation, case.turn.actor_ids[1])
        heard = "我尚未决定是否同行。"
        event_id = append_record(
            s,
            story_id=case.turn.story_id,
            kind="speech",
            actor=other,
            text="钥匙藏在铁盒里。我尚未决定是否同行。",
            observer_ids=[other.id, case.actor.id],
            perceptions={case.actor.id: {"text": heard, "speaker_known": False}},
            dedupe="partial-response",
        ).id
    job, _ = install(case, event_id=event_id)
    job.grounded_response.focus_quote = "同行"
    install(case, jobs=[job])
    _, info = case.services.context.assemble(case.task, actor_id=case.actor.id)
    assignment, _ = compile_response(case.services, case.task, case.turn, case.actor, job, info)
    assert assignment["response_utterance"] == {
        "event_id": event_id,
        "speaker_id": None,
        "text": heard,
        "focus_quote": "同行",
    }
    assert "钥匙" not in str(assignment)


@pytest.mark.parametrize("change", ["deleted", "actor_privacy", "ledger", "assignment", "epoch", "lease"])
def test_compiled_authorization_is_revalidated_before_recording(actor_case, change):
    case = actor_case
    job, _ = install(case)
    _, info = case.services.context.assemble(case.task, actor_id=case.actor.id)
    assignment, proof = compile_response(case.services, case.task, case.turn, case.actor, job, info)
    with case.services.db.session.begin() as s:
        if change == "deleted":
            s.get(SceneRecord, case.task.payload["event_id"]).redacted = True
        elif change == "actor_privacy":
            s.get(Conversation, case.actor.id).privacy_version += 1
        elif change == "ledger":
            row = s.get(Operation, f"{case.task.id}:director-ledger")
            row.payload = {**row.payload, "revision": row.payload["revision"] + 1}
        elif change == "assignment":
            assignment["objective"] = "声称已经取得钥匙。"
        elif change == "epoch":
            s.get(Conversation, case.turn.player_id).epoch += 1
        else:
            from chatbot.storage import Task

            s.get(Task, case.task.id).lease_version += 1
    with pytest.raises(StaleTask):
        record_authorization(case.services, case.task, case.actor, job, assignment, proof, "test:")


async def test_reference_to_unpublished_job_is_never_a_speech(actor_case):
    case = actor_case
    job = PerformanceJob(
        id="second", actor=case.actor.id, depends_on=["first"], grounded_response={"job_id": "first"}
    )
    first = PerformanceJob(id="first", actor=case.turn.actor_ids[1], objective="先发言")
    install(case, jobs=[first, job])
    with pytest.raises(AssignmentRejected, match="未正式提交"):
        await authorize(case, job)


async def committed_dependency(case, fake):
    """Produce the dependency through the actual publication gate, not a forged status."""
    from support.story import intention

    first = PerformanceJob(
        id="first",
        actor=case.actor.id,
        addressees=[case.turn.player_id],
        grounded_response={"event_id": case.task.payload["event_id"], "delivery": "natural"},
    )
    second = PerformanceJob(
        id="second",
        actor=case.actor.id,
        addressees=[case.turn.player_id],
        depends_on=["first"],
        grounded_response={"job_id": "first", "delivery": "brief"},
    )
    _, ledger = install(case, jobs=[first, second])
    case.services.config.role_assignment_authorization = "grounded_response"
    fake.replies.update(
        role_intent=intention(case.turn.player_id, wait_for_player=True),
        dialogue={"text": "你好。", "claims": [], "spoken_parts": ["你好。"]},
    )
    # A real player question handoff stops after publishing the first job.
    fake.replies["role_intent"].update(expects_reply=True, awaiting_player_action=True)
    fake.replies["dialogue"]["text"] = "你愿意先说说来意吗？"
    fake.replies["dialogue"]["spoken_parts"] = ["你愿意先说说来意吗？"]
    result = await execute_plan(
        case.services, case.task, case.turn, ledger, SimpleNamespace(prepare_recall=AsyncMock())
    )
    assert len(result["segments"]) == 1
    with case.services.db.session() as s:
        case.actor = s.get(Conversation, case.actor.id)
    return second, result["segments"][0]["event_id"]


async def test_committed_dependency_uses_actual_speech_and_unchanged_audit(actor_case, fake):
    case = actor_case
    job, event_id = await committed_dependency(case, fake)
    assignment = await authorize(case, job)
    assert assignment["response_utterance"]["event_id"] == event_id
    assert assignment["response_utterance"]["text"] == "你愿意先说说来意吗？"
    assert "director_assignment" not in [role for role, _, _ in fake.calls]


@pytest.mark.parametrize(
    "change", ["candidate_status", "candidate_revision", "candidate_event", "commit_event", "commit_actor"]
)
async def test_predecessor_binding_is_rechecked_with_same_plan_revision(actor_case, fake, change):
    from copy import deepcopy

    case = actor_case
    job, _ = await committed_dependency(case, fake)
    _, info = case.services.context.assemble(case.task, actor_id=case.actor.id)
    assignment, proof = compile_response(case.services, case.task, case.turn, case.actor, job, info)
    with case.services.db.session.begin() as s:
        ledger = s.get(Operation, f"{case.task.id}:director-ledger")
        data = deepcopy(ledger.payload)
        if change == "candidate_status":
            data["candidates"]["first"]["status"] = "expired"
        elif change == "candidate_revision":
            data["candidates"]["first"]["revision"] += 1
        elif change == "candidate_event":
            data["candidates"]["first"]["event_id"] = case.task.payload["event_id"]
        else:
            committed = s.get(Operation, f"{case.task.id}:committed:0")
            committed.payload = {
                **committed.payload,
                "event_id" if change == "commit_event" else "actor_id": "wrong",
            }
        ledger.payload = data
    with pytest.raises(StaleTask):
        record_authorization(case.services, case.task, case.actor, job, assignment, proof, "test:")


async def test_first_pass_skips_one_authorization_call_but_keeps_speech_review(actor_case, fake):
    from support.story import intention

    case = actor_case
    case.services.config.role_assignment_authorization = "grounded_response"
    case.services.config.role_actor_generation = "combined_context"
    _, ledger = install(case)
    fake.replies["dialogue"] = {
        "intent": intention(case.turn.player_id, wait_for_player=False),
        "speech": {"text": "你好。", "claims": [], "spoken_parts": ["你好。"]},
    }
    memory = SimpleNamespace(prepare_recall=AsyncMock())
    result = await execute_plan(case.services, case.task, case.turn, ledger, memory)
    assert result["segments"] and result["segments"][0]["text"] == "你好。"
    assert [role for role, _, _ in fake.calls] == ["dialogue", "actor_review"]


async def test_local_authorization_does_not_publish_rejected_speech(actor_case, fake):
    from support.story import intention

    case = actor_case
    case.services.config.role_assignment_authorization = "grounded_response"
    _, ledger = install(case)
    fake.replies.update(
        role_intent=intention(case.turn.player_id, wait_for_player=False),
        dialogue={"text": "我已经替你找到钥匙。", "claims": []},
        actor_review={"accepted": False, "checks": [], "issues": ["凭空执行行动"]},
    )
    with pytest.raises(UnsafeDraftExhausted):
        await execute_plan(
            case.services, case.task, case.turn, ledger, SimpleNamespace(prepare_recall=AsyncMock())
        )
    with case.services.db.session() as s:
        assert not s.scalar(select(Operation).where(Operation.id.startswith(f"{case.task.id}:committed:")))
        assert not list(
            s.scalars(
                select(SceneRecord).where(
                    SceneRecord.task_id == case.task.id, SceneRecord.actor_id == case.actor.id
                )
            )
        )


@pytest.mark.parametrize("mode", ["semantic", "grounded_response"])
async def test_answer_only_contract_rejects_new_request_even_when_review_accepts(actor_case, fake, mode):
    from support.story import intention

    case = actor_case
    case.services.config.role_assignment_authorization = mode
    _, ledger = install(case)
    fake.replies.update(
        director_assignment={"checks": [], "accepted": True, "issues": []},
        role_intent=intention(case.turn.player_id, expects_reply=True),
        dialogue={"text": "我暂时不知道。你先替我核实，行吗？", "claims": []},
        actor_review={
            "accepted": True,
            "checks": [],
            "issues": [],
            "delivery": {
                "addressee_id": case.turn.player_id,
                "expects_reply": True,
                "response_kind": "answer",
                "remaining_request": None,
                "requires_player_action": True,
            },
        },
    )
    with pytest.raises(ResponseContractConflict):
        await execute_plan(
            case.services, case.task, case.turn, ledger, SimpleNamespace(prepare_recall=AsyncMock())
        )
    with case.services.db.session() as s:
        assert not s.get(Operation, f"{case.task.id}:committed:0")
    assert len([role for role, _, _ in fake.calls if role == "actor_review"]) == 1
