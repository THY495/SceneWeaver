import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from chatbot.directing import PerformanceJob
from chatbot.npc.assignment import authorize_assignment
from chatbot.npc.context import TurnContext
from chatbot.providers import ProviderError


@pytest.mark.parametrize("accepted,supported", [(True, True), (False, True), (True, False)])
async def test_assignment_review_only_forwards_accepted_goal(accepted, supported):
    info = {
        "perceived_cast": [{"id": "a", "name": "甲"}, {"id": "p", "name": "玩家"}],
        "profile": {"core": "尚不知道钥匙在哪里"},
    }
    original = deepcopy(info)

    async def answer(_task, _role, _messages, *, operation, **_kwargs):
        if "goal-repair" in operation:
            return {"objective": "仅询问玩家的意见", "delivery_constraints": ["一到两句"]}
        return {
            "accepted": accepted,
            "issues": [],
            "checks": [{"text": "预设", "basis": "检查依据", "supported": supported}],
        }

    provider = SimpleNamespace(chat=AsyncMock(side_effect=answer))
    services = SimpleNamespace(
        runtime=SimpleNamespace(check=Mock()),
        provider=provider,
        context=SimpleNamespace(assemble=Mock(return_value=([], info))),
    )
    task = SimpleNamespace(id="t", lease_version=1)
    job = PerformanceJob(
        id="j",
        actor="a",
        objective="询问玩家的意见",
        addressees=["p"],
        delivery_constraints=["一到两句"],
    )
    args = (services, task, TurnContext("s", "p", ("a",)), SimpleNamespace(id="a"), job)
    if accepted and supported:
        assert await authorize_assignment(*args, operation_scope="test:") == {
            "objective": job.objective,
            "addressees": ["p"],
            "delivery_constraints": ["一到两句"],
        }
    else:
        with pytest.raises(ProviderError, match="知情边界"):
            await authorize_assignment(*args, operation_scope="test:")
    assert info == original
    proposal = json.loads(provider.chat.call_args_list[0].args[2][1]["content"])
    assert proposal["performer_id"] == "a"
    assert proposal["assignment"]["addressees"] == ["p"]
    assert proposal["assignment"]["delivery_constraints"] == ["一到两句"]
    job.addressees = ["hidden"]
    with pytest.raises(ProviderError, match="不可感知"):
        await authorize_assignment(*args, operation_scope="test:")
    assert provider.chat.await_count == (1 if accepted and supported else 3)


async def test_assignment_approval_with_issues_is_rechecked_before_actor_sees_it():
    info = {"perceived_cast": [{"id": "a", "name": "甲"}, {"id": "p", "name": "玩家"}]}
    provider = SimpleNamespace(
        chat=AsyncMock(
            side_effect=[
                {
                    "accepted": True,
                    "issues": ["若要确认未读过的公告原文，需要再核实。"],
                    "checks": [{"text": "公告原文", "basis": "任务未要求确认原文", "supported": False}],
                },
                {"accepted": True, "issues": [], "checks": []},
            ]
        )
    )
    services = SimpleNamespace(
        runtime=SimpleNamespace(check=Mock()),
        provider=provider,
        context=SimpleNamespace(assemble=Mock(return_value=([], info))),
    )
    job = PerformanceJob(
        id="j",
        actor="a",
        objective="回应玩家的转述，不确认公告原文",
        addressees=["p"],
        delivery_constraints=["未知就承认未知"],
    )
    result = await authorize_assignment(
        services,
        SimpleNamespace(id="t", lease_version=1),
        TurnContext("s", "p", ("a",)),
        SimpleNamespace(id="a"),
        job,
        operation_scope="test:",
    )
    assert result["objective"] == job.objective
    assert provider.chat.await_count == 2
    assert "format-repair" in provider.chat.await_args.kwargs["operation"]


