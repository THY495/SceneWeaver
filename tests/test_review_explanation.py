import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_combined_actor_generation import actor_case as actor_case
from test_combined_actor_generation import perform, prepare

from chatbot.config import Settings
from chatbot.npc.performance import UnsafeDraftExhausted
from chatbot.npc.review import CONCISE_EXPLANATION, REVIEW_SYSTEM
from chatbot.providers import Provider
from chatbot.schemas import ActorReview
from chatbot.voice_candidate import CandidateAudio


def calibration():
    spec = importlib.util.spec_from_file_location(
        "calibration_explanation", Path(__file__).parents[1] / "scripts/calibrate_reviewer.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate():
    module = calibration()
    path = Path(__file__).parents[1] / "evals/cases/reviewer-procedure-v4.json"
    return module.materialize_cases(json.loads(path.read_text("utf-8")))[0]


def test_default_and_short_modes_share_all_grounding_and_rules():
    module, case = calibration(), candidate()
    full = module.candidate_messages(case)
    short = module.candidate_messages(case, explanation="concise")
    assert full[0]["content"] == REVIEW_SYSTEM
    assert short[0]["content"] == REVIEW_SYSTEM + CONCISE_EXPLANATION
    assert short[1:] == full[1:]
    altered = {**case, "expected_accept": not case["expected_accept"], "rationale": "SECRET_LABEL"}
    assert module.candidate_messages(altered, explanation="concise") == short
    assert "role_review_explanation" not in Settings.model_fields
    with pytest.raises(ValueError, match="Unknown"):
        module.candidate_messages(case, explanation="unsupported")


def test_pairs_alternate_order_without_mutating_candidates_or_exceeding_call_cap():
    module, case = calibration(), candidate()
    originals = [{**case, "repeat": 1}, {**case, "repeat": 2}]
    snapshot = copy.deepcopy(originals)
    plan = module.explanation_plan(originals, "paired")
    assert [row["review_explanation"] for row in plan] == ["full", "concise", "concise", "full"]
    assert originals == snapshot
    assert all(
        {k: v for k, v in row.items() if k != "review_explanation"} == originals[i // 2]
        for i, row in enumerate(plan)
    )
    with pytest.raises(ValueError, match="24 planned calls"):
        module.explanation_plan(originals * 7, "paired")
    with pytest.raises(ValueError, match="speech calibration"):
        module.explanation_plan(originals, "concise", assignment_target=True)


@pytest.mark.parametrize("cap", [-1, 0, 1.3, float("nan")])
async def test_invalid_batch_cap_stops_before_credentials_or_database(tmp_path, monkeypatch, cap):
    module = calibration()
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [candidate()]}), encoding="utf-8")

    def forbidden():
        raise AssertionError("Must reject before reading settings")

    monkeypatch.setattr(module, "Settings", forbidden)
    args = SimpleNamespace(cases=path, case_id=None, repeats=1, max_spend_usd=cap)
    with pytest.raises(ValueError, match="cap must be within"):
        await module.run(args)


async def test_live_review_keeps_full_contract_after_calibration_calls(actor_case, fake):
    case = actor_case
    reviews = []
    calibration().candidate_messages(candidate(), explanation="concise")
    for _ in range(2):
        intent = await prepare(case, fake)
        await perform(case, fake, intent)
        reviews.append(fake.calls[-1])
    assert [row[0] for row in reviews] == ["actor_review"] * 2
    assert reviews[0][2]["operation"] == reviews[1][2]["operation"]
    assert all(row[1][0]["content"] == REVIEW_SYSTEM for row in reviews)
    assert reviews[0][1][1:] == reviews[1][1][1:]
    assert all(row[2]["schema"] is ActorReview and row[2]["max_tokens"] == 1000 for row in reviews)


async def test_live_full_cache_and_calibration_short_cache_are_isolated(actor_case, fake):
    case, requests = actor_case, []
    captured_messages = []
    provider = Provider(case.services.db, case.services.config, case.services.runtime)
    await provider.http.aclose()

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        review = {
            "checks": [],
            "accepted": True,
            "issues": [],
            "delivery": {
                "addressee_id": case.turn.player_id,
                "expects_reply": False,
                "requires_player_action": False,
                "response_kind": "answer",
            },
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(review)}, "finish_reason": "stop"}]}
        )

    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    original = fake.chat

    async def chat(task_id, role, messages, **kwargs):
        if role == "actor_review":
            captured_messages.append(copy.deepcopy(messages))
            return await provider.chat(task_id, role, messages, **kwargs)
        return await original(task_id, role, messages, **kwargs)

    fake.chat = chat
    try:
        for _ in range(2):
            await perform(case, fake, await prepare(case, fake))
        assert len(requests) == 1
        short_messages = copy.deepcopy(captured_messages[0])
        short_messages[0]["content"] += CONCISE_EXPLANATION
        import hashlib

        basis = hashlib.sha256(
            json.dumps(short_messages, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:24]
        await provider.chat(
            case.task.id,
            "actor_review",
            short_messages,
            schema=ActorReview,
            operation=f"calibration-short:{basis}",
            max_tokens=1000,
        )
        await perform(case, fake, await prepare(case, fake))
        assert len(requests) == 2
        assert requests[0]["messages"][1:] == requests[1]["messages"][1:]
    finally:
        await provider.close()


async def test_short_basis_cannot_publish_unsupported_checks_or_audio(actor_case, fake):
    case = actor_case
    intent = await prepare(case, fake)
    audio_started, review_started = asyncio.Event(), asyncio.Event()
    original = fake.chat

    async def chat(task_id, role, messages, **kwargs):
        if role == "actor_review":
            review_started.set()
            await asyncio.wait_for(audio_started.wait(), 2)
            # Even an inconsistent accepted flag cannot overrule a failed check.
            return {
                "accepted": True,
                "issues": [],
                "checks": [
                    {
                        "text": "你好。",
                        "basis": "没有依据",
                        "supported": False,
                        "reason_code": "unsupported_fact",
                    }
                ],
            }
        return await original(task_id, role, messages, **kwargs)

    async def audio(task, text, voice, direction):
        audio_started.set()
        await asyncio.wait_for(review_started.wait(), 2)
        return CandidateAudio(text, voice, direction, "test-scope", b"\x00\x00")

    fake.chat = chat
    discard = AsyncMock()
    with pytest.raises(UnsafeDraftExhausted):
        await perform(
            case,
            fake,
            intent,
            voice="Cherry",
            voice_service=SimpleNamespace(stream_candidate=audio, discard_candidate=discard),
        )
    assert discard.await_count == 1
