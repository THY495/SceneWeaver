"""Identity checks complement, never replace, the existing semantic reviewer."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_combined_actor_generation import actor_case as actor_case  # noqa: F401
from test_combined_actor_generation import perform, prepare

from chatbot.config import Settings
from chatbot.npc.performance import UnsafeDraftExhausted
from chatbot.npc.response_review import RESPONSE_BINDING_SYSTEM, BoundActorReview, binding_issues
from chatbot.npc.review import REVIEW_SYSTEM
from chatbot.observability.timing import TimingTrace
from chatbot.providers import Provider
from chatbot.storage import SceneRecord
from chatbot.voice_candidate import CandidateAudio


def enable(case):
    case.services.config.role_response_review_binding = True
    case.assignment.update(objective="回应访客实际发言", response_turn_policy="answer_only", response_utterance={
        "event_id": case.task.payload["event_id"], "speaker_id": case.turn.player_id,
        "text": case.task.payload["text"],
    })


def verdict(case, *, status="responded", quote="你好。", kind="answer"):
    return {"accepted": True, "issues": [], "checks": [], "delivery": {
        "addressee_id": case.turn.player_id, "expects_reply": False,
        "requires_player_action": False, "response_kind": kind},
        "response_assessment": {"source_event_id": case.task.payload["event_id"],
            "status": status, "quotes": [quote], "basis": "候选原文实际回应当前发言"}}


@pytest.mark.parametrize("fault,code", [
    ("source", "response_binding_source_mismatch"),
    ("quote", "response_binding_quote_mismatch"),
    ("blank", "response_binding_quote_blank"),
    ("empty", "response_binding_quote_missing"),
    ("not_addressed", "response_task_not_addressed"),
    ("delivery", "response_binding_delivery_mismatch"),
    ("missing", "response_binding_missing_or_invalid"),
])
def test_runtime_rejects_identity_and_verdict_errors_despite_accepted_true(fault, code):
    assignment = {"response_turn_policy": "answer_only", "response_utterance": {"event_id": "actual", "text": "你好。"}}
    review = {"accepted": True, "checks": [], "issues": [], "delivery": {"response_kind": "answer"},
        "response_assessment": {"source_event_id": "actual", "status": "responded", "quotes": ["你好。"], "basis": "实际回应"}}
    if fault == "source":
        review["response_assessment"]["source_event_id"] = "another-event"
    elif fault == "quote":
        review["response_assessment"]["quotes"] = ["未出现的答复"]
    elif fault == "blank":
        review["response_assessment"]["quotes"] = [" "]
    elif fault == "empty":
        review["response_assessment"]["quotes"] = []
    elif fault == "not_addressed":
        review["response_assessment"]["status"] = "not_addressed"
    elif fault == "delivery":
        review["response_assessment"]["status"] = "refused"
    else:
        review.pop("response_assessment")
    snapshot = copy.deepcopy(review)
    assert code in binding_issues(review, assignment, "你好。 ")
    assert review == snapshot


def test_exact_quote_and_label_do_not_prove_semantic_relevance():
    assignment = {"response_turn_policy": "answer_only", "response_utterance": {"event_id": "job-question", "text": "你做什么工作？"}}
    weather = "今天看着要变天，就多留了个心眼。"
    review = {"accepted": True, "checks": [], "issues": [], "delivery": {"response_kind": "answer"},
        "response_assessment": {"source_event_id": "job-question", "status": "responded", "quotes": [weather], "basis": "错误的相关性判断"}}
    # This is a counterexample, not a successful semantic test. The model's
    # relevance judgment must be calibrated on this exact false-positive case.
    assert binding_issues(review, assignment, weather) == []
    assert not Settings(_env_file=None).role_response_review_binding
    review["delivery"].pop("response_kind")
    with pytest.raises(ValidationError):
        BoundActorReview.model_validate(review)


@pytest.mark.parametrize("actor_case,status,kind,answer", [
    ("你好。", "responded", "answer", "你好。"),
    ("便条写了什么？", "unknown", "answer", "便条上的文字，我现在不知道。"),
    ("能讲讲你的私人经历吗？", "refused", "refuse", "这件事我不愿意谈。"),
    ("现在能谈谈吗？", "deferred", "defer", "这件事我想稍后再谈。"),
], indirect=["actor_case"])
async def test_ordinary_response_refusal_unknown_and_defer_keep_same_call_count(actor_case, fake, status, kind, answer):
    enable(actor_case)
    intent = await prepare(actor_case, fake)
    intent["_speech_draft"]["speech"] = {"text": answer, "spoken_parts": [answer], "claims": []}
    fake.replies["actor_review"] = verdict(actor_case, status=status, kind=kind, quote=answer)
    result = await perform(actor_case, fake, intent)
    assert result["answer"] == answer
    assert [role for role, _, _ in fake.calls] == ["dialogue", "actor_review"]
    review_call = fake.calls[-1]
    assert review_call[2]["schema"] is BoundActorReview
    assert review_call[1][0]["content"] == REVIEW_SYSTEM + RESPONSE_BINDING_SYSTEM
    assert "response_assessment" not in intent and "response_assessment" not in result


async def test_bound_cache_changes_with_mode_source_and_candidate(actor_case, fake):
    case, requests = actor_case, []
    enable(case)
    provider = Provider(case.services.db, case.services.config, case.services.runtime)
    await provider.http.aclose()

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        last = json.loads(body["messages"][-2]["content"])
        result = verdict(case, quote=last["candidate"])
        result["response_assessment"]["source_event_id"] = last["authorized_performance_requirement"]["response_utterance"]["event_id"]
        if RESPONSE_BINDING_SYSTEM not in body["messages"][0]["content"]:
            result.pop("response_assessment")
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]})

    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    original = fake.chat

    async def chat(task_id, role, messages, **kwargs):
        if role == "actor_review":
            return await provider.chat(task_id, role, messages, **kwargs)
        return await original(task_id, role, messages, **kwargs)

    fake.chat = chat
    try:
        case.services.config.role_response_review_binding = False
        await perform(case, fake, await prepare(case, fake))
        case.services.config.role_response_review_binding = True
        await perform(case, fake, await prepare(case, fake))
        await perform(case, fake, await prepare(case, fake))
        assert len(requests) == 2
        assert requests[0]["messages"][1:-1] == requests[1]["messages"][1:-1]
        # Same task and response words, different actual source => no old verdict.
        case.assignment["response_utterance"] = {**case.assignment["response_utterance"], "event_id": "different-source"}
        await perform(case, fake, await prepare(case, fake))
        assert len(requests) == 3
        intent = await prepare(case, fake)
        intent["_speech_draft"]["speech"] = {"text": "您好。", "spoken_parts": ["您好。"], "claims": []}
        await perform(case, fake, intent)
        assert len(requests) == 4
    finally:
        await provider.close()


@pytest.mark.parametrize("fault", ["not_addressed", "forged_quote", "unsupported", "issues"])
async def test_binding_rejection_discards_parallel_audio_and_commits_nothing(actor_case, fake, fault):
    case = actor_case
    enable(case)
    intent = await prepare(case, fake)
    original = fake.chat
    audio_started, review_started = asyncio.Event(), asyncio.Event()

    async def chat(task_id, role, messages, **kwargs):
        if role == "actor_review":
            review_started.set()
            if not audio_started.is_set():
                await asyncio.wait_for(audio_started.wait(), 2)
            review = verdict(case, status="not_addressed" if fault == "not_addressed" else "responded",
                           quote="你好。" if fault == "not_addressed" else "不存在的原文")
            if fault in {"unsupported", "issues"}:
                review["response_assessment"]["quotes"] = ["你好。"]
                if fault == "unsupported":
                    review["checks"] = [{"text": "你好。", "basis": "测试故障：事实检查拒绝",
                        "supported": False, "reason_code": "unsupported_fact"}]
                else:
                    review["issues"] = ["测试故障：仍有表达问题"]
            return review
        return await original(task_id, role, messages, **kwargs)

    async def audio(task, text, voice, direction):
        audio_started.set()
        await asyncio.wait_for(review_started.wait(), 2)
        return CandidateAudio(text, voice, direction, "binding-private", b"\x00\x00")

    fake.chat = chat
    discard = AsyncMock()
    with case.services.db.session() as s:
        before = s.scalar(select(func.count()).select_from(SceneRecord))
    trace = TimingTrace(case.task)
    with trace.activate(), pytest.raises(UnsafeDraftExhausted):
        await perform(case, fake, intent, voice_service=SimpleNamespace(stream_candidate=audio, discard_candidate=discard), voice="Cherry")
    assert audio_started.is_set() and discard.await_count == 1
    with case.services.db.session() as s:
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == before
    results = [span for span in trace.spans if span["name"] == "npc.semantic_review_result"]
    assert results and all(span["attributes"]["review_provider_accepted"] is True for span in results)
    assert all(span["attributes"]["review_accepted"] is False for span in results)
    assert all(span["attributes"]["response_binding_accepted"] == (fault in {"unsupported", "issues"})
        for span in results)
    assert "不存在的原文" not in json.dumps(trace.spans, ensure_ascii=False)


def test_calibration_pairs_keep_identical_data_and_do_not_send_expected_grades(monkeypatch):
    import importlib

    root = Path(__file__).parents[1]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    calibration = importlib.import_module("calibrate_reviewer")
    suite = json.loads((root / "evals/cases/reviewer-response-binding-v1.json").read_text("utf-8"))
    originals = calibration.materialize_cases(suite)
    snapshot = copy.deepcopy(originals)
    plan = calibration.response_binding_plan(originals, "paired")
    assert originals == snapshot and len(plan) == 16
    assert [case["review_response_binding"] for case in plan[:4]] == [False, True, True, False]
    for case in originals:
        off = calibration.candidate_messages(case)
        on = calibration.candidate_messages(case, response_binding=True)
        assert off[1:] == on[1:] and on[0]["content"] == REVIEW_SYSTEM + RESPONSE_BINDING_SYSTEM
        changed = {**case, "case_id": "HIDDEN", "rationale": "HIDDEN", "expected_response_status": "HIDDEN", "expected_accept": not case["expected_accept"]}
        assert calibration.candidate_messages(changed, response_binding=True) == on
    with pytest.raises(ValueError, match="24 planned calls"):
        calibration.response_binding_plan(originals * 2, "paired")
    with pytest.raises(ValueError, match="full speech"):
        calibration.response_binding_plan(originals, "paired", explanation="concise")
    with pytest.raises(ValueError, match="response source"):
        calibration.response_binding_plan([{**originals[0], "director_assignment": {}}], "on")


def test_calibration_distinguishes_model_verdict_from_runtime_gate(monkeypatch):
    import importlib

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    calibration = importlib.import_module("calibrate_reviewer")
    raw = {"accepted": True, "checks": [], "issues": [],
        "response_assessment": {"status": "not_addressed"}}
    report = calibration.response_binding_summary([{
        "case_id": "unrelated", "expected_accept": False,
        "expected_response_status": "not_addressed", "review_response_binding": True,
        "provider_review": raw, "provider_observed_accept": True,
        "review": {**raw, "accepted": False}, "observed_accept": False,
        "binding_issues": ["response_task_not_addressed"],
    }])
    assert report["provider_verdict"]["false_accept"] == 1
    assert report["after_runtime_gate"]["false_accept"] == 0
    assert report["gate_changed_accept_to_reject"] == 1
    assert report["expected_status_matched"] == 1