async def test_rejected_assignment_is_rewritten_once_and_rechecked_before_actor_sees_it():
    info = {
        "perceived_cast": [{"id": "a", "name": "甲"}, {"id": "p", "name": "玩家"}],
        "profile": {"core": "没有读过登记册"},
    }

    async def answer(_task, _role, messages, *, operation, **_kwargs):
        if "goal-repair" in operation:
            assert "没有读过登记册" in messages[1]["content"]
            return {
                "objective": "回应玩家的问题，仅说自己实际知道的部分，不知道就承认未知",
                "delivery_constraints": ["一到两句"],
            }
        proposal = json.loads(messages[1]["content"])["assignment"]
        if "亲眼读过登记册" in proposal["objective"]:
            return {
                "accepted": False,
                "issues": ["没有依据证明演员读过登记册"],
                "checks": [{"text": "亲眼读过登记册", "basis": "授权状态无此记录", "supported": False}],
            }
        return {"accepted": True, "issues": [], "checks": []}

    provider = SimpleNamespace(chat=AsyncMock(side_effect=answer))
    services = SimpleNamespace(
        runtime=SimpleNamespace(check=Mock()),
        provider=provider,
        context=SimpleNamespace(assemble=Mock(return_value=([], info))),
    )
    job = PerformanceJob(
        id="j",
        actor="a",
        objective="说明自己亲眼读过登记册后发现了什么",
        addressees=["p"],
        delivery_constraints=["一到两句"],
    )
    result = await authorize_assignment(
        services,
        SimpleNamespace(id="t", lease_version=1),
        TurnContext("s", "p", ("a",)),
        SimpleNamespace(id="a"),
        job,
        operation_scope="test:",
    )
    assert result == {
        "objective": "回应玩家的问题，仅说自己实际知道的部分，不知道就承认未知",
        "addressees": ["p"],
        "delivery_constraints": ["一到两句"],
    }
    assert job.objective == "说明自己亲眼读过登记册后发现了什么"
    assert [call.args[1] for call in provider.chat.call_args_list] == [
        "director_assignment",
        "director_assignment_repair",
        "director_assignment",
    ]


async def test_recipient_conflict_is_repaired_before_actor_receives_assignment():
    info = {
        "perceived_cast": [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}, {"id": "p", "name": "玩家"}]
    }

    async def answer(_task, role, messages, **_kwargs):
        if role == "director_assignment_repair":
            payload = json.loads(messages[1]["content"])
            assert payload["rejected_assignment"]["addressees"] == ["p"]
            assert "保留addressees" in payload["recipient_contract"]
            return {"objective": "向玩家说明目前所知，其余承认未知", "delivery_constraints": []}
        assert "如需可询问其他人" in messages[0]["content"]
        proposal = json.loads(messages[1]["content"])["assignment"]
        rejected = "询问乙" in proposal["objective"]
        return {"accepted": not rejected, "issues": ["单一接收者契约冲突"] if rejected else [], "checks": []}

    provider = SimpleNamespace(chat=AsyncMock(side_effect=answer))
    services = SimpleNamespace(
        runtime=SimpleNamespace(check=Mock()),
        provider=provider,
        context=SimpleNamespace(assemble=Mock(return_value=([], info))),
    )
    job = PerformanceJob(id="j", actor="a", objective="向玩家说明所知；如需可询问乙", addressees=["p"])
    result = await authorize_assignment(
        services,
        SimpleNamespace(id="t", lease_version=1),
        TurnContext("s", "p", ("a", "b")),
        SimpleNamespace(id="a"),
        job,
        operation_scope="test:",
    )
    assert result["addressees"] == ["p"]
    assert "询问乙" not in result["objective"]
    assert provider.chat.await_count == 3


