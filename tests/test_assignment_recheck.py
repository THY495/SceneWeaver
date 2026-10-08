"""Keep original actor jobs on evidence-format recovery without widening authority."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from chatbot.directing import PerformanceJob
from chatbot.npc.assignment import (
    AssignmentRejected,
    ReplyCapacityRejected,
    authorize_assignment,
)
from chatbot.npc.context import TurnContext
from chatbot.observability.timing import TimingTrace
from chatbot.providers import StructuredOutputError
from chatbot.runtime import StaleTask
from chatbot.storage import now


def setup(replies):
    info = {
        "profile": {"core": "谨慎说明私密设定"},
        "perceived_cast": [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}, {"id": "p", "name": "玩家"}],
        "observations": [{"kind": "speech", "actor_id": "b", "text": "乙说目前没有确切消息，先等观察"}],
    }
    provider = SimpleNamespace(chat=AsyncMock(side_effect=replies))
    services = SimpleNamespace(
        runtime=SimpleNamespace(check=Mock()),
        context=SimpleNamespace(assemble=Mock(return_value=([], info))),
        provider=provider,
    )
    task = SimpleNamespace(id="t", lease_version=1, attempt=1, created_at=now(), updated_at=now())
    job = PerformanceJob(
        id="j",
        actor="a",
        addressees=["b"],
        objective="回应乙已说的消息，说明已知与未知",
        delivery_constraints=["不代替玩家作决定"],
    )
    args = (services, task, TurnContext("s", "p", ("a", "b")), SimpleNamespace(id="a"), job)
    return provider, args


def bad_review():
    return {
        "accepted": True,
        "issues": [],
        "reply_requirement": "none",
        "checks": [
            {
                "text": "不代替玩家作决定",
                "basis": "局部表达",
                "supported": True,
                "premise_kind": "expression",
                "support_extent": "not_needed",
                "source_kind": "profile",
                "source_quote": "谨慎说明私密设定",
            }
        ],
    }


def good_review():
    value = bad_review()
    value["checks"][0].update(source_kind="expression", source_quote=None)
    return value


def reference_review():
    value = good_review()
    check = value["checks"][0]
    del check["source_kind"]
    del check["source_quote"]
    check["source_ref"] = None
    return value


async def test_recheck_uses_same_scope_and_original_unmutated_verdict_then_returns_same_job():
    provider, args = setup([bad_review(), reference_review()])
    job = args[-1]
    original = job.model_dump()
    trace = TimingTrace(args[1])
    with trace.activate():
        result = await authorize_assignment(*args, operation_scope="test:")
    assert result == {key: original[key] for key in ("objective", "addressees", "delivery_constraints")}
    assert job.model_dump() == original
    first, second = provider.chat.call_args_list
    assert first.args[1] == second.args[1] == "director_assignment"
    assert [message["role"] for message in second.args[2]] == ["system", "user"]
    request = json.loads(second.args[2][-1]["content"])
    original_request = json.loads(first.args[2][-1]["content"])
    assert all(request[key] == value for key, value in original_request.items())
    assert request["runtime_evidence_errors"] == ["expression_source_invalid"]
    assert "review_to_recheck" not in request
    assert all("text" not in record for record in request["evidence_catalog"])
    assert first.kwargs["operation"] != second.kwargs["operation"]
    assert "reference-recheck-v1" in second.kwargs["operation"]
    assert second.kwargs["max_attempts"] == 1
    assert provider.chat.await_count == 2
    parent = next(s for s in trace.spans if s["name"] == "npc.assignment")
    recheck = next(s for s in trace.spans if s["name"] == "npc.assignment_review_recheck")
    assert parent["attributes"]["assignment_evidence_expression_source_invalid_count"] == 1
    assert recheck["parent_id"] == parent["id"]
    assert "accepted" in recheck["marks"]
    assert "私密" not in json.dumps(trace.spans, ensure_ascii=False)


@pytest.mark.parametrize("failure", ["same_format", "semantic", "utterance_promotion"])
async def test_failed_recheck_does_not_generate_a_new_job_or_accept_old_approval(failure):
    second = reference_review()
    if failure == "same_format":
        second = StructuredOutputError("invalid json", [])
    if failure == "semantic":
        second.update(accepted=False, issues=["任务仍无依据"])
        second["checks"][0].update(supported=False, support_extent="none")
    elif failure == "utterance_promotion":
        second["checks"][0].update(
            premise_kind="world_fact",
            support_extent="full",
            source_ref="observed-0:0",
        )
    provider, args = setup([bad_review(), second])
    original = args[-1].model_dump()
    with pytest.raises(AssignmentRejected):
        await authorize_assignment(*args, operation_scope="test:")
    assert provider.chat.await_count == 2
    assert all(call.args[1] == "director_assignment" for call in provider.chat.call_args_list)
    assert args[-1].model_dump() == original


@pytest.mark.parametrize("reason", ["model_rejection", "partial_support"])
async def test_original_semantic_rejection_keeps_bounded_goal_repair(reason):
    rejected = bad_review()
    if reason == "model_rejection":
        rejected.update(accepted=False, issues=["未知前提"])
        rejected["checks"][0]["supported"] = False
    else:
        rejected["checks"][0]["support_extent"] = "partial"
    replacement = {"objective": "仅说明已知部分", "delivery_constraints": []}
    provider, args = setup([rejected, replacement, {"accepted": True, "issues": [], "checks": []}])
    result = await authorize_assignment(*args, operation_scope="test:")
    assert result["objective"] == replacement["objective"]
    assert [call.args[1] for call in provider.chat.call_args_list] == [
        "director_assignment",
        "director_assignment_repair",
        "director_assignment",
    ]


async def test_repaired_job_has_no_second_recovery_opportunity():
    provider, args = setup([bad_review()])
    with pytest.raises(AssignmentRejected):
        await authorize_assignment(*args, operation_scope="test:", _repair_allowed=False)
    provider.chat.assert_awaited_once()


async def test_recheck_does_not_bypass_reply_capacity():
    fixed = reference_review()
    fixed["reply_requirement"] = "required"
    provider, args = setup([bad_review(), fixed])
    with pytest.raises(ReplyCapacityRejected):
        await authorize_assignment(
            *args,
            operation_scope="test:",
            dialogue_capacity={"scheduled_reply_actor_ids": [], "can_schedule_more": False},
        )
    assert provider.chat.await_count == 2


async def test_stale_task_cannot_launch_recheck():
    provider, args = setup([bad_review()])
    args[0].runtime.check.side_effect = [None, StaleTask("expired")]
    with pytest.raises(StaleTask):
        await authorize_assignment(*args, operation_scope="test:")
    provider.chat.assert_awaited_once()