@pytest.mark.parametrize(
    "requirement,capacity,blocked",
    [
        ("required", {"scheduled_reply_actor_ids": [], "can_schedule_more": False}, True),
        ("optional", {"scheduled_reply_actor_ids": [], "can_schedule_more": False}, False),
        ("required", {"scheduled_reply_actor_ids": ["b"], "can_schedule_more": False}, False),
        ("required", {"scheduled_reply_actor_ids": [], "can_schedule_more": True}, False),
        ("required", None, False),
    ],
)
async def test_required_npc_question_without_reply_slot_is_replanned_before_actor(
    requirement, capacity, blocked
):
    from chatbot.npc.assignment import ReplyCapacityRejected

    info = {
        "perceived_cast": [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}, {"id": "p", "name": "玩家"}]
    }
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value={"accepted": True, "issues": [], "checks": [], "reply_requirement": requirement}
        )
    )
    services = SimpleNamespace(
        runtime=SimpleNamespace(check=Mock()),
        provider=provider,
        context=SimpleNamespace(assemble=Mock(return_value=([], info))),
    )
    job = PerformanceJob(id="j", actor="a", objective="询问乙知道什么", addressees=["b"])
    call = authorize_assignment(
        services,
        SimpleNamespace(id="t", lease_version=1),
        TurnContext("s", "p", ("a", "b")),
        SimpleNamespace(id="a"),
        job,
        operation_scope="test:",
        dialogue_capacity=capacity,
    )
    if blocked:
        with pytest.raises(ReplyCapacityRejected, match="后续发言位置"):
            await call
    else:
        assert (await call)["objective"] == job.objective
    provider.chat.assert_awaited_once()

    assert "回复位置由程序判断" in provider.chat.call_args.args[2][0]["content"]


@pytest.mark.parametrize(
    "fields,reason",
    [
        (
            {
                "premise_kind": "reported_statement",
                "support_extent": "full",
                "source_kind": "utterance",
                "source_quote": "确认背景资料",
            },
            "source_class_invalid",
        ),
        ({"premise_kind": "expression"}, "missing_fields"),
        (
            {"premise_kind": "expression", "support_extent": "partial", "source_kind": "expression"},
            "extent_invalid",
        ),
        (
            {
                "premise_kind": "past_action",
                "support_extent": "full",
                "source_kind": "utterance",
                "source_quote": "我昨晚读过公告",
            },
            "source_class_invalid",
        ),
        (
            {
                "premise_kind": "expression",
                "support_extent": "not_needed",
                "source_kind": "profile",
                "source_quote": "人物档案私密内容",
            },
            "expression_source_invalid",
        ),
        (
            {"premise_kind": "reported_statement", "support_extent": "full", "source_kind": "utterance"},
            "quote_missing",
        ),
        (
            {
                "premise_kind": "reported_statement",
                "support_extent": "full",
                "source_kind": "utterance",
                "source_quote": "我昨晚……公告",
            },
            "quote_not_found",
        ),
    ],
)
def test_runtime_evidence_rejection_has_one_content_free_reason(fields, reason):
    from chatbot.npc.assignment import AssignmentReview, validate_assignment_evidence
    from chatbot.observability.timing import TimingTrace, measure
    from chatbot.storage import now

    info = {
        "profile": {"core": "人物档案私密内容"},
        "scene_facts": {"opening": {"value": "确认背景资料"}},
        "observations": [{"kind": "speech", "text": "我昨晚读过公告", "actor_id": "a"}],
    }
    review = AssignmentReview.model_validate(
        {
            "accepted": True,
            "issues": [],
            "checks": [{"text": "待审核任务私密内容", "basis": "模型私密说明", "supported": True, **fields}],
        }
    )
    trace = TimingTrace(
        SimpleNamespace(id="t", attempt=1, lease_version=1, created_at=now(), updated_at=now())
    )
    with trace.activate(), measure("npc.assignment"):
        validated = validate_assignment_evidence(review, info)
    assert not validated.checks[0].supported
    assert validated.accepted and not validated.issues
    attributes = trace.spans[0]["attributes"]
    assert attributes[f"assignment_evidence_{reason}_count"] == 1
    assert len(attributes) == 6 and sum(attributes.values()) == 1
    assert "私密" not in json.dumps(trace.spans, ensure_ascii=False)
    assert "我昨晚" not in json.dumps(trace.spans, ensure_ascii=False)


@pytest.mark.parametrize("legacy", [False, True])
def test_evidence_diagnostics_do_not_reclassify_existing_model_rejection_or_legacy(legacy):
    from chatbot.npc.assignment import AssignmentReview, validate_assignment_evidence
    from chatbot.observability.timing import TimingTrace, measure
    from chatbot.storage import now

    fields = (
        {} if legacy else {"premise_kind": "past_action", "support_extent": "none", "source_kind": "none"}
    )
    review = AssignmentReview.model_validate(
        {
            "accepted": legacy,
            "issues": [] if legacy else ["模型已拒绝"],
            "checks": [{"text": "原任务", "basis": "原依据", "supported": legacy, **fields}],
        }
    )
    original = review.model_dump()
    trace = TimingTrace(
        SimpleNamespace(id="t", attempt=1, lease_version=1, created_at=now(), updated_at=now())
    )
    with trace.activate(), measure("npc.assignment"):
        assert validate_assignment_evidence(review, {}).model_dump() == original
    assert sum(trace.spans[0]["attributes"].values()) == 0


@pytest.mark.parametrize(
    "kind,extent,source,quote,expected",
    [
        ("past_inaction", "partial", "utterance", "我不知道记录是否准确", False),
        ("past_inaction", "full", "utterance", "我从未核对过", False),
        ("past_inaction", "full", "profile", "我从未核对过", True),
        ("past_inaction", "full", "profile", "不存在的引用", False),
        ("world_fact", "none", "none", None, False),
        ("current_uncertainty", "not_needed", "expression", None, True),
        ("reported_statement", "full", "utterance", "我不知道记录是否准确", True),
        ("world_fact", "full", "confirmed_event", "册子在桌上", True),
    ],
)
def test_assignment_evidence_extent_and_source_class_cannot_be_overridden_by_acceptance(
    kind, extent, source, quote, expected
):
    from chatbot.npc.assignment import AssignmentReview, validate_assignment_evidence

    info = {
        "profile": {"core": "明确经历：\n我从未核对过"},
        "scene_facts": {"book": {"value": "册子在桌上"}},
        "observations": [
            {"id": "u", "kind": "speech", "actor_id": "a", "text": "我不知道记录是否准确；我从未核对过"}
        ],
    }
    review = AssignmentReview.model_validate(
        {
            "checks": [
                {
                    "text": "任务预设",
                    "basis": "模型的依据说明",
                    "supported": True,
                    "premise_kind": kind,
                    "support_extent": extent,
                    "source_kind": source,
                    "source_quote": quote,
                }
            ],
            "accepted": True,
            "issues": [],
        }
    )
    validated = validate_assignment_evidence(review, info)
    assert validated.checks[0].supported is expected
    assert validated.accepted  # Raw approval is preserved; checks still gate authorization.


async def test_partial_assignment_support_is_rejected_before_actor_without_extra_review():
    from chatbot.npc.assignment import AssignmentRejected

    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value={
                "accepted": True,
                "issues": [],
                "checks": [
                    {
                        "text": "尚未核对",
                        "basis": "只知道当前未知",
                        "supported": True,
                        "premise_kind": "past_inaction",
                        "support_extent": "partial",
                        "source_kind": "utterance",
                        "source_quote": "不知道是否准确",
                    }
                ],
            }
        )
    )
    info = {"perceived_cast": [{"id": "a", "name": "甲"}, {"id": "p", "name": "玩家"}]}
    services = SimpleNamespace(
        runtime=SimpleNamespace(check=Mock()),
        provider=provider,
        context=SimpleNamespace(assemble=Mock(return_value=([], info))),
    )
    job = PerformanceJob(id="j", actor="a", objective="说明尚未核对", addressees=["p"])
    with pytest.raises(AssignmentRejected, match="知情边界") as caught:
        await authorize_assignment(
            services,
            SimpleNamespace(id="t", lease_version=1),
            TurnContext("s", "p", ("a",)),
            SimpleNamespace(id="a"),
            job,
            operation_scope="test:",
            _repair_allowed=False,
        )
    assert [p.model_dump() for p in caught.value.rejected_premises] == [{
        "text": "尚未核对", "premise_kind": "past_inaction", "code": "unsupported_premise",
    }]
    assert "只知道当前未知" not in repr(caught.value.rejected_premises)
    assert "不知道是否准确" not in repr(caught.value.rejected_premises)
    provider.chat.assert_awaited_once()
